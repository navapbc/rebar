"""Bug ff4a-2832-def4-4e55 — a PASSED plan-review persisted an unverifiable attestation.

Two independent defects, one RED test each.

**(1) The async gate surface loses the op-cert signer binding.** ``_spawn_gate_daemon``
(``rebar._mcp_llm``) and the ``run_workflow`` daemon (``rebar._mcp_writes``) run their work on a
bare ``threading.Thread``, which starts with a FRESH ``contextvars.Context``. The signer bound by
``bound_signer()`` in the uvicorn serving thread is therefore invisible inside them, so
``ensure_opcert_key`` falls through to the ``<tracker>/.opcert-key`` genesis and silently
auto-generates a key — while ``opcert_principal`` still reads the process-global
``REBAR_OPCERT_ENV_ID`` and keeps claiming the pinned environment. The cert is minted under a key
nobody trusts.

**(2) Nothing says so at sign time.** ``mint_opcert_record`` will happily mint a cert whose claimed
principal IS pinned in ``.rebar/trusted_environments.yaml`` but whose signing key is NOT one of
that principal's pinned keys — an attestation that cannot verify anywhere but the signing box. The
failure surfaced hours later, at close.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass

import pytest

from tests.unit._opcert_helpers import keypair


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


def test_no_mcp_tool_daemon_spawns_a_bare_thread(tmp_path):
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
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "Thread":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "MCP tool daemons must spawn through spawn_context_daemon so the op-cert signer "
        f"binding survives the thread boundary; bare threading.Thread at: {offenders}"
    )


def _pin(repo, env_id: str, public_key: str) -> None:
    rebar_dir = repo / ".rebar"
    rebar_dir.mkdir(parents=True, exist_ok=True)
    (rebar_dir / "trusted_environments.yaml").write_text(
        "environments:\n"
        f'  - env_id: "{env_id}"\n'
        "    keys:\n"
        f'      - public_key: "{public_key}"\n'
        '        added_at_log_position: "1-a"\n'
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def test_mint_refuses_a_cert_whose_key_is_not_pinned_for_its_claimed_principal(
    tmp_path, monkeypatch
):
    """Sign time, not close time: minting must REFUSE when the claimed principal is pinned but
    the signing key is not one of its pinned keys.

    That cert can only ever verify on the box that made it. Today the mint reports success and
    the failure surfaces hours later, at the close gate, as
    ``ssh-keygen -Y verify rejected signature (exit 255)``.
    """
    import subprocess

    import rebar
    from rebar._opcert_signing import OpcertKeyUnavailable, mint_opcert_record

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    # Someone ELSE's key is what the project pinned for this principal.
    _, pinned_public = keypair(tmp_path, "the-real-prod-key")
    _pin(repo, "pinned-prod-env", pinned_public)

    # This process claims that principal but has no bound signer and no key override, so it
    # will sign under a freshly generated <tracker>/.opcert-key.
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "pinned-prod-env")
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)

    with pytest.raises(OpcertKeyUnavailable) as excinfo:
        mint_opcert_record(
            "t-ff4a",
            ["plan-review: PASS", "ticket: t-ff4a", "material: deadbeef"],
            kind="plan-review",
            repo_root=str(repo),
        )
    assert "pinned-prod-env" in str(excinfo.value)


def test_mint_still_succeeds_for_an_unpinned_principal(tmp_path, monkeypatch):
    """The guard must not disturb the developer-local path: an UNPINNED principal (every
    ordinary dev box) signs under its genesis key exactly as before."""
    import subprocess

    import rebar
    from rebar._opcert_signing import mint_opcert_record

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    _, other_public = keypair(tmp_path, "someone-else")
    _pin(repo, "some-other-env", other_public)
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "an-unpinned-dev-box")
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)

    record = mint_opcert_record(
        "t-ff4a",
        ["plan-review: PASS", "ticket: t-ff4a", "material: deadbeef"],
        kind="plan-review",
        repo_root=str(repo),
    )
    assert record["principal"] == "an-unpinned-dev-box"
    assert record["algorithm"] == "sshsig"
