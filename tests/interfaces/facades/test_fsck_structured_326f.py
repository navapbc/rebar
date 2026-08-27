"""Bug 326f-595a-088b-40a5 — a COMPLETED fsck run is a result, not a tool error.

``fsck`` is a DIAGNOSTIC: finding problems IS its success condition. The library
flattened every non-zero exit into ``RebarError("rebar fsck failed (exit N): <whole
text report>")`` and the MCP tool relayed that to the client as
``{"error": "command_failed", "exit_code": 1, "message": "<the report>"}`` — so a
caller could not ENUMERATE the findings, and could not tell "the store has issues"
from "the tool broke".

These tests pin the four halves of the contract:

* a completed run (exit 0 or 1) returns a STRUCTURED, enumerable result;
* a run that could NOT happen (exit >= 2) still raises — the two stay distinguishable;
* the CLI's non-zero-on-findings exit is untouched (``git fsck``/``fsck(8)`` convention);
* ``rebar.fsck()``'s legacy ``-> str`` / raise contract is untouched for its callers.

Plus AC4: a self-healed ``STATUS_FORK_RESOLVED`` is REPORTED but does not fail the run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar._errors import RebarError

pytestmark = pytest.mark.interface

from adapters import _unwrap  # noqa: E402  (tests/interfaces is on sys.path)


# ── fixture helpers ───────────────────────────────────────────────────────────
def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _seed_missing_create(repo: Path, ticket_id: str = "aaaa-bbbb-cccc-dddd") -> str:
    """Seed a ticket dir holding an event but NO CREATE → one MISSING_CREATE finding.

    Chosen because it is a *counted* per-ticket finding with a ticket_id, so it
    exercises the enumerate-by-kind-and-ticket path the bug report asks for.
    """
    d = _tracker(repo) / ticket_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001-comment.json").write_text(
        json.dumps({"type": "COMMENT", "ticket_id": ticket_id, "body": "orphan"})
    )
    return ticket_id


def _cli_fsck(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar", "fsck", *extra],
        cwd=repo,
        capture_output=True,
        check=False,
        text=True,
    )


def _mcp_fsck(**kwargs):
    from rebar.mcp_server import build_server

    return _unwrap(asyncio.run(build_server().call_tool("fsck", kwargs)))


# ── the RED core: a completed run with findings is a RESULT ───────────────────
def test_mcp_fsck_with_findings_returns_enumerable_structure_not_an_error(
    rebar_repo: Path,
) -> None:
    """THE BUG. A store WITH findings must yield findings a client can enumerate."""
    ticket_id = _seed_missing_create(rebar_repo)

    # Must not raise: a completed diagnostic run is not a failed call.
    result = _mcp_fsck()

    assert isinstance(result, dict), f"expected a structured mapping, got {type(result)}"
    assert isinstance(result.get("issues"), list)
    # Enumerable BY KIND and BY TICKET — the two axes the bug report names.
    by_kind = {i["kind"] for i in result["issues"]}
    assert "missing_create" in by_kind, by_kind
    seeded = [i for i in result["issues"] if i.get("ticket_id") == ticket_id]
    assert len(seeded) == 1, result["issues"]
    assert seeded[0]["kind"] == "missing_create"
    assert "no CREATE event found" in seeded[0]["detail"]
    assert result["issue_count"] == len(result["issues"])
    assert result["fixed"] == []


def test_library_fsck_report_with_findings_returns_completed_run(rebar_repo: Path) -> None:
    """The library seam under the MCP tool: findings, and the exit as DATA."""
    ticket_id = _seed_missing_create(rebar_repo)

    report = rebar.fsck_report(repo_root=str(rebar_repo))

    assert report["returncode"] == 1, "a completed run WITH findings reports exit 1 as data"
    assert any(i.get("ticket_id") == ticket_id for i in report["issues"])
    assert report["issue_count"] >= 1


def test_library_fsck_report_on_clean_store_reports_zero(rebar_repo: Path) -> None:
    report = rebar.fsck_report(repo_root=str(rebar_repo))
    assert report["returncode"] == 0
    assert report["issue_count"] == 0
    assert report["issues"] == []


# ── the other half: a run that could NOT happen still raises ──────────────────
def test_fsck_report_raises_when_fsck_could_not_run(tmp_path: Path) -> None:
    """A genuine EXECUTION failure stays distinguishable from findings.

    An uninitialised store cannot be scanned at all, so the library must RAISE rather
    than hand back an empty "clean" report — reporting "your store is fine" for a store
    that was never read would be worse than the bug this ticket fixes.

    Deliberately NOT asserted on the exit code: this path exits **1**, the same code as
    "issues found" (``_commands/fsck.py`` ``_missing_tracker_result``, whose JSON branch
    emits a payload byte-identical to a clean store and puts the real diagnostic on the
    TEXT path only). The exit code alone therefore cannot carry this distinction, so the
    test asserts the OBSERVABLE contract — it raised instead of returning a report, and
    the error says the store could not be scanned — not a guess about the code.
    """
    bare = tmp_path / "not-a-store"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=bare, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=bare, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=bare, check=True)

    with pytest.raises(RebarError) as exc:
        rebar.fsck_report(repo_root=str(bare))
    assert "could not scan" in str(exc.value), (
        f"the error must name the unscannable store, not read as a finding: {exc.value}"
    )


# ── the preserved contracts ───────────────────────────────────────────────────
def test_cli_still_exits_nonzero_on_findings(rebar_repo: Path) -> None:
    """PRESERVED: scripts depend on the git-fsck/fsck(8) non-zero-on-findings exit."""
    _seed_missing_create(rebar_repo)
    assert _cli_fsck(rebar_repo).returncode == 1
    assert _cli_fsck(rebar_repo, "--output", "json").returncode == 1


def test_legacy_rebar_fsck_still_returns_str_and_still_raises(rebar_repo: Path) -> None:
    """PRESERVED: rebar.fsck()'s -> str / raise-on-findings contract is untouched.

    ~10 existing tests across tests/interfaces/** assert on its string return.
    """
    assert isinstance(rebar.fsck(repo_root=str(rebar_repo)), str)  # clean store
    _seed_missing_create(rebar_repo)
    with pytest.raises(RebarError):
        rebar.fsck(repo_root=str(rebar_repo))


def test_mcp_fsck_advertises_the_canonical_output_schema() -> None:
    """The tool joins its typed neighbours (clarity_check/check_ac/validate/…)."""
    from rebar.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server().list_tools())}
    schema = tools["fsck"].outputSchema
    assert schema, "MCP fsck must advertise an outputSchema"
    props = set(schema.get("properties", {}))
    assert {"issues", "fixed", "issue_count"} <= props, props


# ── AC4: a self-healed race is REPORTED but does not fail the run ─────────────
_FORK_TS = 1999999999999999999
_FORK_UUID = "ffffffff-ffff-4fff-8fff-ffffffffffff"


def _seed_status_fork(repo: Path) -> str:
    """Put a real ticket into the post-race state the reducer records.

    A STATUS event whose ``current_status`` disagrees with the compiled status is
    exactly what the reducer treats as a diverged chain: it resolves the fork by the
    lexical-UUID rule and appends to ``status_fork_resolutions``
    (``reducer/_processors_status.py:110-162``). Same shape the reducer's own fork
    test uses (``tests/unit/test_reducer_status_fork_record.py:47-56``), so this is a
    genuine resolved fork rather than a hand-written SNAPSHOT.

    The event is committed to the tracker so it cannot be mistaken for tracker dirt.
    """
    created = rebar.create_ticket("task", "forked", return_alias=True)
    ticket_id = created["id"]
    event = {
        "event_type": "STATUS",
        "ticket_id": ticket_id,
        "uuid": _FORK_UUID,
        "timestamp": _FORK_TS,
        "author": "T",
        "author_email": "t@e",
        "parent_status_uuid": None,
        "data": {"status": "closed", "current_status": "in_progress", "parent_status_uuid": None},
    }
    path = _tracker(repo) / ticket_id / f"{_FORK_TS}-{_FORK_UUID}-STATUS.json"
    path.write_text(json.dumps(event))
    tracker = _tracker(repo)
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed resolved fork"], cwd=tracker, check=True)
    assert rebar.show_ticket(ticket_id).get("status_fork_resolutions"), (
        "fixture precondition: the reducer must have RECORDED a resolved fork"
    )
    return ticket_id


def test_status_fork_resolved_is_reported_but_does_not_fail_the_run(rebar_repo: Path) -> None:
    """AC4. A race the reducer already healed is history, not a live defect.

    Mirrors the established never-counted precedent (TRACKER_DIRTY_TMP_EVENT,
    PUSH_PENDING) — the finding stays fully visible, only its contribution to the
    failure exit is dropped.
    """
    ticket_id = _seed_status_fork(rebar_repo)

    proc = _cli_fsck(rebar_repo, "--output", "json")
    payload = json.loads(proc.stdout)
    kinds = {i["kind"] for i in payload["issues"]}

    assert "status_fork_resolved" in kinds, "the finding must STILL be reported"
    assert any(
        i.get("ticket_id") == ticket_id and i["kind"] == "status_fork_resolved"
        for i in payload["issues"]
    )
    assert proc.returncode == 0, (
        "a self-healed fork is the ONLY finding here and must not fail the run"
    )
