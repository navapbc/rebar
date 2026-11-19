"""Bug ff4a-2832-def4-4e55 — a PASSED plan-review persisted an unverifiable attestation.

The bug had two defects. This module covers the first; the second (sign-time refusal of a key
not pinned for its principal) was retired with the repository pin file by ADR 0125, because
signing is never gated on an allowed key.

**(1) The async gate surface loses the op-cert signer binding.** ``_spawn_gate_daemon``
(``rebar._mcp_llm``) and the ``run_workflow`` daemon (``rebar._mcp_writes``) run their work on a
bare ``threading.Thread``, which starts with a FRESH ``contextvars.Context``. The signer bound by
``bound_signer()`` in the uvicorn serving thread is therefore invisible inside them, so
``ensure_opcert_key`` falls through to the ``<tracker>/.opcert-key`` genesis and silently
auto-generates a key — while ``opcert_principal`` still reads the process-global
``REBAR_OPCERT_ENV_ID`` and keeps claiming the deployed environment. The cert is minted under a key
nobody expects.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass

from _opcert_helpers import keypair


@dataclass(frozen=True)
class _Signer:
    """A minimal ``OpcertBinding``: a key path plus the principal to sign under."""

    key_path: str
    principal: str | None


@contextlib.contextmanager
def _tracker(tmp_path):
    """A bare tracker dir plus an ``.env-id``, enough for the key/principal resolvers."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("local-genesis-env\n", encoding="utf-8")
    yield str(tracker)


def _resolve_signing_identity(tracker: str) -> tuple[str, str]:
    """What a certified op running HERE would sign with: ``(key_path, principal)``."""
    from rebar._opcert_signing import ensure_opcert_key, opcert_principal

    return ensure_opcert_key(tracker), opcert_principal(tracker)


def _await(done: threading.Event) -> None:
    assert done.wait(30), "background daemon did not finish"


def test_async_gate_daemon_signs_under_the_bound_key(tmp_path, monkeypatch):
    """``review_plan_start``'s daemon must sign under the BOUND signer, not a genesis key.

    RED before the fix: the daemon sees no binding, so it resolves ``<tracker>/.opcert-key``
    (freshly generated) while still claiming the bound principal — the exact cert that failed
    ``ssh-keygen -Y verify`` on ticket e956-b1c3-45b9-4016.
    """
    import rebar.llm
    from rebar._mcp_inflight import GateJobHandle
    from rebar._mcp_llm import _spawn_gate_daemon
    from rebar._opcert_binding import bound_signer

    monkeypatch.setattr(rebar.llm, "record_gate_run", lambda record: None)
    deployed_key, _ = keypair(tmp_path, "deployed")
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "pinned-prod-env")
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)

    with _tracker(tmp_path) as tracker:
        seen: dict[str, tuple[str, str]] = {}
        done = threading.Event()

        def work():
            try:
                seen["identity"] = _resolve_signing_identity(tracker)
            finally:
                done.set()
            return {"verdict": "PASS"}

        with bound_signer(_Signer(deployed_key, "pinned-prod-env"), push_mode=None):
            _spawn_gate_daemon(GateJobHandle("job-ff4a", True), "plan_review", "t-1", work)
        _await(done)

        key_path, principal = seen["identity"]
        assert principal == "pinned-prod-env"
        assert key_path == deployed_key, (
            "the async gate daemon signed under a DIFFERENT key than the bound signer "
            f"({key_path!r} != {deployed_key!r}) while still claiming principal "
            f"{principal!r} — the cert cannot verify against that principal's pinned key"
        )


def _thread_spawn_names(tree) -> set[str]:
    """Every local name in ``tree`` that is bound to ``threading.Thread``, however imported.

    Resolves the aliases a rename could otherwise hide from the guard:
    ``import threading`` / ``import threading as th`` (→ attribute calls on that module name),
    and ``from threading import Thread`` / ``... as T`` (→ a bare call on that name).
    """
    import ast

    module_aliases: set[str] = set()
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "threading":
                    module_aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            for a in node.names:
                if a.name == "Thread":
                    direct_names.add(a.asname or a.name)
    return {f"{m}.Thread" for m in module_aliases} | direct_names


def test_no_mcp_tool_daemon_spawns_a_bare_thread():
    """Class guard (parity sibling). The ``run_workflow`` daemon (``rebar._mcp_writes``) is the
    same construct as the gate daemon — the async gate starter was written to "mirror
    run_workflow" — and loses the binding identically.

    A behavioural test cannot reach it: that daemon is a closure inside a nested FastMCP tool
    function with no importable seam. So the invariant is enforced structurally instead: an MCP
    tool that hands work to a background thread MUST route it through
    :func:`rebar._opcert_binding.spawn_context_daemon`, which carries the caller's
    ``contextvars.Context`` (and therefore the op-cert signer binding) into that thread. The one
    sanctioned exception is ``_mcp_serving``, which ENTERS ``bound_signer`` inside its own thread
    target and so needs no inherited context.

    The scan resolves IMPORT ALIASES rather than matching the literal name ``Thread``: this test
    is what stands between bug ff4a-2832-def4-4e55 and its recurrence, so
    ``from threading import Thread as T`` must not walk around it.
    """
    import ast
    import pathlib

    import rebar

    root = pathlib.Path(rebar.__file__).parent
    offenders = []
    for path in sorted(root.glob("_mcp_*.py")):
        if path.name == "_mcp_serving.py":  # binds inside its own thread target
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        spawn_names = _thread_spawn_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                base = getattr(func.value, "id", None)
                dotted = f"{base}.{func.attr}" if base else None
            else:
                dotted = getattr(func, "id", None)
            if dotted is not None and dotted in spawn_names:
                offenders.append(f"{path.name}:{node.lineno} ({dotted})")
    assert not offenders, (
        "MCP tool daemons must spawn through spawn_context_daemon so the op-cert signer "
        f"binding survives the thread boundary; thread spawn at: {offenders}"
    )


def test_the_thread_spawn_scan_sees_through_import_aliases():
    """The guard above is only worth having if a rename cannot evade it (review finding c)."""
    import ast

    aliased = ast.parse("from threading import Thread as T\nT(target=f, daemon=True)\n")
    assert _thread_spawn_names(aliased) == {"T"}
    module_aliased = ast.parse("import threading as th\nth.Thread(target=f)\n")
    assert _thread_spawn_names(module_aliased) == {"th.Thread"}
    plain = ast.parse("import threading\nthreading.Thread(target=f)\n")
    assert _thread_spawn_names(plain) == {"threading.Thread"}
    unrelated = ast.parse("from concurrent.futures import Thread\n")
    assert _thread_spawn_names(unrelated) == set()
