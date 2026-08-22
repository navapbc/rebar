"""One terminal-state blocker predicate, agreed on by every reader (bug e63c).

Four sites independently defined "this blocker no longer blocks", with three
different memberships -- ``graph/_ready.py`` (``{closed}``), ``graph/_unblock.py``
and ``graph/_graph.py`` (``{closed, deleted}``), and
``_engine_support/next_batch.py`` (``{closed, done, completed, deleted}``, two of
which are not in the status vocabulary at all).

The observable consequence, reproduced end-to-end below: a ticket blocked by a
*deleted* ticket was offered by ``next-batch`` while ``ready`` withheld it. The
maintainer's terminal set is ``{closed, archived, deleted}``.

These tests assert observable behavior only -- the predicate's answers, the
schema-conformance of its membership, and agreement between the ``ready`` /
``next-batch`` / ``deps`` surfaces over a real store. Nothing here pins which
module holds the predicate or how the four call sites import it, so a later
refactor that preserves the answers keeps these green.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar

# ─────────────────────────── the predicate itself ───────────────────────────
# Imported inside each test rather than at module scope: until the predicate
# exists the import raises, and a module-level import would collapse the whole
# file into a collection error -- hiding whether the behavioral parity tests
# below fail for the RIGHT reason (the ready/next-batch split) or merely because
# the module would not load.


def test_terminal_set_is_exactly_the_maintainer_decision() -> None:
    """closed / archived / deleted -- each is a terminal state."""
    from rebar.reducer import TERMINAL_STATUSES

    assert set(TERMINAL_STATUSES) == {"closed", "archived", "deleted"}


@pytest.mark.parametrize("status", ["closed", "archived", "deleted"])
def test_terminal_states_satisfy_a_blocker(status: str) -> None:
    from rebar.reducer import is_terminal_status

    assert is_terminal_status(status) is True


@pytest.mark.parametrize("status", ["idea", "open", "in_progress", "blocked"])
def test_live_states_do_not_satisfy_a_blocker(status: str) -> None:
    from rebar.reducer import is_terminal_status

    assert is_terminal_status(status) is False


@pytest.mark.parametrize("phantom", ["done", "completed"])
def test_phantom_statuses_are_not_terminal(phantom: str) -> None:
    """``done``/``completed`` were carried by next_batch's set but have never been
    in the status vocabulary -- they were unreachable branches, and reintroducing
    them would silently re-diverge next-batch from ready."""
    from rebar.reducer import is_terminal_status

    assert is_terminal_status(phantom) is False


def test_terminal_set_is_a_subset_of_the_schema_status_enum() -> None:
    """Vocabulary drift must fail fast: every terminal status has to be a real
    status in ``common.schema.json#/$defs/ticket_status``."""
    from rebar import schemas
    from rebar.reducer import TERMINAL_STATUSES

    enum = set(schemas.load("common")["$defs"]["ticket_status"]["enum"])
    assert set(TERMINAL_STATUSES) <= enum


# ────────────────────── agreement across the read surfaces ──────────────────


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=False,
    )


def _next_batch_ids(epic: str, cwd: Path) -> set[str]:
    cp = _cli("next-batch", epic, "--output", "json", cwd=cwd)
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    items = payload["batch"] if isinstance(payload, dict) else payload
    # batch_item is keyed `id` (common.schema.json#/$defs/batch_item), not `ticket_id`.
    return {i["id"] if isinstance(i, dict) else i for i in items}


def _ready_ids(cwd: Path) -> set[str]:
    return {t["ticket_id"] for t in rebar.ready(repo_root=str(cwd))}


def _deps_ready(tid: str, cwd: Path) -> bool:
    cp = _cli("deps", tid, cwd=cwd)
    assert cp.returncode == 0, cp.stderr
    return bool(json.loads(cp.stdout)["ready_to_work"])


def _blocked_by_terminal(rebar_repo: Path, make_terminal) -> tuple[str, str, str]:
    """Build epic → {blocker, blocked} where blocked depends_on blocker, and drive
    the blocker into a terminal state. The link is made AFTER the transition so the
    peer-unlink that ``delete`` performs cannot erase the edge under test."""
    root = str(rebar_repo)
    epic = rebar.create_ticket("epic", "Terminal parity epic", repo_root=root)
    blocker = rebar.create_ticket("task", "Blocker", parent=epic, repo_root=root)
    blocked = rebar.create_ticket("task", "Blocked", parent=epic, repo_root=root)
    make_terminal(blocker, rebar_repo)
    rebar.link(blocked, blocker, "depends_on", repo_root=root)
    return epic, blocker, blocked


def _delete(tid: str, repo: Path) -> None:
    cp = _cli("delete", tid, "--user-approved", cwd=repo)
    assert cp.returncode == 0, cp.stderr


def _archive(tid: str, repo: Path) -> None:
    rebar.archive(tid, repo_root=str(repo))


def test_ready_and_next_batch_agree_on_a_deleted_blocker(rebar_repo: Path) -> None:
    """The reproduced split: ``next-batch`` offered a ticket blocked by a deleted
    ticket while ``ready`` withheld it. Both must now treat it as unblocked."""
    epic, _blocker, blocked = _blocked_by_terminal(rebar_repo, _delete)

    ready = _ready_ids(rebar_repo)
    batch = _next_batch_ids(epic, rebar_repo)

    assert (blocked in ready) == (blocked in batch), (
        f"ready and next-batch disagree: ready={blocked in ready} batch={blocked in batch}"
    )
    assert blocked in ready, "a deleted blocker is terminal and must not block"


def test_ready_and_next_batch_agree_on_an_archived_blocker(rebar_repo: Path) -> None:
    """``archived`` is newly terminal for all four readers."""
    epic, _blocker, blocked = _blocked_by_terminal(rebar_repo, _archive)

    ready = _ready_ids(rebar_repo)
    batch = _next_batch_ids(epic, rebar_repo)

    assert (blocked in ready) == (blocked in batch)
    assert blocked in ready, "an archived blocker is terminal and must not block"


def test_deps_ready_to_work_agrees_with_ready_on_a_deleted_blocker(
    rebar_repo: Path,
) -> None:
    """``deps``' own ``ready_to_work`` is the third reader of the same predicate."""
    _epic, _blocker, blocked = _blocked_by_terminal(rebar_repo, _delete)

    assert _deps_ready(blocked, rebar_repo) is (blocked in _ready_ids(rebar_repo))
    assert _deps_ready(blocked, rebar_repo) is True


def test_deps_ready_to_work_agrees_with_ready_on_an_archived_blocker(
    rebar_repo: Path,
) -> None:
    _epic, _blocker, blocked = _blocked_by_terminal(rebar_repo, _archive)

    assert _deps_ready(blocked, rebar_repo) is (blocked in _ready_ids(rebar_repo))
    assert _deps_ready(blocked, rebar_repo) is True


def test_an_open_blocker_still_blocks_every_surface(rebar_repo: Path) -> None:
    """The negative case -- widening the terminal set must not make everything
    ready. A live blocker still withholds the dependent on all three surfaces."""
    root = str(rebar_repo)
    epic = rebar.create_ticket("epic", "Live blocker epic", repo_root=root)
    blocker = rebar.create_ticket("task", "Live blocker", parent=epic, repo_root=root)
    blocked = rebar.create_ticket("task", "Blocked", parent=epic, repo_root=root)
    rebar.link(blocked, blocker, "depends_on", repo_root=root)

    assert blocked not in _ready_ids(rebar_repo)
    assert blocked not in _next_batch_ids(epic, rebar_repo)
    assert _deps_ready(blocked, rebar_repo) is False
