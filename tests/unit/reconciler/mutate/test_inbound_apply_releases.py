"""Inbound Jira fixVersions land as RELEASE_DELTA events (ADR 0124, lovely-bipartisan-mullet).

Drives the real applier (``applier._apply_inbound_update`` / ``_apply_inbound_create``) and,
for the multi-pass cases, the real ``compute_inbound_mutations`` differ in between.
"""

from __future__ import annotations

import importlib
import json
import time
import uuid as _uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def applier():
    """The applier facade; resets the lazily cached ticket reducer afterwards so a
    monkeypatched reducer never leaks into another test."""
    yield importlib.import_module("rebar.reconciler.applier")
    from rebar.reconciler import inbound_translate

    inbound_translate._TICKET_REDUCER_MODULE = None


def _mutation(applier, target: str, payload: dict, action: str = "update"):
    mut_mod = applier._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=getattr(mut_mod.MutationAction, action),
        target=target,
        payload=payload,
        provenance={"source": "test", "jira_key": target},
    )


def _seed(tracker_dir: Path, ticket_id: str) -> None:
    (tracker_dir / ticket_id).mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    uid = str(_uuid.uuid4())
    event = {
        "timestamp": ts,
        "uuid": uid,
        "event_type": "CREATE",
        "env_id": "test-env",
        "author": "test",
        "data": {"id": ticket_id, "ticket_type": "task", "title": "seed", "parent_id": ""},
    }
    (tracker_dir / ticket_id / f"{ts}-{uid}-CREATE.json").write_text(json.dumps(event))


def _release_deltas(ticket_dir: Path) -> list[dict]:
    events = [json.loads(p.read_text()) for p in sorted(ticket_dir.glob("*.json"))]
    return [e["data"] for e in events if e.get("event_type") == "RELEASE_DELTA"]


def _reduce(ticket_dir: Path) -> dict:
    from rebar.reconciler import inbound_translate

    return inbound_translate._load_ticket_reducer().reduce_ticket(str(ticket_dir))


def _apply_releases(applier, tmp_path: Path, local_id: str, releases: list[dict]) -> None:
    payload = {"local_id": local_id, "fields": {}, "labels": [], "releases": releases}
    applier._apply_inbound_update(_mutation(applier, "DIG-1", payload), repo_root=tmp_path)


@pytest.fixture
def ticket(tmp_path: Path) -> tuple[Path, str]:
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_id = "re1ea5e0-0000-0000-0000-000000000001"
    _seed(tracker_dir, local_id)
    return tracker_dir, local_id


def test_add_and_remove_write_one_inbound_release_delta(applier, tmp_path, ticket) -> None:
    tracker_dir, local_id = ticket
    _apply_releases(applier, tmp_path, local_id, [{"action": "add", "release": "2.4"}])
    _apply_releases(
        applier,
        tmp_path,
        local_id,
        [{"action": "add", "release": "2.5"}, {"action": "remove", "release": "2.4"}],
    )
    deltas = _release_deltas(tracker_dir / local_id)
    assert deltas == [
        {"added": ["2.4"], "removed": [], "source": "inbound"},
        {"added": ["2.5"], "removed": ["2.4"], "source": "inbound"},
    ]
    assert _reduce(tracker_dir / local_id)["releases"] == ["2.5"]


def test_repeat_with_matching_state_writes_nothing(applier, tmp_path, ticket) -> None:
    tracker_dir, local_id = ticket
    _apply_releases(applier, tmp_path, local_id, [{"action": "add", "release": "2.4"}])
    _apply_releases(
        applier,
        tmp_path,
        local_id,
        [{"action": "add", "release": "2.4"}, {"action": "remove", "release": "absent"}],
    )
    assert len(_release_deltas(tracker_dir / local_id)) == 1


def test_add_and_remove_of_an_absent_name_is_written_as_added(applier, tmp_path, ticket) -> None:
    tracker_dir, local_id = ticket
    _apply_releases(
        applier,
        tmp_path,
        local_id,
        [{"action": "add", "release": "X"}, {"action": "remove", "release": "X"}],
    )
    assert _release_deltas(tracker_dir / local_id) == [
        {"added": ["X"], "removed": [], "source": "inbound"}
    ]


def test_reducer_failure_writes_nothing(applier, tmp_path, ticket, monkeypatch) -> None:
    tracker_dir, local_id = ticket
    from rebar.reconciler import inbound_translate

    reducer_mod = inbound_translate._load_ticket_reducer()

    def _boom(*_a: Any, **_kw: Any) -> dict:
        raise RuntimeError("simulated reducer failure")

    monkeypatch.setattr(reducer_mod, "reduce_ticket", _boom)
    _apply_releases(applier, tmp_path, local_id, [{"action": "add", "release": "2.4"}])
    assert _release_deltas(tracker_dir / local_id) == []


def test_release_delta_leaves_tags_alone(applier, tmp_path, ticket) -> None:
    tracker_dir, local_id = ticket
    _apply_releases(applier, tmp_path, local_id, [{"action": "add", "release": "2.4"}])
    assert not [p for p in (tracker_dir / local_id).glob("*-TAG_DELTA.json")]


# ── Multi-pass: real differ between applies ───────────────────────────────────


class _Store:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = bindings

    def get_local_id(self, jira_key: str) -> str | None:
        return self._bindings.get(jira_key)


def _jira_entry(names: list[str]) -> dict[str, Any]:
    return {
        "summary": "seed",
        "issuetype": {"name": "Task"},
        "labels": [],
        "fixVersions": [
            {"self": "s", "id": str(i), "name": n, "archived": False, "released": False}
            for i, n in enumerate(names)
        ],
    }


def _one_pass(applier, tmp_path: Path, tracker_dir: Path, jira_key: str, local_id: str, entry):
    """Diff the snapshot against the reduced local ticket and apply the release records,
    shaped as run_differs builds the inbound update payload."""
    from rebar.reconciler import inbound_differ

    local = _reduce(tracker_dir / local_id)
    mutations, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot={jira_key: entry},
        binding_store=_Store({jira_key: local_id}),
        local_tickets_by_id={local_id: local},
    )
    for im in mutations:
        payload = {
            "local_id": im.local_id,
            "fields": {},
            "labels": im.labels,
            "comments": [],
            "links": [],
            "releases": im.releases,
        }
        applier._apply_inbound_update(_mutation(applier, jira_key, payload), repo_root=tmp_path)


def test_two_passes_over_one_payload_write_exactly_one_delta(applier, tmp_path, ticket) -> None:
    tracker_dir, local_id = ticket
    entry = _jira_entry(["2.4"])
    _one_pass(applier, tmp_path, tracker_dir, "DIG-2", local_id, entry)
    _one_pass(applier, tmp_path, tracker_dir, "DIG-2", local_id, entry)
    assert len(_release_deltas(tracker_dir / local_id)) == 1
    assert _reduce(tracker_dir / local_id)["releases"] == ["2.4"]


def test_imported_ticket_gets_releases_on_the_next_pass(applier, tmp_path) -> None:
    tracker_dir = tmp_path / ".tickets-tracker"
    entry = _jira_entry(["2.4"])
    create = _mutation(applier, "DIG-71", {"fields": entry}, action="create")
    applier._apply_inbound_create(create, client=None, repo_root=tmp_path, binding_store=None)
    local_id = "jira-dig-71"
    assert _reduce(tracker_dir / local_id)["releases"] == []
    _one_pass(applier, tmp_path, tracker_dir, "DIG-71", local_id, entry)
    assert _release_deltas(tracker_dir / local_id) == [
        {"added": ["2.4"], "removed": [], "source": "inbound"}
    ]
