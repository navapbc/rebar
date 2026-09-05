"""Event-schema forward-compatibility: unknown event types are preserved-and-ignored.

Sub-effort (c) of story fatty-cipher-range / ticket astir-plank-scuff.

The event log is the wire format BETWEEN CLONES running different rebar versions
(docs/event-schema.md). An older clone must tolerate an event kind a newer clone
introduced. "Tolerate" has two halves, both pinned here:

  * IGNORED (state level): replaying an unknown ``event_type`` does not error and
    leaves the ticket fully readable (the reducer skips it).
  * PRESERVED (file level): the unknown-type event FILE survives untouched — in
    particular ``compact`` must NOT absorb it into a SNAPSHOT and delete it, or an
    older clone's compaction would destroy a newer clone's data.

Also pins that the schema declares an explicit SCHEMA_VERSION, and — mirror F1 —
that ``_version.KNOWN_EVENT_TYPES`` and the reducer's ``_replay._EVENT_HANDLERS``
table stay EQUAL as sets. That parity was advertised here long before anything
checked it: the only assertions were three memberships. It matters because the two
sides fail asymmetrically. ``_replay.py:195`` gates on KNOWN_EVENT_TYPES and
``_commands/compact_plan.py:131`` makes known types eligible for SNAPSHOT squash and
file retirement, so a type that is KNOWN but has NO handler is folded into nothing
and then deleted — silent permanent data loss. A handler with no known type is only
dead code.

The parity is asserted rather than generated: ``_version`` is a leaf module and
``_replay`` imports FROM it, so deriving one from the other would invert a live
import edge into a cycle. The runtime gate deliberately keeps consulting the broader
KNOWN_EVENT_TYPES (see the comment at ``_replay.py:188-194``) so a downgraded clone
can preserve-and-ignore what it cannot fold; only the possibility of the two sets
DIFFERING is removed here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

import rebar

FUTURE_TYPE = "FUTURE_TYPE"
FUTURE_UUID = "ffffffff-0000-4000-8000-000000000001"
FUTURE_TS = 1_781_000_000_000_000_000  # fixed ns prefix; sorts before any new event


def _cli(*args: str, cwd: str, **env: str) -> subprocess.CompletedProcess:
    e = subprocess_env()
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=e,
        check=False,
    )


def _ticket_dir(repo: Path, tid: str) -> Path:
    return repo / ".tickets-tracker" / tid


def _write_future_event(repo: Path, tid: str) -> Path:
    tdir = _ticket_dir(repo, tid)
    env_id = (repo / ".tickets-tracker" / ".env-id").read_text().strip()
    event = {
        "event_type": FUTURE_TYPE,
        "timestamp": FUTURE_TS,
        "uuid": FUTURE_UUID,
        "env_id": env_id,
        "author": "a-newer-rebar",
        "data": {"some_future_field": "value"},
    }
    path = tdir / f"{FUTURE_TS}-{FUTURE_UUID}-{FUTURE_TYPE}.json"
    path.write_text(json.dumps(event, ensure_ascii=False))
    return path


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "Forward-compat task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


# ── version constant + known-type set are declared and self-consistent ────────
def _load_version_module():
    # Engine python modules are imported as top-level `ticket_reducer` (the engine
    # dir is added to sys.path), not as `rebar._engine.*` (that tree is shipped data,
    # not a python package). Mirror the unit-tier conftest's sys.path insertion.
    from _engine_path import engine_dir

    engine = str(engine_dir())
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from rebar.reducer import _version

    return _version


def test_schema_version_and_known_types_declared() -> None:
    _version = _load_version_module()
    assert isinstance(_version.SCHEMA_VERSION, int)
    assert _version.SCHEMA_VERSION >= 1
    # The reducer's processor dispatch keys "unknown -> ignore" off this set.
    assert "CREATE" in _version.KNOWN_EVENT_TYPES
    assert "SNAPSHOT" in _version.KNOWN_EVENT_TYPES
    assert FUTURE_TYPE not in _version.KNOWN_EVENT_TYPES


# ── IGNORED: unknown event_type does not break replay ─────────────────────────
def test_unknown_event_type_is_ignored_on_replay(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _write_future_event(rebar_repo, tid)
    # show drives a full reduce; an unknown event must not error and the ticket
    # remains fully readable.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "open"
    assert state["ticket_id"] == tid


# ── PRESERVED: compaction must not absorb/delete the unknown event file ────────
def test_unknown_event_file_survives_compaction(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    future_path = _write_future_event(rebar_repo, tid)
    assert future_path.exists()

    # Force compaction (threshold 0 => compact whatever is present). REBAR_SYNC_PULL
    # =off so the no-origin fixture doesn't attempt the in-process pull before compact.
    cp = _cli("compact", tid, "--threshold=0", cwd=str(rebar_repo), REBAR_SYNC_PULL="off")
    assert cp.returncode == 0, f"compact failed: {cp.stderr}"

    # A SNAPSHOT must have been written ...
    snaps = list(_ticket_dir(rebar_repo, tid).glob("*-SNAPSHOT.json"))
    assert snaps, "expected a SNAPSHOT after compaction"
    # ... but the unknown-type event file must remain untouched on disk.
    assert future_path.exists(), (
        "compaction deleted the unknown-type event file — a newer clone's data "
        "would be destroyed by an older clone's compaction"
    )

    # Existence alone is not enough: the PAYLOAD must be preserved byte-equivalently.
    # A regression that truncated/rewrote the file while keeping the path would pass
    # an exists()-only check — so re-read and assert the future fields survived.
    import json as _json

    future_event = _json.loads(future_path.read_text(encoding="utf-8"))
    assert future_event["event_type"] == FUTURE_TYPE
    assert future_event["uuid"] == FUTURE_UUID
    assert future_event["data"]["some_future_field"] == "value", (
        "compaction rewrote the unknown event's payload"
    )

    # And the SNAPSHOT must NOT have absorbed the unknown event's uuid into its
    # provenance — an older clone compacting must not claim to subsume a newer
    # clone's event (which would let a later compaction delete it as 'covered').
    snap = _json.loads(snaps[0].read_text(encoding="utf-8"))
    absorbed = snap.get("data", {}).get("source_event_uuids", [])
    assert FUTURE_UUID not in absorbed, (
        f"compaction snapshot absorbed the unknown event {FUTURE_UUID}: {absorbed}"
    )

    # And replay still succeeds after compaction.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "open"


# ── DETECTABLE: fsck warns when the store has event types newer than this binary ─
def test_fsck_warns_on_unknown_newer_event_type(rebar_repo: Path) -> None:
    """P2.3: a preserved-and-ignored future event is silent in reduced state, so
    fsck must surface it (the rollout window where an old binary under-displays /
    a reconcile host pushes stale state). Generic over KNOWN_EVENT_TYPES."""
    tid = _seed(rebar_repo)
    _write_future_event(rebar_repo, tid)
    report = rebar.fsck(repo_root=str(rebar_repo))
    assert FUTURE_TYPE in report
    assert "newer than this rebar understands" in report
    # It is a WARN, not a corruption finding — fsck must still pass overall.
    assert "CORRUPT" not in report


# ── mirror F1: KNOWN_EVENT_TYPES vs the reducer's dispatch table ──────────────


def _event_type_parity_failure(known, handlers) -> str | None:
    """Describe how the two event-type masters diverge, or ``None`` when they agree.

    Split out from the assertion so the drift cases below can exercise the REPORT
    itself: a parity check whose message does not name the offender sends a reader
    back to diff two files by hand.
    """
    known_only = sorted(set(known) - set(handlers))
    handler_only = sorted(set(handlers) - set(known))
    parts = []
    if known_only:
        parts.append(
            f"KNOWN_EVENT_TYPES with no handler {known_only} — these are folded into "
            "nothing and then made eligible for SNAPSHOT squash + file retirement, "
            "which is silent permanent data loss"
        )
    if handler_only:
        parts.append(
            f"_EVENT_HANDLERS not in KNOWN_EVENT_TYPES {handler_only} — dead handlers, "
            "since the replay gate skips these events before dispatch"
        )
    return "; ".join(parts) or None


def test_known_event_types_equals_the_reducer_dispatch_table() -> None:
    """AC1. Imports both real objects; re-listing the members would defeat the point."""
    from rebar.reducer._replay import _EVENT_HANDLERS

    _version = _load_version_module()
    failure = _event_type_parity_failure(_version.KNOWN_EVENT_TYPES, _EVENT_HANDLERS)
    assert failure is None, failure


def test_parity_reports_a_known_type_that_has_no_handler() -> None:
    """AC2/AC3, the data-loss direction."""
    failure = _event_type_parity_failure({"CREATE", "GHOST"}, {"CREATE": object()})
    assert failure is not None
    assert "GHOST" in failure
    assert "no handler" in failure and "data loss" in failure


def test_parity_reports_a_handler_with_no_known_type() -> None:
    """AC2/AC3, the dead-code direction."""
    failure = _event_type_parity_failure({"CREATE"}, {"CREATE": object(), "ORPHAN": object()})
    assert failure is not None
    assert "ORPHAN" in failure
    assert "dead handler" in failure


def test_parity_holds_against_the_real_objects_under_a_seeded_drift() -> None:
    """AC2 with teeth: seed the divergence into a COPY of the real sets, so the check is
    proven against production values rather than only against hand-built literals."""
    from rebar.reducer._replay import _EVENT_HANDLERS

    _version = _load_version_module()
    known = set(_version.KNOWN_EVENT_TYPES)
    assert _event_type_parity_failure(known, _EVENT_HANDLERS) is None
    known.add("SEEDED_UNHANDLED")
    failure = _event_type_parity_failure(known, _EVENT_HANDLERS)
    assert failure is not None and "SEEDED_UNHANDLED" in failure
