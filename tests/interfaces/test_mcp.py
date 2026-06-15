"""MCP-server-specific behaviors (FastMCP).

Covers the read-only gate, the live-reconcile gate, and the lazy-import error
when the optional `mcp` extra is absent. Skipped wholesale if `mcp` is not
installed.
"""

from __future__ import annotations

import builtins

import pytest

pytest.importorskip("mcp")

import asyncio

import rebar
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
    "val,expect_readonly",
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


def test_live_reconcile_refused_without_optin(monkeypatch: pytest.MonkeyPatch, rebar_repo) -> None:
    monkeypatch.delenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", raising=False)
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": "live"}))
    assert "live reconcile is disabled" in str(exc.value).lower()


# ── reconcile mode-gate matrix (BUG 9d7c) ──────────────────────────────────────
# cap-0 modes are non-mutating and always allowed; the rest mutate Jira and must
# be gated by both readonly and the live-opt-in env. A fake acli on PATH fails
# loudly if reconcile ever shells out — proving the gate refuses BEFORE any
# Jira-touching work for the cases that must be refused.
_CAP0_MODES = ["reconcile-check", "dry-run"]
_MUTATING_MODES = ["bootstrap-strict", "bootstrap-throttle", "live"]


@pytest.fixture
def _loud_acli(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Put a fake `acli` (and python3 shim is untouched) on PATH that errors if
    executed, so an ungated mutating reconcile would crash visibly."""
    import os
    import stat

    bindir = tmp_path / "loud-bin"
    bindir.mkdir()
    acli = bindir / "acli"
    acli.write_text("#!/bin/sh\necho 'FAKE ACLI INVOKED' >&2\nexit 99\n")
    acli.chmod(acli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return bindir


@pytest.mark.parametrize("mode", _CAP0_MODES)
def test_reconcile_cap0_modes_allowed_in_both_gates(
    monkeypatch: pytest.MonkeyPatch, rebar_repo, mode, _loud_acli
) -> None:
    """Non-mutating (cap-0) modes run regardless of readonly/opt-in — the gate
    must not refuse them. (They may legitimately fail for lack of Jira creds; we
    only assert they are NOT refused by the gate.)"""
    for readonly in ("", "1"):
        monkeypatch.setenv("REBAR_MCP_READONLY", readonly) if readonly else monkeypatch.delenv(
            "REBAR_MCP_READONLY", raising=False
        )
        monkeypatch.delenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", raising=False)
        srv = build_server()
        try:
            asyncio.run(srv.call_tool("reconcile", {"mode": mode}))
        except Exception as exc:  # noqa: BLE001
            assert "disabled" not in str(exc).lower(), (mode, readonly, exc)


@pytest.fixture
def _empty_acli(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Put a fake `acli` on PATH that returns an EMPTY but valid issue list for
    any search (`[]`). This lets a dry-run reconcile complete a real pass —
    fetch → diff → (no-write) report — without touching real Jira, so the test
    can assert the no-write contract end-to-end."""
    import os
    import stat

    bindir = tmp_path / "empty-bin"
    bindir.mkdir()
    acli = bindir / "acli"
    acli.write_text("#!/bin/sh\necho '[]'\nexit 0\n")
    acli.chmod(acli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    # Ensure no JIRA_* creds leak in → parent/comment REST enrichment degrades
    # to {} (it never writes), keeping the pass fully offline.
    for var in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return bindir


def _store_write_tree(repo) -> set[str]:
    """Set of files under the reconciler's store-write locations."""
    import pathlib

    roots = [
        pathlib.Path(repo) / "bridge_state",
        pathlib.Path(repo) / ".tickets-tracker" / ".bridge_state",
    ]
    out: set[str] = set()
    for r in roots:
        if r.exists():
            out |= {str(p.relative_to(repo)) for p in r.rglob("*") if p.is_file()}
    return out


def test_readonly_dry_run_reconcile_performs_zero_store_writes(
    monkeypatch: pytest.MonkeyPatch, rebar_repo, _empty_acli
) -> None:
    """A REBAR_MCP_READONLY=1 server running reconcile(mode='dry-run') must
    perform ZERO local-store writes — no snapshot / manifest / health /
    sync-log / bindings / prev_snapshot file (ticket yaw-plait-doe). The
    no-write contract holds universally, not only because of the readonly gate
    (cap-0 dry-run is allowed under readonly), so the differ runs but nothing
    is persisted."""
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    monkeypatch.delenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", raising=False)
    srv = build_server()

    before = _store_write_tree(rebar_repo)
    result = asyncio.run(srv.call_tool("reconcile", {"mode": "dry-run"}))
    after = _store_write_tree(rebar_repo)

    new_files = sorted(after - before)
    assert new_files == [], (
        f"readonly dry-run reconcile must write NOTHING to the store, "
        f"but created: {new_files} (result={result})"
    )


@pytest.mark.parametrize("mode", _MUTATING_MODES)
def test_reconcile_mutating_refused_under_readonly(
    monkeypatch: pytest.MonkeyPatch, rebar_repo, mode, _loud_acli
) -> None:
    """Readonly blocks ALL mutating modes — even with the live opt-in set."""
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    monkeypatch.setenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", "1")
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": mode}))
    msg = str(exc.value).lower()
    assert "disabled" in msg and "read-only" in msg, (mode, exc.value)


@pytest.mark.parametrize("mode", _MUTATING_MODES)
def test_reconcile_mutating_refused_without_optin(
    monkeypatch: pytest.MonkeyPatch, rebar_repo, mode, _loud_acli
) -> None:
    """Non-readonly but missing the live opt-in refuses all mutating modes."""
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.delenv("REBAR_MCP_ALLOW_RECONCILE_LIVE", raising=False)
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": mode}))
    msg = str(exc.value).lower()
    assert f"{mode} reconcile is disabled" in msg, (mode, exc.value)


def test_reconcile_bogus_mode_clean_error(
    monkeypatch: pytest.MonkeyPatch, rebar_repo, _loud_acli
) -> None:
    """An unknown mode is a clean tool error (ValueError listing allowed modes),
    raised before any acli invocation — not a crash."""
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    srv = build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(srv.call_tool("reconcile", {"mode": "bogus"}))
    assert "FAKE ACLI" not in str(exc.value)
    assert "unknown mode" in str(exc.value).lower() or "bogus" in str(exc.value).lower()


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
# The MCP parity adapter collapses every exception to ``return False``, so the
# parity "rejected" tests prove a rejection happened but NOT that MCP surfaces a
# concurrency-SPECIFIC error. These call the server directly and assert the raised
# tool error's message identifies the concurrency condition.
def _concurrency_err_text(srv, tool: str, args: dict) -> str:
    with pytest.raises(Exception) as exc:  # FastMCP raises mcp ...ToolError
        asyncio.run(srv.call_tool(tool, args))
    return str(exc.value).lower()


def test_transition_stale_current_mcp_concurrency_error(rebar_repo) -> None:
    """transition_ticket with a STALE expected current_status surfaces a
    concurrency-identifying tool error over MCP (the ticket is unchanged)."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Stale guard", repo_root=str(rebar_repo))
    # Ticket is open; declare a valid-but-wrong current → optimistic mismatch.
    msg = _concurrency_err_text(
        srv,
        "transition_ticket",
        {"ticket_id": tid, "current_status": "in_progress", "target_status": "closed"},
    )
    assert "transition rejected" in msg and "no longer" in msg
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_claim_already_claimed_mcp_concurrency_error(rebar_repo) -> None:
    """claim_ticket on an already-claimed ticket surfaces a concurrency-identifying
    tool error over MCP; the original assignee is preserved."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Already claimed", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    msg = _concurrency_err_text(srv, "claim_ticket", {"ticket_id": tid, "assignee": "bob"})
    assert "claim rejected" in msg and "already claimed" in msg
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "in_progress" and state.get("assignee") == "alice"


def test_reopen_non_closed_mcp_concurrency_error(rebar_repo) -> None:
    """reopen_ticket on a NON-closed ticket surfaces a concurrency-identifying tool
    error over MCP (parity with CLI exit-10 and library ConcurrencyError)."""
    srv = build_server()
    tid = rebar.create_ticket("task", "Reopen guard", repo_root=str(rebar_repo))
    # Ticket is open, not closed → reopen (closed→open) is a state mismatch.
    msg = _concurrency_err_text(srv, "reopen_ticket", {"ticket_id": tid})
    assert "rejected" in msg and "no longer" in msg
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


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
