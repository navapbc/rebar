"""Golden parity test: the compiled ticket state is produced by ONE reducer.

After the single-reducer refactor (bug f026), `show`, `list`, `search`, the
native `reduce_ticket`, and the `--output llm` path all derive state from the
Python `ticket_reducer`. This test pins that invariant by comparing the FULL
top-level key set AND values across interfaces for a ticket that exercises every
event type — the check the retired jq/Python schema-parity test could not make
(it only compared selected fields, which is why the show-vs-list drift went
unnoticed). It also asserts internal-only keys never leak to the interface, and
that `verify_commands` (previously emitted only by the jq `show` reducer) is now
present everywhere and survives compaction.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rebar

# Keys the reducer keeps internally but must NOT appear in any interface output.
INTERNAL_KEYS = {"parent_status_uuid", "last_status_env_id"}


def _cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _rich_ticket(repo: Path) -> str:
    """Create a ticket exercising many event types: CREATE, STATUS, EDIT,
    COMMENT, LINK, UNLINK, FILE_IMPACT, VERIFY_COMMANDS, SNAPSHOT (compaction)."""
    r = str(repo)
    epic = rebar.create_ticket("epic", "Epic", repo_root=r)
    tid = rebar.create_ticket("task", "Rich ticket", repo_root=r)
    rebar.claim(tid, assignee="me", repo_root=r)  # STATUS
    rebar.edit_ticket(tid, priority=1, repo_root=r)  # EDIT
    rebar.comment(tid, "a comment", repo_root=r)  # COMMENT
    rebar.link(tid, epic, "relates_to", repo_root=r)  # LINK (no promotion)
    rebar.tag(tid, "alpha", repo_root=r)  # EDIT (tags)
    rebar.set_file_impact(tid, [{"path": "a.py", "reason": "r"}], repo_root=r)  # FILE_IMPACT
    rebar.set_verify_commands(
        tid, [{"dd_id": "D1", "dd_text": "t", "command": "echo"}], repo_root=r
    )  # VERIFY_COMMANDS
    rebar.compact(tid, repo_root=r)  # SNAPSHOT
    return tid


def _elem(items: list[dict], tid: str) -> dict:
    return next(t for t in items if t["ticket_id"] == tid)


def test_show_list_search_share_one_shape(rebar_repo: Path) -> None:
    tid = _rich_ticket(rebar_repo)
    r = str(rebar_repo)

    show = rebar.show_ticket(tid, repo_root=r)
    lst = _elem(rebar.list_tickets(repo_root=r), tid)
    srch = _elem(rebar.search("Rich", repo_root=r), tid)

    # Identical top-level key sets across all three interfaces.
    assert set(show) == set(lst) == set(srch), (
        f"show={sorted(show)}\nlist={sorted(lst)}\nsearch={sorted(srch)}"
    )
    # Identical values too (not just keys).
    assert show == lst == srch

    # Internal-only reducer keys never leak to the interface.
    assert not (INTERNAL_KEYS & set(show))

    # verify_commands flows everywhere and survived compaction (SNAPSHOT).
    assert show["verify_commands"] == [{"dd_id": "D1", "dd_text": "t", "command": "echo"}]
    # Sanity: the rich event history is reflected.
    assert show["status"] == "in_progress"
    assert show["priority"] == 1
    assert show["tags"] == ["alpha"]
    assert show["file_impact"] == [{"path": "a.py", "reason": "r"}]
    assert len(show["comments"]) == 1


def test_native_reduce_matches_interface_modulo_internal_keys(rebar_repo: Path) -> None:
    """The native reducer keeps internal keys (needed in-process); the interface
    strips them. show == native minus the internal keys."""
    tid = _rich_ticket(rebar_repo)
    r = str(rebar_repo)
    show = rebar.show_ticket(tid, repo_root=r)

    tracker = rebar_repo / ".tickets-tracker" / tid
    native = rebar.reduce_ticket(str(tracker))

    # Native carries internal bookkeeping that the interface hides.
    assert "parent_status_uuid" in native
    stripped = {k: v for k, v in native.items() if k not in INTERNAL_KEYS}
    if isinstance(stripped.get("preconditions_summary"), dict):
        stripped["preconditions_summary"] = {
            k: v for k, v in stripped["preconditions_summary"].items() if k != "source_count"
        }
    assert stripped == show


def test_llm_parity_show_vs_list(rebar_repo: Path) -> None:
    """`show --output llm` and the `list --full --output llm` element for the same
    ticket are identical (both internal-marker-free). `list` is lean by default
    (no `desc`/`cm`), so full parity is asserted against `--full`."""
    tid = _rich_ticket(rebar_repo)
    r = str(rebar_repo)

    show_llm = json.loads(_cli("show", "--output", "llm", tid, cwd=r).stdout)
    list_lines = [
        json.loads(ln)
        for ln in _cli("list", "--full", "--output", "llm", cwd=r).stdout.splitlines()
        if ln.strip()
    ]
    list_llm = next(t for t in list_lines if t.get("id") == tid)

    assert show_llm == list_llm
    for internal in INTERNAL_KEYS:
        assert internal not in show_llm


def test_list_lean_by_default_omits_body(rebar_repo: Path) -> None:
    """The default `list` is lean: it drops the bulky `description`/`comments`
    fields (and their LLM short keys `desc`/`cm`); `--full` restores them."""
    tid = _rich_ticket(rebar_repo)
    r = str(rebar_repo)

    # JSON form: lean default omits description/comments; --full includes them.
    lean = next(t for t in json.loads(_cli("list", cwd=r).stdout) if t["ticket_id"] == tid)
    assert "description" not in lean
    assert "comments" not in lean
    full = next(
        t for t in json.loads(_cli("list", "--full", cwd=r).stdout) if t["ticket_id"] == tid
    )
    assert "description" in full
    assert "comments" in full

    # LLM form mirrors the lean default (short keys desc/cm absent).
    lean_llm = next(
        json.loads(ln)
        for ln in _cli("list", "--output", "llm", cwd=r).stdout.splitlines()
        if ln.strip() and json.loads(ln).get("id") == tid
    )
    assert "desc" not in lean_llm
    assert "cm" not in lean_llm
