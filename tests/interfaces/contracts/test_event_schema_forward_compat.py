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

Also pins that the schema declares an explicit SCHEMA_VERSION and that the set of
event types the reducer handles matches the declared KNOWN_EVENT_TYPES.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rebar

FUTURE_TYPE = "FUTURE_TYPE"
FUTURE_UUID = "ffffffff-0000-4000-8000-000000000001"
FUTURE_TS = 1_781_000_000_000_000_000  # fixed ns prefix; sorts before any new event


def _cli(*args: str, cwd: str, **env: str) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=e,
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
    engine_dir = Path(rebar.__file__).resolve().parent / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
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
