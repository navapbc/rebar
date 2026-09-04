"""MCP-server-specific behaviors (FastMCP).

Covers the read-only gate, removed reconcile tool, and the lazy-import error
when the optional `mcp` extra is absent. Skipped wholesale if `mcp` is not
installed.
"""

from __future__ import annotations

import builtins

import pytest

pytest.importorskip("mcp")

import asyncio
import importlib.util

from mcp.server.fastmcp.exceptions import ToolError

import rebar
import rebar.llm
from rebar.mcp_server import build_server


def _tool_names(srv) -> set[str]:
    return {t.name for t in asyncio.run(srv.list_tools())}


def test_readonly_hides_write_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    names = _tool_names(build_server())
    # Reads remain; writes are gone.
    assert "show_ticket" in names and "list_tickets" in names
    for write_tool in (
        "create_ticket",
        "transition_ticket",
        "tag_ticket",
        "archive_ticket",
        "claim_ticket",
        "reopen_ticket",
        "set_file_impact",
        "set_verify_commands",
        "bridge_sync",
        "bridge_pause",
        "bridge_resume",
    ):
        assert write_tool not in names, write_tool
    # WS5d: quality-gate + file-impact READ tools stay exposed in readonly mode.
    for read_tool in (
        "clarity_check",
        "check_ac",
        "quality_check",
        "validate",
        "get_file_impact",
        "get_verify_commands",
    ):
        assert read_tool in names, read_tool


def test_write_tools_present_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    names = _tool_names(build_server())
    assert {
        "create_ticket",
        "transition_ticket",
        "claim_ticket",
        "reopen_ticket",
        "set_file_impact",
        "set_verify_commands",
    } <= names


@pytest.mark.parametrize(
    ("val", "expect_readonly"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("yes", True),
        ("YES", True),
        ("Yes", True),
        (" true ", True),
        ("", False),
        ("0", False),
        ("no", False),
        ("false", False),
    ],
)
def test_readonly_truthy_parse_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, val, expect_readonly
) -> None:
    """Bug ship-mogul-glob: common truthy spellings (TRUE/Yes/…, whitespace
    tolerated) must enable the readonly gate — the parse must not be
    case-sensitive (TRUE previously failed OPEN, the dangerous direction)."""
    monkeypatch.setenv("REBAR_MCP_READONLY", val)
    write_present = "create_ticket" in _tool_names(build_server())
    if expect_readonly:
        assert not write_present, f"{val!r} must enable readonly (write tools hidden)"
    else:
        assert write_present, f"{val!r} must NOT enable readonly (write tools present)"


def test_mcp_reconcile_tool_is_removed_before_library_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy MCP reconcile(mode=...) tool is absent, so stale callers fail
    at tool dispatch before any library or reconciler work can begin."""
    calls: list[str] = []
    monkeypatch.setattr(
        rebar,
        "reconcile",
        lambda mode="dry-run": calls.append(mode) or {"mode": mode},
        raising=False,
    )
    srv = build_server()
    assert "reconcile" not in _tool_names(srv)
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": "live"}))
    assert "reconcile" in str(exc.value).lower()
    assert calls == []


# ── fsck recover-gate (BUG f6f6) ────────────────────────────────────────────────
def test_fsck_recover_blocked_under_readonly(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    """fsck(recover=True) is a write op and must be refused under readonly."""
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("fsck", {"recover": True}))
    msg = str(exc.value).lower()
    assert "read-only" in msg and "recover" in msg


def test_fsck_recover_allowed_when_writable(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    """Non-readonly server still runs the recovery path."""
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    srv = build_server()
    asyncio.run(srv.call_tool("fsck", {"recover": True}))  # no raise


def test_plain_fsck_available_in_both_modes(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    """Plain fsck() (no recovery) works readonly and writable alike."""
    for readonly in ("1", ""):
        monkeypatch.setenv("REBAR_MCP_READONLY", readonly) if readonly else monkeypatch.delenv(
            "REBAR_MCP_READONLY", raising=False
        )
        srv = build_server()
        asyncio.run(srv.call_tool("fsck", {}))  # no raise


def _make_stale_index_lock(repo):
    """Create a stale (>5min) .git/index.lock in the repo's tracker; return its path."""
    import os
    import time
    from pathlib import Path

    tracker = Path(repo) / ".tickets-tracker"
    gd = tracker / ".git"
    gitdir = Path(gd.read_text().split("gitdir:", 1)[1].strip()) if gd.is_file() else gd
    lock = gitdir / "index.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    return lock


def test_plain_fsck_does_not_remove_lock_under_readonly(
    monkeypatch: pytest.MonkeyPatch, rebar_repo
) -> None:
    """Bug terse-frost-ale (sibling of f6f6): plain fsck() removes a stale
    .git/index.lock — a git-state write. A read-only server must report it, not
    remove it."""
    lock = _make_stale_index_lock(rebar_repo)
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    srv = build_server()
    res = asyncio.run(srv.call_tool("fsck", {}))
    assert lock.exists(), "read-only fsck() must NOT remove the stale index.lock"
    assert "not removed (read-only)" in str(res)


def test_plain_fsck_removes_lock_when_writable(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    """Control: a writable server still cleans the stale lock."""
    lock = _make_stale_index_lock(rebar_repo)
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    srv = build_server()
    asyncio.run(srv.call_tool("fsck", {}))
    assert not lock.exists(), "writable fsck() should remove the stale index.lock"


# ── clarity_check missing-ticket schema-conformance over MCP (BUG ef5f) ─────────
def test_clarity_check_missing_ticket_mcp_clean(
    monkeypatch: pytest.MonkeyPatch, rebar_repo
) -> None:
    """clarity_check on a nonexistent id returns a clean, schema-shaped payload
    over MCP (no pydantic ValidationError / ToolError)."""
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    srv = build_server()
    for tool in ("clarity_check", "check_ac", "quality_check"):
        asyncio.run(srv.call_tool(tool, {"ticket_id": "no-such-ticket-xyz"}))  # no raise


# ── doc-conformance: all six relations documented (BUG b7af) ────────────────────
def test_mcp_link_docstring_lists_all_relations(rebar_repo) -> None:
    """The MCP link_tickets docstring must mention all six canonical relations
    (sourced from the engine's CANONICAL_RELATIONS — single source of truth)."""
    import rebar  # noqa: F401
    from rebar.graph._links import CANONICAL_RELATIONS

    srv = build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    doc = tools["link_tickets"].description or ""
    for rel in CANONICAL_RELATIONS:
        assert rel in doc, f"MCP link doc missing relation {rel!r}"


def test_mcp_module_docstring_describes_inprocess_reads() -> None:
    """The module docstring must not claim reads use subprocess wrappers."""
    import rebar.mcp_server as m

    doc = (m.__doc__ or "").lower()
    assert "in-process" in doc
    assert "subprocess wrapper" not in doc


# ── optimistic-concurrency error IDENTITY over MCP (parity with CLI exit-10 and
#    rebar.ConcurrencyError) ──────────────────────────────────────────────────
# A state-dependent write that fails optimistic concurrency must surface the ONE
# shared structured identity across all three interfaces: the engine's exit code
# 10, raised by the library as ``rebar.ConcurrencyError``. FastMCP wraps the
# tool's exception in a ToolError but chains the original via ``__cause__`` — and
# the write tools call the library directly, so that cause IS a ConcurrencyError
# (returncode 10). These tests assert on that TYPED, chained identity (class +
# code), not on the wrapper's prose message, and verify it is DISTINCT from a
# non-concurrency failure (e.g. not-found → returncode 1) so a wrong-reason
# rejection fails the test.
CONCURRENCY_CODE = 10


def _concurrency_cause(srv, tool: str, args: dict) -> BaseException:
    """Run an MCP tool expected to reject, returning the chained root cause.

    8a31: MCP failures now carry a structured ``error_envelope`` on an
    ``McpEnvelopeError`` (the same machine identity the CLI emits) — here the
    ``concurrency_conflict`` code with the shared engine exit code (10). The original
    ``rebar.ConcurrencyError`` is preserved one level deeper on ``__cause__``."""
    from rebar._mcp_errors import McpEnvelopeError

    with pytest.raises(Exception) as exc:  # FastMCP raises mcp ...ToolError
        asyncio.run(srv.call_tool(tool, args))
    cause = exc.value.__cause__ or exc.value
    assert isinstance(cause, McpEnvelopeError), (
        f"expected McpEnvelopeError cause, got {type(cause).__name__}: {cause!r}"
    )
    assert cause.envelope["error"] == "concurrency_conflict"
    assert cause.envelope["exit_code"] == CONCURRENCY_CODE
    engine = cause.__cause__
    assert isinstance(engine, rebar.ConcurrencyError), (
        f"expected ConcurrencyError chained, got {type(engine).__name__}: {engine!r}"
    )
    assert getattr(engine, "returncode", None) == CONCURRENCY_CODE
    return engine


def test_transition_stale_current_mcp_concurrency_error(rebar_repo) -> None:
    """transition_ticket with a STALE expected current_status surfaces the shared
    ConcurrencyError identity (returncode 10) over MCP (the ticket is unchanged)."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Stale guard", repo_root=str(rebar_repo))
    # Ticket is open; declare a valid-but-wrong current → optimistic mismatch.
    _concurrency_cause(
        srv,
        "transition_ticket",
        {"ticket_id": tid, "current_status": "in_progress", "target_status": "closed"},
    )
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_claim_already_claimed_mcp_concurrency_error(rebar_repo) -> None:
    """claim_ticket on an already-claimed ticket surfaces the shared
    ConcurrencyError identity (returncode 10) over MCP; assignee is preserved."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Already claimed", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    _concurrency_cause(srv, "claim_ticket", {"ticket_id": tid, "assignee": "bob"})
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "in_progress" and state.get("assignee") == "alice"


def test_reopen_non_closed_mcp_concurrency_error(rebar_repo) -> None:
    """reopen_ticket on a NON-closed ticket surfaces the shared ConcurrencyError
    identity (returncode 10) over MCP (parity with CLI exit-10 and library)."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Reopen guard", repo_root=str(rebar_repo))
    # Ticket is open, not closed → reopen (closed→open) is a state mismatch.
    _concurrency_cause(srv, "reopen_ticket", {"ticket_id": tid})
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_mcp_rejection_identity_distinguishes_not_found(rebar_repo) -> None:
    """A NON-concurrency failure (not-found) must NOT carry the shared concurrency
    identity — proving the typed identity discriminates the rejection REASON, not
    just rejection presence. The chained cause is a plain RebarError (exit 1),
    never a ConcurrencyError (exit 10)."""
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(
            srv.call_tool(
                "transition_ticket",
                {
                    "ticket_id": "ffff-ffff-ffff-ffff",
                    "current_status": "open",
                    "target_status": "in_progress",
                },
            )
        )
    cause = exc.value.__cause__ or exc.value
    assert not isinstance(cause, rebar.ConcurrencyError)
    assert getattr(cause, "returncode", None) != CONCURRENCY_CODE


def test_absent_mcp_extra_raises_systemexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the optional `mcp` extra is missing, build_server() exits with a clear
    install hint (SystemExit), not an opaque ImportError."""
    import rebar.mcp_server as m

    real_import = builtins.__import__

    def _fail_mcp(name, *a, **k):
        if name == "mcp.server.fastmcp" or name.startswith("mcp.server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fail_mcp)
    with pytest.raises(SystemExit) as exc:
        m.build_server()
    assert "mcp" in str(exc.value).lower()


def test_absent_pydantic_declares_every_mcp_model_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A core-only wheel must import mcp_server far enough to show its install hint."""
    import rebar._mcp_models as installed_models

    spec = importlib.util.spec_from_file_location(
        "rebar_mcp_models_without_pydantic", installed_models.__file__
    )
    assert spec is not None and spec.loader is not None
    fallback_models = importlib.util.module_from_spec(spec)
    real_import = builtins.__import__

    def _fail_pydantic(name, *args, **kwargs):
        if name == "pydantic" or name.startswith("pydantic."):
            raise ImportError("No module named 'pydantic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_pydantic)
    spec.loader.exec_module(fallback_models)

    for name in (
        "BridgeRunOut",
        "BridgeStatusOut",
        "BridgeControlOut",
        "BridgeAccessStepOut",
        "BridgeAccessCheckOut",
    ):
        assert getattr(fallback_models, name) is None


# ── bug-close over MCP requires a bounded close_class (ticket 376d) ──────────
# transition_core enforces a bounded --class on every *->closed write for a bug
# (ticket ed13). The MCP transition_ticket tool must expose an optional
# ``close_class`` and thread it to rebar.transition, else agents over MCP cannot
# close bug tickets at all.
def test_mcp_transition_closes_bug_with_close_class(rebar_repo) -> None:
    """transition_ticket must close a BUG when handed a valid close_class, and
    the classification must be recorded on the ticket (observable outcome)."""
    srv = build_server()
    tid = rebar.create_ticket(
        "bug", "MCP bug close", description="x" * 60, repo_root=str(rebar_repo)
    )
    asyncio.run(
        srv.call_tool(
            "transition_ticket",
            {
                "ticket_id": tid,
                "current_status": "open",
                "target_status": "closed",
                "close_class": "regression",
            },
        )
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "closed"
    assert state.get("close_class") == "regression"


# ── reason threads through MCP to become close_reason (ticket e1a2) ──────────
# The write-parity oracle classifies the MCP surface by INTROSPECTING
# transition_ticket's signature, so merely declaring ``reason`` would flip its
# strict-xfail to xpass while the body silently discarded the datum (the
# defiant-orthoclase-buck failure mode). This test asserts the datum ARRIVES:
# the reason must be persisted as ``close_reason`` on the reduced state.
def test_mcp_transition_persists_close_reason_on_reason_required_class(rebar_repo) -> None:
    """transition_ticket must forward ``reason`` to rebar.transition so a
    reason-required administrative close records its close_reason."""
    srv = build_server()
    tid = rebar.create_ticket(
        "task", "MCP obsolete close", description="x" * 60, repo_root=str(rebar_repo)
    )
    asyncio.run(
        srv.call_tool(
            "transition_ticket",
            {
                "ticket_id": tid,
                "current_status": "open",
                "target_status": "closed",
                "close_class": "obsolete",
                "reason": "superseded by the consolidated surface",
            },
        )
    )
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "closed"
    assert state.get("close_reason") == "superseded by the consolidated surface", (
        "reason was accepted by the MCP tool but never reached the close disposition "
        "— the parameter exists, the datum did not arrive"
    )


def test_mcp_transition_records_explicit_caused_by_edge(rebar_repo) -> None:
    culprit = rebar.create_ticket("task", "culprit", repo_root=str(rebar_repo))
    bug = rebar.create_ticket("bug", "attributed bug", repo_root=str(rebar_repo))
    asyncio.run(
        build_server().call_tool(
            "transition_ticket",
            {
                "ticket_id": bug,
                "current_status": "open",
                "target_status": "closed",
                "close_class": "preexisting",
                "caused_by": culprit,
            },
        )
    )
    deps = rebar.show_ticket(bug, repo_root=str(rebar_repo))["deps"]
    assert any(dep["relation"] == "caused_by" and dep["target_id"] == culprit for dep in deps)


def test_mcp_transition_ref_targets_and_signs_requested_commit(rebar_repo, monkeypatch) -> None:
    from interfaces.lifecycle.test_close_ref_target_80af import (
        _enable_completion_gate,
        _make_stack,
        _stub_verifier,
    )

    _enable_completion_gate(rebar_repo)
    story, story_sha, head_sha = _make_stack(rebar_repo)
    assert story_sha != head_sha
    calls: list[dict] = []
    _stub_verifier(monkeypatch, calls)

    asyncio.run(
        build_server().call_tool(
            "transition_ticket",
            {
                "ticket_id": story,
                "current_status": "in_progress",
                "target_status": "closed",
                "ref": story_sha,
            },
        )
    )
    assert calls and calls[-1]["ref"] == story_sha
    signature = rebar.verify_signature(story, kind="completion-verifier", repo_root=str(rebar_repo))
    assert signature["verdict"] == "certified"
    assert signature["verified_at_sha"] == story_sha


@pytest.mark.parametrize(
    ("force", "forced", "audit_reason"),
    [
        (None, False, None),
        ("operator bypass", True, "operator bypass"),
        ("", True, "(no reason given)"),
    ],
)
def test_mcp_start_work_force_matches_claim_semantics(
    rebar_repo, force: str | None, forced: bool, audit_reason: str | None
) -> None:
    (rebar_repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")
    server = build_server()
    transition_ticket = rebar.create_ticket("task", "transition force", repo_root=str(rebar_repo))
    claim_ticket = rebar.create_ticket("task", "claim force", repo_root=str(rebar_repo))

    def transition_call():
        return server.call_tool(
            "transition_ticket",
            {
                "ticket_id": transition_ticket,
                "current_status": "open",
                "target_status": "in_progress",
                "force": force,
            },
        )

    def claim_call():
        return server.call_tool(
            "claim_ticket",
            {"ticket_id": claim_ticket, "force": force},
        )

    if forced:
        asyncio.run(transition_call())
        asyncio.run(claim_call())
    else:
        with pytest.raises(ToolError):
            asyncio.run(transition_call())
        with pytest.raises(ToolError):
            asyncio.run(claim_call())

    transition_state = rebar.show_ticket(transition_ticket, repo_root=str(rebar_repo))
    claim_state = rebar.show_ticket(claim_ticket, repo_root=str(rebar_repo))
    expected_status = "in_progress" if forced else "open"
    assert transition_state["status"] == claim_state["status"] == expected_status
    transition_comments = " ".join(c["body"] for c in transition_state["comments"])
    claim_comments = " ".join(c["body"] for c in claim_state["comments"])
    if audit_reason is None:
        assert "FORCE_CLAIM" not in transition_comments
        assert "FORCE_CLAIM" not in claim_comments
    else:
        assert "FORCE_CLAIM" in transition_comments and audit_reason in transition_comments
        assert "FORCE_CLAIM" in claim_comments and audit_reason in claim_comments


@pytest.mark.parametrize(
    ("force", "forced", "audit_reason"),
    [
        (None, False, None),
        ("completion override", True, "completion override"),
        ("", True, "(no reason given)"),
    ],
)
def test_mcp_transition_force_drives_completion_bypass(
    rebar_repo,
    monkeypatch: pytest.MonkeyPatch,
    force: str | None,
    forced: bool,
    audit_reason: str | None,
) -> None:
    verifier_calls: list[str] = []

    def fail_verification(ticket_id: str, **_kwargs):
        verifier_calls.append(ticket_id)
        return {
            "verdict": "FAIL",
            "runner": "fake",
            "model": "fake",
            "findings": [
                {
                    "criterion": "AC1",
                    "detail": "missing",
                    "severity": "high",
                    "dimension": "completion",
                }
            ],
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", fail_verification)
    ticket = rebar.create_ticket("task", "completion force", repo_root=str(rebar_repo))
    rebar.claim(ticket, repo_root=str(rebar_repo))
    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_completion_verification_for_close = true\n"
    )
    call = build_server().call_tool(
        "transition_ticket",
        {
            "ticket_id": ticket,
            "current_status": "in_progress",
            "target_status": "closed",
            "force": force,
        },
    )
    if forced:
        asyncio.run(call)
    else:
        with pytest.raises(ToolError):
            asyncio.run(call)

    state = rebar.show_ticket(ticket, repo_root=str(rebar_repo))
    assert state["status"] == ("closed" if forced else "in_progress")
    assert verifier_calls == ([] if forced else [ticket])
    audit = " ".join(c["body"] for c in state["comments"])
    if audit_reason is None:
        assert "FORCE_CLOSE" not in audit
    else:
        assert "FORCE_CLOSE" in audit and audit_reason in audit
