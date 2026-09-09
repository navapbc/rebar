"""Require the LLM stack to remain optional across library, CLI, and MCP.

Public operations stay lazy-imported and an exhaustive discovered matrix prevents omissions.
Without ``[agents]``, each surface follows its typed, non-billable degradation contract;
``review_code`` remains fail-safe. Offline tests always cover import cleanliness and exercise
missing-extra behavior only when the runtime is absent.
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


@pytest.fixture(autouse=True)
def _gate_source_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use ``source=local`` so code-less fixture repos reach missing-extra preflight.

    The suite's attested default cannot resolve ``HEAD`` here and would raise
    ``SnapshotRefError`` before optionality. Degradation signs nothing, so local reads are
    appropriate."""
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)


# The guard below discovers public operations and rejects omissions from this matrix.
OPERATIONS = ("review_code", "scan_epics_for_spec", "verify_completion")

# ``review_code`` is the fail-safe exception: missing runtime returns a valid
# INDETERMINATE result with ``coverage.llm_unavailable``, never PASS or a raise.
_FAIL_SAFE = frozenset({"review_code"})


# ── Import-cleanliness: every interface entrypoint imports lazily ──────────────
@pytest.mark.parametrize(
    "module",
    ["rebar", "rebar._cli", "rebar.mcp_server", "rebar.llm"],
)
def test_interface_import_pulls_no_agents_stack(module: str) -> None:
    """Import each interface cleanly and require the agents stack to stay unloaded."""
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
    """Without the extra, library operations raise typed ``LLMError``, never silent success."""
    from rebar.llm.errors import LLMError

    epic = _seed(rebar_repo)
    r = str(rebar_repo)
    calls = {
        "review_code": lambda: rebar.llm.review_code(
            diff_text="--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", repo_root=r
        ),
        "scan_epics_for_spec": lambda: rebar.llm.scan_epics_for_spec("the spec", repo_root=r),
        "verify_completion": lambda: rebar.llm.verify_completion(epic, repo_root=r),
    }
    import rebar.llm

    if op in _FAIL_SAFE:  # review_code: fail-safe — returns a valid result, never raises (WS4)
        result = calls[op]()
        assert isinstance(result, dict) and "findings" in result, result
        return
    with pytest.raises(LLMError) as exc:
        calls[op]()
    assert "agents" in str(exc.value).lower(), exc.value


@pytest.mark.skipif(_AGENTS, reason="the fail-safe path is the point WITHOUT the extra installed")
def test_review_code_is_fail_safe_without_extra(rebar_repo: Path) -> None:
    """Without the extra, ``review_code`` returns INDETERMINATE without raising or billing."""
    import rebar.llm

    result = rebar.llm.review_code(
        diff_text="--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", repo_root=str(rebar_repo)
    )
    assert isinstance(result, dict)
    assert result.get("findings") == []
    assert result["verdict"]["verdict"] == "INDETERMINATE"
    assert result["coverage"].get("llm_unavailable") is True


# ── CLI surface: each command degrades with Error: + exit 1 without the extra ──
@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
@pytest.mark.parametrize("op", OPERATIONS)
def test_cli_operation_degrades_without_extra(
    op: str, rebar_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Without the extra, CLI operations emit ``Error:`` and exit nonzero without traceback."""
    from rebar._cli import main

    epic = _seed(rebar_repo)
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\nrebar must do X.\n", encoding="utf-8")
    diff = tmp_path / "change.diff"
    diff.write_text("--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+y\n", encoding="utf-8")
    argv = {
        "review_code": ["review-code", "--diff-file", str(diff)],
        "scan_epics_for_spec": ["scan-spec", "--spec-file", str(spec)],
        "verify_completion": ["verify-completion", epic],
    }[op]

    rc = main(argv)
    captured = capsys.readouterr()
    err = captured.err
    if op in _FAIL_SAFE:  # review_code: degrades to INDETERMINATE (exit 2), never a traceback
        assert rc == 2, f"{op} without the extra is an INDETERMINATE degrade → exit 2, got {rc}"
        assert "Traceback" not in err, "fail-safe path must not surface a raw traceback"
        return
    assert rc == 1, f"{op} should exit 1 when the extra is absent"
    assert "Error:" in err and "agents" in err.lower(), err
    assert "Traceback" not in err, "degradation must not surface a raw traceback"


def test_cli_review_check_is_offline_and_truthful(
    capsys: pytest.CaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline ``review-plan --check`` reports availability without importing the stack."""
    import json

    from rebar._cli import main

    # Sandbox: `review-plan` is a real (mount-eligible) subcommand — from an unsandboxed cwd
    # the central mount (bug ad9f) would attach `.tickets-tracker` into the REAL repo
    # root and trip the leak guard.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rc = main(["review-plan", "--check"])
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
    """LLM tools register disabled by default, preventing accidental billable calls."""
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path

    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    srv = _build_mcp()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    epic = _seed(rebar_repo)
    gated = {
        "review_code": {},
        "scan_spec": {"spec_text": "the spec"},
        "verify_completion": {"ticket_id": epic},
    }
    for name, args in gated.items():
        assert name in tools, f"{name} not registered"
        with pytest.raises(Exception) as exc:
            _unwrap(asyncio.run(srv.call_tool(name, args)))
        # Prove it errored *because it is gated*, not for some unrelated reason.
        assert "disabled" in str(exc.value).lower(), str(exc.value)


@pytest.mark.skipif(_AGENTS, reason="agents extra installed → degradation path not exercised")
def test_mcp_operations_error_cleanly_when_gated_on_but_extra_absent(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With MCP gating enabled, missing runtime follows each tool's explicit contract.

    ``scan_spec`` raises transport-wrapped ``LLMError``; gate-shaped tools return
    structured degradation. Both identify ``[agents]`` without billing or silent success."""
    import asyncio

    from adapters import _unwrap

    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    srv = _build_mcp()
    epic = _seed(rebar_repo)
    # Supply a real range so ``review_code`` reaches runner preflight, not range resolution.
    _two_commits(rebar_repo)
    # Contract A raises typed ``LLMError``. Fail-safe ``review_code`` and structured
    # ``verify_completion`` follow Contract B instead.
    forced = {
        "scan_spec": {"spec_text": "the spec"},
    }
    for name, args in forced.items():
        with pytest.raises(Exception) as exc:
            _unwrap(asyncio.run(srv.call_tool(name, args)))
        # The gate is open; any error must therefore identify the missing extra.
        msg = str(exc.value).lower()
        assert "agents" in msg and "disabled" not in msg, str(exc.value)

    # Contract B returns structured degradation without raising or billing; its classifier
    # disposition lets the close gate fail closed.
    verdict = _unwrap(asyncio.run(srv.call_tool("verify_completion", {"ticket_id": epic})))
    assert isinstance(verdict, dict), verdict
    # Human detail belongs in ``message``; ``error`` is the shared vocabulary code.
    assert verdict.get("error") == "llm_unavailable", verdict
    msg = str(verdict.get("message", "")).lower()
    assert "agents" in msg and "disabled" not in msg, verdict
    assert verdict.get("resolution_class"), verdict  # classifier disposition present, not silent


# Discover runner-backed operations; reject omissions from this matrix.
def test_optionality_matrix_covers_every_public_operation() -> None:
    """Require each exported callable with a ``runner`` seam to appear in ``OPERATIONS``.

    Deterministic ``select_*`` helpers have no runner and are excluded."""
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
    # Use a childless epic so deterministic child closure passes and runner preflight is reached.
    # A child would fail before exercising missing-extra degradation.
    return rebar.create_ticket("epic", "Login epic", repo_root=str(repo))
