"""Task 08c8: ``_repair_ticket`` must rebuild through ``rebuild_with_restore``.

Bug b636 added deleted-source recovery to fsck, but only ``repair_or_plan`` routed its
rebuild through the composed ``rebuild_with_restore``. The per-ticket path
``_repair_ticket`` still called ``rebuild_snapshot_from_full_log`` DIRECTLY, so a ticket
whose SNAPSHOT source was dropped by a legacy (delete-style) compaction was never
recovered when repair ran through that entry point — the b636 fail-closed guard simply
refused the rebuild.

These tests pin the three properties of the fix:

* the deleted source IS restored (and the rebuild then succeeds) via ``_repair_ticket``;
* ``no_commit=True`` still suppresses the commit — the batch repair loop
  (``fsck_repair.py``'s ``--repair`` batching) depends on deferring commits;
* a second pass restores nothing (no double-restore, no double-report).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar


def _git(tracker: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tracker), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(tracker: Path, msg: str) -> None:
    _git(tracker, "add", "-A")
    subprocess.run(
        [
            "git",
            "-C",
            str(tracker),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "-m",
            msg,
        ],
        check=False,
        capture_output=True,
    )


# The crafted SNAPSHOT sits far above any real HLC tick, so every real event sorts
# BEFORE it — which is what makes the surviving COMMENT a pre-snapshot orphan.
_SNAP_TS = "9000000000000000000"
_SNAP_UUID = "88c88c88-8c88-4c88-8c88-8c888c888c88"


@pytest.fixture
def legacy_compacted(rebar_repo: Path):
    """A ticket shaped like a legacy delete-style compaction left it.

    Yields ``(tracker, ticket_id, ticket_dir, victim_name)`` where ``victim_name`` is a
    COMMENT source the SNAPSHOT cites but which is GONE from the worktree (recoverable
    only from tickets history). A second, surviving COMMENT is left uncited so it reads
    as an AUTO-RECOVER orphan — that is what makes ``_repair_ticket`` attempt a rebuild
    at all.
    """
    tracker = Path(rebar_repo) / ".tickets-tracker"
    tid = rebar.create_ticket("task", "08c8 legacy-compacted fixture")
    rebar.comment(tid, "victim-body-that-must-be-restored")
    rebar.comment(tid, "surviving-orphan-body")

    ticket_dir = tracker / tid
    comments = sorted(ticket_dir.glob("*-COMMENT.json"))
    assert len(comments) == 2, f"fixture expects two COMMENTs, got {[c.name for c in comments]}"
    victim = comments[0]
    victim_uuid = json.loads(victim.read_text())["uuid"]
    create = next(ticket_dir.glob("*-CREATE.json"))
    create_uuid = json.loads(create.read_text())["uuid"]
    _commit(tracker, "seed events")

    # Legacy delete-style compaction: the folded source is REMOVED and the removal
    # committed, so the blob survives only at the deleting commit's parent.
    victim_name = victim.name
    victim.unlink()
    _commit(tracker, "ticket: COMPACT (legacy delete-style)")

    (ticket_dir / f"{_SNAP_TS}-{_SNAP_UUID}-SNAPSHOT.json").write_text(
        json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": int(_SNAP_TS),
                "uuid": _SNAP_UUID,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {
                    "compiled_state": {"status": "open"},
                    "source_event_uuids": [create_uuid, victim_uuid],
                },
            }
        ),
        encoding="utf-8",
    )
    _commit(tracker, "ticket: SNAPSHOT")

    from rebar._commands.fsck_repair import snapshot_missing_sources

    assert snapshot_missing_sources(str(ticket_dir)) == [victim_uuid], (
        "precondition: the SNAPSHOT must cite a source absent from disk"
    )
    return tracker, tid, ticket_dir, victim_name


def _repair(tracker: Path, tid: str, ticket_dir: Path, *, no_commit: bool) -> dict:
    from rebar._commands.fsck_repair import _repair_ticket

    return _repair_ticket(str(tracker), tid, str(ticket_dir), dry_run=False, no_commit=no_commit)


def test_repair_ticket_restores_a_source_deleted_by_legacy_compaction(legacy_compacted):
    """RED before 08c8: ``_repair_ticket`` bypassed ``rebuild_with_restore``, so the
    deleted source was never recovered and the b636 guard refused the rebuild."""
    tracker, tid, ticket_dir, victim_name = legacy_compacted
    from rebar._commands.fsck_repair import snapshot_missing_sources

    disp = _repair(tracker, tid, ticket_dir, no_commit=True)

    assert disp.get("restored") == [victim_name], (
        "_repair_ticket must report the sources it restored, like repair_or_plan does"
    )
    assert (ticket_dir / (victim_name + ".retired")).exists(), (
        "the deleted source must be written back as a FOLDED .retired source"
    )
    assert not (ticket_dir / victim_name).exists(), "it must NOT be resurrected as a live event"
    assert snapshot_missing_sources(str(ticket_dir)) == [], "the log is complete again"
    assert disp["rebuilt"] is True, (
        "with the log completed the rebuild must proceed, not fail closed"
    )


def test_repair_ticket_honours_no_commit(legacy_compacted):
    """``no_commit=True`` must still suppress the commit after the route change —
    the batch repair loop calls ``_repair_ticket(..., no_commit=True)`` precisely so
    commits are deferred until after the batch."""
    tracker, tid, ticket_dir, victim_name = legacy_compacted
    before = _git(tracker, "rev-parse", "HEAD")

    disp = _repair(tracker, tid, ticket_dir, no_commit=True)

    assert disp["rebuilt"] is True, "precondition: the rebuild must actually run"
    assert (ticket_dir / (victim_name + ".retired")).exists(), "precondition: restored"
    assert _git(tracker, "rev-parse", "HEAD") == before, (
        "no_commit=True must not create a commit on the tickets branch"
    )
    assert _git(tracker, "status", "--porcelain"), (
        "the rebuild's writes must be left UNCOMMITTED for the batch to commit later"
    )


def test_repair_ticket_commits_when_no_commit_is_false(legacy_compacted):
    """The default (``no_commit=False``) still commits — the flag is forwarded, not
    hard-wired."""
    tracker, tid, ticket_dir, _victim = legacy_compacted
    before = _git(tracker, "rev-parse", "HEAD")

    disp = _repair(tracker, tid, ticket_dir, no_commit=False)

    assert disp["rebuilt"] is True
    assert _git(tracker, "rev-parse", "HEAD") != before, (
        "no_commit=False must commit the rebuild, as it did before the route change"
    )


def test_second_repair_pass_does_not_double_restore(legacy_compacted):
    """Restore is idempotent: a source already present as ``*.retired`` is skipped, and
    ``rebuild_with_restore`` only attempts a restore while sources are still missing. A
    second pass must therefore report an EMPTY restored list."""
    tracker, tid, ticket_dir, victim_name = legacy_compacted

    first = _repair(tracker, tid, ticket_dir, no_commit=True)
    assert first.get("restored") == [victim_name], "precondition: the first pass restores"

    second = _repair(tracker, tid, ticket_dir, no_commit=True)

    assert second.get("restored") == [], "a second pass must restore nothing"
    retired = sorted(p.name for p in ticket_dir.glob(victim_name + "*"))
    assert retired == [victim_name + ".retired"], (
        f"the restored source must not be duplicated: {retired}"
    )


def test_rebuild_with_restore_default_is_unchanged_for_repair_or_plan(legacy_compacted):
    """The existing ``repair_or_plan`` call site passes no ``no_commit``; the new
    parameter must default to False so that path's behavior is unchanged."""
    import inspect

    from rebar._commands.fsck_restore import rebuild_with_restore

    params = inspect.signature(rebuild_with_restore).parameters
    assert "no_commit" in params, "rebuild_with_restore must accept no_commit"
    assert params["no_commit"].default is False, (
        "default must be False so the repair_or_plan call site is unchanged"
    )
    assert params["no_commit"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "no_commit must be keyword-only, matching rebuild_snapshot_from_full_log"
    )

    tracker, tid, ticket_dir, victim_name = legacy_compacted
    before = _git(tracker, "rev-parse", "HEAD")

    rebuilt, restored = rebuild_with_restore(str(tracker), tid, str(ticket_dir))

    assert rebuilt is True and restored == [victim_name]
    assert _git(tracker, "rev-parse", "HEAD") != before, (
        "the default path still commits, exactly as repair_or_plan relies on"
    )
