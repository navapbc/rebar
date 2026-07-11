"""Exhaustive LLM-optionality guard: the ``rebar.llm`` stack must be **optional**
for *every* interface (library / CLI / MCP) and *every* operation
(``review_ticket`` / ``review_code`` / ``scan_epics_for_spec``).

This is the single, deliberately-redundant contract test for the hard rule stated
in the ``rebar.llm`` epic: core rebar stays stdlib-only; the agent runtime
(pydantic-ai) / langfuse / anthropic stack is behind ``nava-rebar[agents]`` and
lazy-imported; and when the
extra is absent every surface **degrades cleanly** (a typed ``LLMError`` / a
``Error:`` + non-zero exit / a gated tool error) rather than crashing with an
``ImportError`` traceback or — worse — silently doing nothing.

Two halves, both runnable offline:
  * **Import-cleanliness** — importing any interface entrypoint must not pull the
    agents stack into ``sys.modules`` (proves the imports are lazy). Always runs.
  * **Graceful degradation** — when the extra is genuinely absent, each
    operation on each interface fails loudly and cleanly. These assertions are
    guarded on the extra's real absence (when it *is* installed, exercising the
    path needs live credentials, which an offline test must not do), mirroring
    the idiom in ``test_llm_framework.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar.llm import agents_extra_installed
from rebar.llm.config import _module_available

# The heavy stack that the [agents] extra ships. None of it may be imported by
# merely importing an interface entrypoint. (pydantic is intentionally NOT here:
# it arrives via FastMCP, a dependency of the MCP interface itself, not the agents
# extra — so it is allowed in `import rebar.mcp_server`.)
_AGENTS_STACK = (
    "pydantic_ai",
    "langfuse",
    "anthropic",
)

# Whether the agent runtime (pydantic_ai) is actually installed in THIS environment.
# When True we skip the "missing-extra" degradation assertions (they would need
# live credentials to exercise the path); import-cleanliness + gating still run.
_AGENTS = agents_extra_installed()

# The full operation matrix. The exhaustiveness test below asserts this stays in
# lock-step with the public ops exported by rebar.llm, so a newly-added operation
# cannot ship without an optionality entry here.
OPERATIONS = ("review_ticket", "review_code", "scan_epics_for_spec", "verify_completion")

# review_code is the ONE FAIL-SAFE op (epic b744 / WS4): the code-review gate is OFF by default
# (verify.enable_code_review) and, being a gated capability, its public `review_code` shim returns
# a VALID review_result rather than raising — inert-empty when disabled, degraded (INDETERMINATE +
# coverage.llm_unavailable) when enabled without the extra. So it degrades WITHOUT the extra by
# returning cleanly, NOT by raising/exit-1. The raise-degradation invariant below therefore EXEMPTS
# it; its real contract is pinned by test_review_code_is_fail_safe_without_extra (here) and by
# tests/unit/test_code_review_ws4.py. It stays in OPERATIONS so the exhaustiveness check holds.
_FAIL_SAFE = frozenset({"review_code"})


# ── Import-cleanliness: every interface entrypoint imports lazily ──────────────
@pytest.mark.parametrize(
    "module",
    ["rebar", "rebar._cli", "rebar.mcp_server", "rebar.llm"],
)
def test_interface_import_pulls_no_agents_stack(module: str) -> None:
    """Importing any interface entrypoint must not drag in the agents stack.

    Run in a clean subprocess so an already-imported module in this test process
    can't mask a non-lazy import."""
    code = (
        f"import sys, {module};"
        f"stack={_AGENTS_STACK!r};"
        "leaked=[m for m in stack if m in sys.modules];"
        "print('LEAK:' + ','.join(leaked) if leaked else 'CLEAN')"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0, f"{module} failed to import: {cp.stderr}"
    assert cp.stdout.strip() == "CLEAN", f"{module} leaked agents stack: {cp.stdout.strip()}"


# ── Library surface: each op degrades to a typed LLMError without the extra ────
@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
@pytest.mark.parametrize("op", OPERATIONS)
def test_library_operation_degrades_without_extra(op: str, rebar_repo: Path) -> None:
    """Calling a library op with the default (pydantic_ai) runner and no extra must
    raise a typed ``LLMError`` whose message points at the extra — never an
    ``ImportError``/``AttributeError`` traceback, and never a silent success."""
    from rebar.llm.errors import LLMError

    epic = _seed(rebar_repo)
    r = str(rebar_repo)
    calls = {
        "review_ticket": lambda: rebar.llm.review_ticket(epic, repo_root=r),
        "review_code": lambda: rebar.llm.review_code(
            diff_text="--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", repo_root=r
        ),
        "scan_epics_for_spec": lambda: rebar.llm.scan_epics_for_spec("the spec", repo_root=r),
        "verify_completion": lambda: rebar.llm.verify_completion(epic, repo_root=r),
    }
    import rebar.llm  # noqa: F401 — populate the rebar.llm attribute namespace

    if op in _FAIL_SAFE:  # review_code: fail-safe — returns a valid result, never raises (WS4)
        result = calls[op]()
        assert isinstance(result, dict) and "findings" in result, result
        return
    with pytest.raises(LLMError) as exc:
        calls[op]()
    assert "agents" in str(exc.value).lower(), exc.value


@pytest.mark.skipif(_AGENTS, reason="the fail-safe path is the point WITHOUT the extra installed")
def test_review_code_is_fail_safe_without_extra(rebar_repo: Path) -> None:
    """review_code is the off-by-default gated capability (WS4): without the agents extra AND
    disabled (the default), it returns a VALID empty review_result — no raise, no billable call —
    so a lean install never crashes on it. (Enabled-without-extra degrades to an INDETERMINATE
    result, also without raising; covered by tests/unit/test_code_review_ws4.py.)"""
    import rebar.llm

    result = rebar.llm.review_code(
        diff_text="--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", repo_root=str(rebar_repo)
    )
    assert isinstance(result, dict)
    assert result.get("findings") == []
    assert "disabled" in str(result.get("summary", "")).lower()


# ── CLI surface: each command degrades with Error: + exit 1 without the extra ──
@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
@pytest.mark.parametrize("op", OPERATIONS)
def test_cli_operation_degrades_without_extra(
    op: str, rebar_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Each ``rebar`` LLM subcommand must exit non-zero with a clean ``Error:``
    line (no Python traceback) when the extra is absent — automation that checks
    exit codes must not mistake a missing-extra run for a successful review."""
    from rebar._cli import main

    epic = _seed(rebar_repo)
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\nrebar must do X.\n", encoding="utf-8")
    diff = tmp_path / "change.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", encoding="utf-8")
    argv = {
        "review_ticket": ["review", epic, "ticket-quality"],
        "review_code": ["review-code", "--diff-file", str(diff)],
        "scan_epics_for_spec": ["scan-spec", "--spec-file", str(spec)],
        "verify_completion": ["verify-completion", epic],
    }[op]

    rc = main(argv)
    err = capsys.readouterr().err
    if op in _FAIL_SAFE:  # review_code: off by default → inert clean exit, no traceback (WS4)
        assert rc == 0, f"{op} is fail-safe (off by default) → exit 0, not a degradation error"
        assert "Traceback" not in err, "fail-safe path must not surface a raw traceback"
        return
    assert rc == 1, f"{op} should exit 1 when the extra is absent"
    assert "Error:" in err and "agents" in err.lower(), err
    assert "Traceback" not in err, "degradation must not surface a raw traceback"


def test_cli_review_check_is_offline_and_truthful(capsys: pytest.CaptureFixture) -> None:
    """``rebar review --check`` is the offline preflight: it never imports the
    stack, always exits 0, and reports the real availability of the extra."""
    import json

    from rebar._cli import main

    rc = main(["review", "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["pydantic_ai"] is _module_available("pydantic_ai")


# ── MCP surface: every op is gated off by default and degrades when forced ─────
def _build_mcp():
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    return build_server()


def test_mcp_operations_registered_and_gated_off_by_default(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three LLM tools are registered but DISABLED unless REBAR_MCP_ALLOW_LLM
    is set, so a default MCP client can never trigger a billable LLM call."""
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path

    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    srv = _build_mcp()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    epic = _seed(rebar_repo)
    gated = {
        "review_ticket": {"ticket_id": epic},
        "review_code": {},
        "scan_spec": {"spec_text": "the spec"},
        "verify_completion": {"ticket_id": epic},
    }
    for name, args in gated.items():
        assert name in tools, f"{name} not registered"
        with pytest.raises(Exception) as exc:  # noqa: B017 — FastMCP wraps the ValueError
            _unwrap(asyncio.run(srv.call_tool(name, args)))
        # Prove it errored *because it is gated*, not for some unrelated reason.
        assert "disabled" in str(exc.value).lower(), str(exc.value)


@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
def test_mcp_operations_error_cleanly_when_gated_on_but_extra_absent(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the gate explicitly opened, a missing extra must surface — never a
    billable call, never a silent empty result. The public tools split into two
    contracts (story authorial-hated-blackbear): review_ticket/scan_spec RAISE the
    typed LLMError (transport-wrapped); the gate-shaped tools (review_code,
    verify_completion, review_plan) instead RETURN a STRUCTURED degrade dict so the
    driving agent/close-gate can branch on it. Both surface the missing 'agents' extra;
    neither is silent."""
    import asyncio

    from adapters import _unwrap

    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    srv = _build_mcp()
    epic = _seed(rebar_repo)
    # Give review_code a real HEAD~1..HEAD range so it reaches the runner preflight
    # (rather than failing earlier at git range resolution) — proving review_code,
    # too, degrades on the missing extra rather than for an unrelated reason.
    _two_commits(rebar_repo)
    # ── Contract A: tools that RAISE the typed LLMError on a missing extra. ──
    # review_code EXCLUDED: fail-safe (WS4) — returns an inert disabled result, see
    # test_review_code_is_fail_safe_without_extra. verify_completion EXCLUDED: it is a
    # gate-shaped tool that returns a STRUCTURED degrade dict (Contract B, below), not a raise.
    forced = {
        "review_ticket": {"ticket_id": epic},
        "scan_spec": {"spec_text": "the spec"},
    }
    for name, args in forced.items():
        with pytest.raises(Exception) as exc:  # noqa: B017
            _unwrap(asyncio.run(srv.call_tool(name, args)))
        # Prove it degraded *because the extra is absent*, not because it is gated
        # (the gate is open here) or for some unrelated reason.
        msg = str(exc.value).lower()
        assert "agents" in msg and "disabled" not in msg, str(exc.value)

    # ── Contract B: verify_completion RETURNS a structured degrade dict (never raises,
    # never a billable call, never a silent empty result). It carries the classifier
    # disposition so the close gate can fail-closed on it programmatically. ──
    verdict = _unwrap(asyncio.run(srv.call_tool("verify_completion", {"ticket_id": epic})))
    assert isinstance(verdict, dict), verdict
    err = str(verdict.get("error", "")).lower()
    assert "agents" in err and "disabled" not in err, verdict
    assert verdict.get("resolution_class"), verdict  # classifier disposition present, not silent


# ── Guard: the matrix above must enumerate every public LLM operation ──────────
def test_optionality_matrix_covers_every_public_operation() -> None:
    """If a new runner-backed LLM operation is added without a matching entry in
    OPERATIONS, this fails — forcing optionality coverage to track the public
    surface rather than silently lagging it.

    Operations are DISCOVERED, not restated: an "operation" is a callable exported
    by one of the operation modules that takes a ``runner`` injection seam (the
    thing that makes it an LLM op). The deterministic ``select_*`` helpers, which
    have no ``runner`` parameter, are correctly excluded."""
    import inspect

    from rebar.llm import code_review, completion, operations, spec_scan

    discovered = set()
    for mod in (operations, code_review, spec_scan, completion):
        for name in getattr(mod, "__all__", []):
            obj = getattr(mod, name)
            if callable(obj) and "runner" in inspect.signature(obj).parameters:
                discovered.add(name)
    assert discovered == set(OPERATIONS), (
        "OPERATIONS is out of sync with the discovered runner-backed operations: "
        f"discovered={sorted(discovered)} matrix={sorted(OPERATIONS)}"
    )


@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
def test_scan_spec_degrades_without_extra_even_with_zero_epics(
    rebar_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Regression guard: a spec-scan over an EMPTY store (zero epics) must still
    surface the missing extra. The batch loop never runs with no epics, so without
    an up-front runner preflight an unusable runner would masquerade as a clean
    empty result — the forbidden silent success."""
    from rebar._cli import main
    from rebar.llm.errors import LLMError

    # NOTE: deliberately do NOT seed any epic — the store is empty.
    r = str(rebar_repo)
    import rebar.llm

    with pytest.raises(LLMError) as exc:
        rebar.llm.scan_epics_for_spec("the spec", repo_root=r)
    assert "agents" in str(exc.value).lower(), exc.value

    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\nrebar must do X.\n", encoding="utf-8")
    rc = main(["scan-spec", "--spec-file", str(spec)])
    err = capsys.readouterr().err
    assert rc == 1 and "agents" in err.lower(), err


# ── local helpers ─────────────────────────────────────────────────────────────
def _two_commits(repo: Path) -> None:
    """Make HEAD~1..HEAD resolvable with a real change on the repo's work branch."""
    f = repo / "sample.txt"
    f.write_text("one\n", encoding="utf-8")
    _git("add", "sample.txt", cwd=repo)
    _git("commit", "-q", "-m", "c1", cwd=repo)
    f.write_text("one\ntwo\n", encoding="utf-8")
    _git("add", "sample.txt", cwd=repo)
    _git("commit", "-q", "-m", "c2", cwd=repo)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# ── local seed helper (mirrors test_llm_framework._seed) ──────────────────────
def _seed(repo: Path) -> str:
    # A CHILDLESS epic on purpose. verify_completion runs a deterministic
    # child-closure gate BEFORE the LLM: an epic with an open/unsigned child returns
    # a FAIL verdict without ever reaching the runner — so it would NOT exercise the
    # missing-extra degradation path these tests assert on. A childless ticket passes
    # the gate and reaches the runner preflight (which raises the typed "install the
    # agents extra" error when the stack is absent). The epic-with-children
    # deterministic path is covered in test_completion_verifier.py.
    return rebar.create_ticket("epic", "Login epic", repo_root=str(repo))
