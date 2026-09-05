"""Readiness must not depend on any field the lean pass drops.

``find_ready_tickets`` computes readiness over LEAN states (``_READY_OMITTED_FIELDS``)
and re-reduces only the ready subset, so the whole store's bodies are never
simultaneously live. The byte-identity check that covers the ``omit_fields=()``
default CANNOT cover this path -- it deliberately produces different intermediate
state. A ticket wrongly classified ready (or not-ready) would be a silent
correctness bug that no memory test catches, so it is asserted directly here.

The readiness computation reads exactly four top-level keys:

* ``ticket_id``  -- the lookup key (``_ready.py``, building ``ticket_states``)
* ``status``     -- the ``error``/``fsck_needed`` skip, ``_is_open``, and
                    ``_is_closed`` on each blocker
* ``parent_id``  -- the ``epic_filter`` narrowing only
* ``deps``       -- via ``build_blocked_by``, which reads ``relation`` and
                    ``target_id`` off each entry

plus ``ticket_type`` / ``archived``, consumed by ``reduce_all_tickets``' own
``exclude_session_logs`` / ``exclude_archived`` filters. None of those is in
``LEAN_OMITTED_FIELDS``. These tests prove that claim by DIFFERENTIAL EXECUTION
rather than by reading: the same store is evaluated with the projection on and
off, and the two ready sets must agree exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar._engine_support.reads import LEAN_OMITTED_FIELDS
from rebar.graph import _ready as ready_mod
from rebar.graph._ready import find_ready_tickets

pytestmark = pytest.mark.unit

_ENV = "00000000-0000-4000-8000-000000000001"


def _event(tdir: Path, ts: int, uid: str, kind: str, data: dict) -> None:
    (tdir / f"{ts}-{uid}-{kind}.json").write_text(
        json.dumps(
            {
                "timestamp": ts,
                "uuid": uid,
                "event_type": kind,
                "env_id": _ENV,
                "author": "Equivalence Tester",
                "data": data,
            }
        ),
        encoding="utf-8",
    )


def _ticket(
    root: Path,
    n: int,
    *,
    ticket_type: str = "task",
    status: str | None = None,
    parent_id: str | None = None,
    deps: list[tuple[str, str]] | None = None,
    heavy: bool = True,
) -> str:
    """One ticket whose BULK lives entirely in the lean-omitted fields."""
    tid = f"{n:04d}-0000-0000-4000"
    tdir = root / tid
    tdir.mkdir()
    uid = f"{n:08d}-1111-4111-8111-111111111111"
    filler = "x" * 20_000 if heavy else ""
    _event(
        tdir,
        100 + n,
        uid,
        "CREATE",
        {
            "ticket_type": ticket_type,
            "title": f"ticket {n}",
            "parent_id": parent_id,
            "description": filler,
        },
    )
    if heavy:
        _event(
            tdir,
            150 + n,
            f"{n:08d}-3333-4333-8333-333333333333",
            "COMMENT",
            {"body": filler},
        )
    if status is not None:
        _event(
            tdir,
            500 + n,
            f"{n:08d}-2222-4222-8222-222222222222",
            "STATUS",
            {"status": status},
        )
    for i, (relation, target) in enumerate(deps or []):
        _event(
            tdir,
            600 + n + i,
            f"{n:08d}-4{i:03d}-4444-8444-444444444444",
            "LINK",
            {"relation": relation, "target_id": target},
        )
    return tid


def _rich_store(root: Path) -> None:
    """A store exercising every branch readiness can take."""
    blocker_open = _ticket(root, 1)  # open blocker
    blocker_closed = _ticket(root, 2, status="closed")
    _ticket(root, 3)  # plain open -> ready
    _ticket(root, 4, status="closed")  # closed -> not ready
    _ticket(root, 5, status="in_progress")  # in_progress -> ready
    # depends_on an OPEN blocker -> blocked
    _ticket(root, 6, deps=[("depends_on", blocker_open)])
    # depends_on a CLOSED blocker -> ready
    _ticket(root, 7, deps=[("depends_on", blocker_closed)])
    # depends_on a MISSING blocker -> treated as closed -> ready
    _ticket(root, 8, deps=[("depends_on", "dead-0000-0000-4000")])
    # the inverse relation: 9 blocks 10, so 10 is blocked while 9 is open
    _ticket(root, 9, deps=[("blocks", "0010-0000-0000-4000")])
    _ticket(root, 10)
    # non-blocking relation must NOT block
    _ticket(root, 11, deps=[("relates_to", blocker_open)])
    # children of an epic, for the epic_filter arm
    epic = _ticket(root, 12, ticket_type="epic")
    _ticket(root, 13, parent_id=epic)
    _ticket(root, 14, parent_id=epic, status="closed")
    # the artifact types reduce_all_tickets excludes
    _ticket(root, 15, ticket_type="session_log")
    _ticket(root, 16, ticket_type="code_review")
    # an event-less directory -> an error/debris state
    (root / "0017-0000-0000-4000").mkdir()


def _ready_ids(tracker: Path, epic: str | None = None) -> list[str]:
    return sorted(s.get("ticket_id", "") for s in find_ready_tickets(str(tracker), epic))


@pytest.fixture
def rich_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _rich_store(tracker)
    return tracker


@pytest.mark.parametrize("epic", [None, "0012-0000-0000-4000"])
def test_lean_and_full_readiness_agree(
    rich_tracker: Path, monkeypatch: pytest.MonkeyPatch, epic: str | None
) -> None:
    """The ready SET is identical with the projection on and off."""
    lean_ids = _ready_ids(rich_tracker, epic)

    # Turn the projection OFF: readiness now runs over FULL states.
    monkeypatch.setattr(ready_mod, "_READY_OMITTED_FIELDS", ())
    full_ids = _ready_ids(rich_tracker, epic)

    assert lean_ids == full_ids, (
        "readiness disagrees between the lean and full passes -- the computation "
        "reads a field the lean pass drops"
    )
    # Guard against the comparison being vacuous.
    assert lean_ids, "fixture produced no ready tickets"


def test_lean_readiness_returns_full_states(rich_tracker: Path) -> None:
    """Pass 2 must rehydrate: callers still receive the omitted fields."""
    ready = find_ready_tickets(str(rich_tracker))
    assert ready
    bodied = [s for s in ready if s.get("description")]
    assert bodied, "no ready ticket came back with its description -- rehydration failed"
    for state in bodied:
        assert len(state["description"]) == 20_000


def test_readiness_ignores_every_lean_omitted_field(
    rich_tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the six fields ONE AT A TIME never changes the ready set.

    Stronger than the joint check above: it localizes the claim per field, so a
    future field that readiness *does* consume cannot hide behind the other five.
    """
    monkeypatch.setattr(ready_mod, "_READY_OMITTED_FIELDS", ())
    baseline = _ready_ids(rich_tracker)

    for field in LEAN_OMITTED_FIELDS:
        monkeypatch.setattr(ready_mod, "_READY_OMITTED_FIELDS", (field,))
        assert _ready_ids(rich_tracker) == baseline, (
            f"dropping {field!r} changed the ready set: readiness reads it"
        )
