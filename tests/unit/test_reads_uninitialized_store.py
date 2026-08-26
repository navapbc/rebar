"""Library reads must not report an ABSENT ticket store as an EMPTY one.

WHY THIS TEST EXISTS. The in-process read path (`rebar._reads`) resolves the tracker
directory through one chokepoint, `_reads._tracker()`, and for a long time never checked
that the directory existed. One layer down, `reducer/_api.py` wraps its `os.listdir(tracker)`
in `except OSError: return results` — and `FileNotFoundError` IS an `OSError` — so a store
that is not there at all reduced to `[]` with no error at all.

That is not a cosmetic difference. Every sibling surface treats an absent store as an error:
the CLI reads (`_engine_support/reads_cli.py:340,384`) and the library WRITES
(`_store/event_prepare.py:99-102`). Only library reads stayed silent, and the MCP read tools
delegate straight to them (`_mcp_reads.py` -> `rebar.list_tickets`). The consequence was
observed in production: an MCP server deployed with no store answered `list_tickets`,
`search` and `ready` with `[]` while `fsck` correctly reported "ticket system not
initialized". A driving agent is then told "there are no tickets" when the truth is "this
store is missing", which is precisely the fallback-masking that `docs/mcp-reference.md:7`
forbids -- faults surface as errors "instead of silently resolving to a fallback", so "a
driving agent can tell a broken config from a deliberate policy refusal".

The parametrized set is EVERY read entry the guard governs, not a sample. `ready` is the
library spelling of the MCP `ready_tickets` tool (one of the three that answered `[]` on
the storeless production server); `deps` and `next_batch` are the remaining two that
funnel through `_tracker()`. Sampling here is what let the gap reach review twice --
plan review caught the missing `ready`, code review caught `deps`/`next_batch` -- so the
set is now exhaustive by construction: any new read entry added to `_reads.py` that is
not listed here is an untested inheritance of this guard.

THE DISTINCTION THIS PINS, and the reason the last test below is not optional: the fix must
separate "the tracker directory does not exist" (an error) from "the tracker exists and holds
no tickets" (a legitimate empty result). A guard that raises on both would be just as wrong
in the other direction, and would break every caller that reads a freshly-initialized store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._errors import RebarError

_NOT_INITIALIZED = "not initialized"


def _bare_repo(tmp_path: Path) -> Path:
    """A git repo with NO ticket store — `rebar init` is deliberately never run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    # Precondition, asserted rather than assumed: the whole test rests on this being absent.
    assert not (repo / ".tickets-tracker").exists()
    return repo


def _initialized_empty_repo(tmp_path: Path) -> Path:
    """An initialized store holding zero tickets — the legitimate `[]` case."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    rebar.init_repo(repo_root=repo)
    assert (repo / ".tickets-tracker").is_dir()
    return repo


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("list_tickets", lambda repo: rebar.list_tickets(repo_root=repo)),
        ("search", lambda repo: rebar.search("anything", repo_root=repo)),
        ("ready", lambda repo: rebar.ready(repo_root=repo)),
        ("recent_session_logs", lambda repo: rebar.recent_session_logs(repo_root=repo)),
        ("deps", lambda repo: rebar.deps("abcd-1234-5678-9abc", repo_root=repo)),
        ("next_batch", lambda repo: rebar.next_batch("abcd-1234-5678-9abc", repo_root=repo)),
    ],
)
def test_reads_raise_on_absent_store_instead_of_returning_empty(
    tmp_path: Path, name: str, call
) -> None:
    """A read against a store that does not exist is an ERROR, never an empty list."""
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        call(repo)

    assert _NOT_INITIALIZED in str(excinfo.value), (
        f"{name} must name the real fault (uninitialized store), got: {excinfo.value}"
    )


def test_show_ticket_on_absent_store_blames_the_store_not_the_id(tmp_path: Path) -> None:
    """`show_ticket` already raised — but for the WRONG reason.

    Against a missing store it reported "Ticket '<id>' not found", sending the reader off to
    hunt for a ticket when the store itself was never there.
    """
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        rebar.show_ticket("abcd-1234-5678-9abc", repo_root=repo)

    assert _NOT_INITIALIZED in str(excinfo.value), (
        f"show_ticket must blame the missing store, not the id, got: {excinfo.value}"
    )


def test_reads_and_writes_agree_that_an_absent_store_is_an_error(tmp_path: Path) -> None:
    """The parity this defect broke: same store, same library, same process.

    The write path has always raised here (`event_prepare._ensure_initialized`). This asserts
    the read path now agrees, so the two surfaces can no longer disagree about whether an
    absent store is a fault.
    """
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as write_err:
        rebar.create_ticket("task", "probe", repo_root=repo)
    with pytest.raises(RebarError) as read_err:
        rebar.list_tickets(repo_root=repo)

    assert _NOT_INITIALIZED in str(write_err.value)
    assert _NOT_INITIALIZED in str(read_err.value)


def test_initialized_but_empty_store_still_returns_empty(tmp_path: Path) -> None:
    """The no-regression half: an EXISTING store with no tickets is not an error.

    This is what keeps the guard honest. It must key on the tracker's existence, not on the
    result being empty -- otherwise a freshly-initialized store would start raising.
    """
    repo = _initialized_empty_repo(tmp_path)

    assert rebar.list_tickets(repo_root=repo) == []
    assert rebar.search("anything", repo_root=repo) == []
    assert rebar.ready(repo_root=repo) == []


def test_audit_index_renders_empty_rather_than_500ing_without_a_store(tmp_path: Path) -> None:
    """The one caller that legitimately depends on the OLD degrade-to-empty behaviour.

    `rebar.audit.server._audited_tickets` backs a read-only web index and documents itself as
    best-effort. Making library reads raise is right for a driving agent -- it can act on
    "the store is missing" -- but a browser hitting the audit index should see an empty page,
    not a 500. This pins that the guard is scoped to the uninitialized-store case: the index
    degrades, while a genuinely broken store still propagates.

    This test exists because the caller sweep for the parent fix searched `_reads.*` consumers
    and so never saw this call site, which reaches the same code through the PUBLIC
    `rebar.list_tickets`. Code review caught it.
    """
    from rebar.audit.server import _audited_tickets

    repo = _bare_repo(tmp_path)

    assert _audited_tickets(repo_root=str(repo)) == []
