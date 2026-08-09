"""b636: a snapshot rebuild must never silently discard state that exists ONLY inside a
prior SNAPSHOT's ``compiled_state``.

Legacy compaction (before 16640a705d, "retire folded events instead of deleting") DELETED
its folded source events. For such a ticket the raw log on disk is INCOMPLETE: the prior
SNAPSHOT cites ``source_event_uuids`` with no corresponding file. ``include_retired=True``
replay therefore reconstructs from a partial history and the newest SURVIVING status wins —
silently reverting closed tickets to their claim state. This regressed 214 live tickets.

The rebuild must fail closed on that shape (surface for human triage) rather than write a
lossy snapshot.
"""

from __future__ import annotations

import json as _json

from tests.integration.test_concurrency_regression import (  # noqa: F401
    _engine_run,
    _tracker,
    two_clones,
)


def _legacy_compacted_closed_ticket(repo_a, seed, tracker_a):
    """Shape a ticket like a legacy (delete-style) compaction left it: a SNAPSHOT whose
    compiled_state is ``closed`` and whose cited STATUS source file is GONE from disk,
    plus a surviving earlier STATUS (the ``open -> in_progress`` claim)."""
    from rebar.reducer import reduce_ticket

    seed_dir = tracker_a / seed
    create_uuid = _json.loads(next(seed_dir.glob("*-CREATE.json")).read_text())["uuid"]

    # A surviving claim STATUS (open -> in_progress) — this is what a lossy replay falls
    # back to.
    claim_uuid = "11111111-1111-4111-8111-111111111111"
    (seed_dir / f"1000000000000000000-{claim_uuid}-STATUS.json").write_text(
        _json.dumps(
            {
                "event_type": "STATUS",
                "timestamp": 1000000000000000000,
                "uuid": claim_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {"current_status": "open", "status": "in_progress"},
            }
        )
    )
    # The close STATUS is DELETED from disk — it survives only as a cited source uuid.
    deleted_close_uuid = "22222222-2222-4222-8222-222222222222"

    compiled = {k: v for k, v in reduce_ticket(str(seed_dir)).items() if k != "updated_at"}
    compiled["status"] = "closed"

    snap_uuid = "33333333-3333-4333-8333-333333333333"
    (seed_dir / f"9000000000000000000-{snap_uuid}-SNAPSHOT.json").write_text(
        _json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 9000000000000000000,
                "uuid": snap_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                "data": {
                    "compiled_state": compiled,
                    # cites the DELETED close event -> the log is provably incomplete
                    "source_event_uuids": [create_uuid, claim_uuid, deleted_close_uuid],
                },
            }
        )
    )
    return seed_dir


def test_rebuild_refuses_when_snapshot_cites_missing_sources(two_clones):  # noqa: F811
    """RED for b636: the ticket reads ``closed`` before the rebuild; a rebuild driven off
    the incomplete log must NOT leave it reverted to the surviving claim status."""
    from rebar._commands.compact import rebuild_snapshot_from_full_log
    from rebar.reducer import reduce_ticket

    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = _legacy_compacted_closed_ticket(repo_a, seed, tracker_a)

    assert reduce_ticket(str(seed_dir))["status"] == "closed", "precondition: reads closed"

    did = rebuild_snapshot_from_full_log(str(tracker_a), seed, str(seed_dir), no_commit=True)

    # Whatever the disposition, the ticket must still read closed. A rebuild that
    # "succeeds" by dropping the close is the bug.
    assert reduce_ticket(str(seed_dir))["status"] == "closed", (
        "rebuild silently discarded a close that existed only in the prior SNAPSHOT"
    )
    assert did is False, "must fail closed (surface for human triage), not rebuild lossily"


def test_repair_snapshots_dry_run_writes_nothing(two_clones):  # noqa: F811
    """b636 secondary defect: ``--repair-snapshots`` ignored ``--dry-run`` entirely and
    MUTATED the store, so the broad legacy rebuild could not be previewed. A dry run must
    leave every byte untouched and still print a plan."""
    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = _legacy_compacted_closed_ticket(repo_a, seed, tracker_a)

    # Event files only: ``.cache.json`` is a DERIVED reducer cache, not store history.
    def _events():
        return {
            p.name: p.read_bytes()
            for p in sorted(seed_dir.iterdir())
            if p.is_file() and not p.name.startswith(".")
        }

    before = _events()

    res = _engine_run(repo_a, "fsck", "--repair-snapshots", "--dry-run", check=False)

    after = _events()
    assert after == before, "--dry-run must not modify, add, or remove any event file"
    assert "DRY-RUN" in res.stdout, "a dry run must print the per-ticket plan"


def test_fsck_surfaces_missing_sources(two_clones):  # noqa: F811
    """The latent population must be visible BEFORE anyone runs a repair."""
    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    _legacy_compacted_closed_ticket(repo_a, seed, tracker_a)

    res = _engine_run(repo_a, "fsck", check=False)

    assert "SNAPSHOT_MISSING_SOURCES" in res.stdout, (
        "fsck must report snapshots citing source events absent from disk"
    )


def test_rebuild_still_folds_orphan_when_log_is_complete(two_clones):  # noqa: F811
    """Guard the RC2b capability we must PRESERVE: when the raw log IS complete, the
    rebuild still folds a merged-in pre-snapshot orphan back in."""
    from rebar._commands.compact import rebuild_snapshot_from_full_log
    from rebar.reducer import reduce_ticket

    _remote, repo_a, _repo_b, seed = two_clones
    tracker_a = _tracker(repo_a)
    seed_dir = tracker_a / seed

    create_uuid = _json.loads(next(seed_dir.glob("*-CREATE.json")).read_text())["uuid"]
    compiled = {k: v for k, v in reduce_ticket(str(seed_dir)).items() if k != "updated_at"}
    _engine_run(repo_a, "comment", seed, "orphan-comment-body")
    snap_uuid = "44444444-4444-4444-8444-444444444444"
    (seed_dir / f"9000000000000000000-{snap_uuid}-SNAPSHOT.json").write_text(
        _json.dumps(
            {
                "event_type": "SNAPSHOT",
                "timestamp": 9000000000000000000,
                "uuid": snap_uuid,
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Test",
                # complete: every cited source is present on disk
                "data": {"compiled_state": compiled, "source_event_uuids": [create_uuid]},
            }
        )
    )

    did = rebuild_snapshot_from_full_log(str(tracker_a), seed, str(seed_dir), no_commit=True)

    assert did is True, "a complete log must still rebuild (RC2b orphan folding preserved)"
    state = reduce_ticket(str(seed_dir))
    assert any("orphan-comment-body" in (c.get("body") or "") for c in state.get("comments", []))
