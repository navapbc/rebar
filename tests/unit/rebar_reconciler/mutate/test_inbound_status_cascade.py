"""Inbound STATUS writes run the parent-first cascade (ticket bb73-97de-eeea-4899).

Operator ruling (recorded on the ticket): "rebar should maintain, as an invariant,
that no non-terminal-state ticket can be a child of a closed ticket." The Jira
reconciler's inbound-apply path used to write STATUS events directly, bypassing the
docs/concurrency.md I4a parent-first cascade — an inbound Jira status change moving a
child into an active status left its closed ancestors closed, composing exactly the
invalid state the invariant forbids.

Behaviour under test (observable via on-disk events, driving the INBOUND-APPLY path,
not ``transition_compute``):

  1. An inbound child reactivation (``closed -> in_progress``) under a ``closed``
     parent writes the parent's STATUS event FIRST (same edge), so both end active.
  2. The cascade walks the whole eligible ancestor chain (grandparent too).
  3. No parent / a parent not in the eligible status -> no cascade (child alone).
  4. A ``* -> closed`` inbound write never cascades (parity with `_CASCADING_EDGES`).
  5. A same-status inbound write is a suppressed no-op — this is what keeps a
     cascaded parent from fighting the parent's OWN status mutation later in the
     same batch, and makes re-applying an inbound payload idempotent.
  6. The inbound CREATE path cascades too: a create landing at ``in_progress``
     under an ``open`` local parent pulls the parent along (``open -> in_progress``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid as _uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_cascade_test", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_cascade_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture(autouse=True)
def _reset_ticket_reducer_module_cache():
    yield
    from rebar_reconciler import inbound_translate

    inbound_translate._TICKET_REDUCER_MODULE = None


def _make_update_mutation(applier, target: str, payload: dict):
    mut_mod = applier._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target=target,
        payload=payload,
        provenance={"source": "test", "jira_key": target},
    )


def _write_ticket(
    tracker_dir: Path, ticket_id: str, *, parent_id: str = "", status: str = "open"
) -> None:
    """Materialise a minimal reducible ticket: a CREATE event and, when the ticket
    should not sit at the reducer default, a STATUS event."""
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True)
    ts = time.time_ns()
    uid = str(_uuid.uuid4())
    create_event = {
        "timestamp": ts,
        "uuid": uid,
        "event_type": "CREATE",
        "env_id": "test",
        "author": "test",
        "data": {
            "id": ticket_id,
            "ticket_type": "task",
            "title": f"fixture {ticket_id}",
            "description": "",
            "parent_id": parent_id,
            "tags": [],
        },
    }
    (ticket_dir / f"{ts}-{uid}-CREATE.json").write_text(json.dumps(create_event))
    if status != "open":
        ts2 = ts + 1
        uid2 = str(_uuid.uuid4())
        status_event = {
            "timestamp": ts2,
            "uuid": uid2,
            "event_type": "STATUS",
            "env_id": "test",
            "author": "test",
            "data": {"status": status, "current_status": "open"},
        }
        (ticket_dir / f"{ts2}-{uid2}-STATUS.json").write_text(json.dumps(status_event))


def _status_events(tracker_dir: Path, ticket_id: str) -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((tracker_dir / ticket_id).glob("*-STATUS.json"))
    ]


def _reduced_status(tracker_dir: Path, ticket_id: str) -> str:
    from rebar.reducer import reduce_ticket

    state = reduce_ticket(str(tracker_dir / ticket_id))
    assert state is not None, f"reducer returned None for {ticket_id}"
    return state["status"]


def _apply_status_update(applier, tmp_path: Path, local_id: str, status: str) -> list[str]:
    mutation = _make_update_mutation(
        applier,
        target="DIG-1",
        payload={"local_id": local_id, "fields": {"status": status}},
    )
    result = applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)
    return list(result.payload["events"])


# ---------------------------------------------------------------------------
# 1. The central case: inbound reactivation cascades a closed parent
# ---------------------------------------------------------------------------


def test_inbound_reactivation_cascades_closed_parent(tmp_path, applier):
    """closed child -> in_progress under a closed parent: the parent is
    reactivated along the SAME edge FIRST, so the store never holds the
    I4a-invalid closed-parent-with-active-child state."""
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-1", status="closed")
    _write_ticket(tracker_dir, "jira-dig-2", parent_id="jira-dig-1", status="closed")

    events = _apply_status_update(applier, tmp_path, "jira-dig-2", "in_progress")

    assert _reduced_status(tracker_dir, "jira-dig-2") == "in_progress"
    assert _reduced_status(tracker_dir, "jira-dig-1") == "in_progress", (
        "inbound STATUS write bypassed the parent-first cascade: the closed parent "
        "was left closed under an in_progress child (the I4a-invalid state)"
    )
    parent_status_events = _status_events(tracker_dir, "jira-dig-1")
    cascaded = [e for e in parent_status_events if e["data"].get("status") == "in_progress"]
    assert len(cascaded) == 1
    assert cascaded[0]["data"].get("current_status") == "closed", (
        f"cascaded parent event must take the same closed -> in_progress edge, "
        f"got {cascaded[0]['data']}"
    )
    # Parent-first ordering: the cascaded parent event is reported BEFORE the
    # child's own STATUS event in the applier's written-events list.
    parent_paths = [p for p in events if "/jira-dig-1/" in p and p.endswith("-STATUS.json")]
    child_paths = [p for p in events if "/jira-dig-2/" in p and p.endswith("-STATUS.json")]
    assert parent_paths and child_paths, f"expected parent+child STATUS paths, got {events}"
    assert events.index(parent_paths[0]) < events.index(child_paths[0])


# ---------------------------------------------------------------------------
# 2. The cascade walks the whole eligible chain, topmost ancestor first
# ---------------------------------------------------------------------------


def test_inbound_reopen_cascades_whole_closed_chain(tmp_path, applier):
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-10", status="closed")
    _write_ticket(tracker_dir, "jira-dig-11", parent_id="jira-dig-10", status="closed")
    _write_ticket(tracker_dir, "jira-dig-12", parent_id="jira-dig-11", status="closed")

    events = _apply_status_update(applier, tmp_path, "jira-dig-12", "open")

    assert _reduced_status(tracker_dir, "jira-dig-12") == "open"
    assert _reduced_status(tracker_dir, "jira-dig-11") == "open"
    assert _reduced_status(tracker_dir, "jira-dig-10") == "open"
    # Topmost eligible ancestor first: grandparent, then parent, then child.
    chain = ("jira-dig-10", "jira-dig-11", "jira-dig-12")
    order = [seg for p in events for seg in chain if f"/{seg}/" in p]
    assert order == list(chain), f"got {order} from {events}"


# ---------------------------------------------------------------------------
# 3. No cascade when there is no eligible parent
# ---------------------------------------------------------------------------


def test_no_parent_no_cascade(tmp_path, applier):
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-20", status="closed")

    events = _apply_status_update(applier, tmp_path, "jira-dig-20", "in_progress")

    assert _reduced_status(tracker_dir, "jira-dig-20") == "in_progress"
    assert len(events) == 1, f"expected exactly the child's STATUS event, got {events}"


def test_active_parent_not_cascaded(tmp_path, applier):
    """A parent already in_progress is not in the eligible status for the
    closed -> in_progress edge: only the child moves, and the parent gets no
    additional STATUS event."""
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-30", status="in_progress")
    _write_ticket(tracker_dir, "jira-dig-31", parent_id="jira-dig-30", status="closed")
    before = len(_status_events(tracker_dir, "jira-dig-30"))

    _apply_status_update(applier, tmp_path, "jira-dig-31", "in_progress")

    assert _reduced_status(tracker_dir, "jira-dig-31") == "in_progress"
    assert len(_status_events(tracker_dir, "jira-dig-30")) == before


# ---------------------------------------------------------------------------
# 4. Closing edges never cascade (parity with _CASCADING_EDGES)
# ---------------------------------------------------------------------------


def test_close_edge_never_cascades(tmp_path, applier):
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-40", status="closed")
    _write_ticket(tracker_dir, "jira-dig-41", parent_id="jira-dig-40", status="in_progress")
    before = len(_status_events(tracker_dir, "jira-dig-40"))

    _apply_status_update(applier, tmp_path, "jira-dig-41", "closed")

    assert _reduced_status(tracker_dir, "jira-dig-41") == "closed"
    assert len(_status_events(tracker_dir, "jira-dig-40")) == before


# ---------------------------------------------------------------------------
# 5. Same-status no-op suppression (batch self-consistency / idempotency)
# ---------------------------------------------------------------------------


def test_same_status_inbound_write_is_suppressed(tmp_path, applier):
    """After a cascade already moved the parent, the parent's OWN inbound status
    mutation to the same status must not write a duplicate STATUS event."""
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-50", status="in_progress")
    before = len(_status_events(tracker_dir, "jira-dig-50"))

    events = _apply_status_update(applier, tmp_path, "jira-dig-50", "in_progress")

    assert events == [], f"same-status write must be a suppressed no-op, got {events}"
    assert len(_status_events(tracker_dir, "jira-dig-50")) == before


# ---------------------------------------------------------------------------
# 6. The inbound CREATE path cascades too (open -> in_progress)
# ---------------------------------------------------------------------------


def test_inbound_create_active_status_cascades_open_parent(tmp_path, applier):
    tracker_dir = tmp_path / ".tickets-tracker"
    _write_ticket(tracker_dir, "jira-dig-60", status="open")

    mut_mod = applier._load_mutation_module()
    mutation = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-61",
        payload={
            "_parent_local_id": "jira-dig-60",
            "fields": {
                "summary": "inbound child",
                "issuetype": {"name": "Task"},
                "status": {"name": "In Progress"},
            },
        },
        provenance={"source": "test", "jira_key": "DIG-61"},
    )
    applier._apply_inbound_create(mutation, client=None, repo_root=tmp_path, binding_store=None)

    assert _reduced_status(tracker_dir, "jira-dig-61") == "in_progress"
    assert _reduced_status(tracker_dir, "jira-dig-60") == "in_progress", (
        "inbound CREATE landing at in_progress left its open parent behind "
        "(open -> in_progress cascade bypassed)"
    )
