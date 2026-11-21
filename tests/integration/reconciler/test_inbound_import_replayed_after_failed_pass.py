"""A reconciler pass that fails mid-apply loses no inbound import and duplicates no Jira write.

Bug reponsible-exposed-okapi asked what happens when a pass's inbound write is refused
mid-pass (a reclaim fetched during the pass) after the pass has already written to Jira.
Answer, proven here over the stateful fake Jira with real passes:

* The applier applies inbound mutations before outbound ones, so the refusal ends the pass
  before any outbound Jira write. The only Jira writes already made are an inbound create's
  identity markers (``rebar-id:jira-<key>`` label and ``local_id`` property).
* The production bridge delivers nothing from a failed pass, and the next run starts from a
  fresh checkout, so the local CREATE and binding are gone while those markers remain.
* The binding walk's level-triggered ADOPT arm (``binding_walk._adopt_stands_down`` lets a
  lone marker equal to the deterministic inbound id, with no binding and no local ticket,
  replay ADOPT) re-imports the issue on the next pass: one CREATE, one binding, the same
  single label, no ``create_issue``. Plain inbound changes are simply re-applied.
* The blast-radius breaker gates that ADOPT like every acting decision: when replays exceed
  ``max_acting_fraction`` of confirmed bindings it holds them and logs
  ``binding_walk_breaker_refused`` instead of acting.
"""

# ruff: noqa: F811 — pytest fixtures are imported by name; ruff reads the parameter of
# every test that uses one as a redefinition.
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_echo_loop_experiment import _bindings, _ensure_tracker_git, _local_ticket_ids, _tracker
from test_echo_loop_experiment import _run_pass as _run_echo_pass
from test_reconcile_idempotency import (  # noqa: F401
    _FakeClient,
    _FakeJiraState,
    git_repo,
    reconciler_modules,
)

from rebar._commands._seam import SupersededStoreRefused
from rebar.reconciler import apply_inbound_events
from rebar.reconciler.scratch_retention import sync_log_path

pytestmark = pytest.mark.integration

_NEW_KEY = "DIG-2"
_NEW_ID = "jira-dig-2"
_RENAMED_KEY = "DIG-101"
# 20 bound pairs: one replayed ADOPT is 0.05 of confirmed bindings, under the 0.10 default.
_POPULATION = 20


def _git(tracker: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tracker), *args], check=True, capture_output=True, text=True
    ).stdout


def _run_pass(reconciler_modules, client, git_repo, monkeypatch, pass_id: str):
    return _run_echo_pass(reconciler_modules, client, git_repo, monkeypatch, pass_id)


def _converged_store(git_repo: Path, reconciler_modules, monkeypatch, population: int):
    """``population`` bound pairs whose converged tracker is committed (a fresh checkout)."""
    state = _FakeJiraState()
    for i in range(1, population + 1):
        state.seed(
            f"DIG-{100 + i}",
            summary=f"Ticket {i}",
            status={"name": "In Progress"},
            issuetype={"name": "Story"},
            priority={"name": "High"},
        )
    client = _FakeClient(state)
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "seed")
    tracker = _tracker(git_repo)
    _ensure_tracker_git(tracker)
    if _git(tracker, "status", "--porcelain").strip():
        _git(tracker, "add", "-A")
        _git(tracker, "commit", "-qm", "converged tracker")
    return state, client


def _seed_new_issue(state: _FakeJiraState) -> None:
    state.seed(
        _NEW_KEY,
        summary="New in Jira",
        status={"name": "To Do"},
        issuetype={"name": "Story"},
        priority={"name": "High"},
    )


def _fail_pass_after_writeback(
    git_repo: Path, reconciler_modules, monkeypatch, client, error: Exception
) -> list[tuple[str, str]]:
    """Run one pass whose inbound EDIT write raises ``error`` after DIG-2's create landed."""
    real = apply_inbound_events._write_event_file
    seen: list[tuple[str, str]] = []

    def _write(tracker_dir, ticket_id, event_type, data):
        seen.append((ticket_id, event_type))
        if event_type == "EDIT":
            raise error
        return real(tracker_dir, ticket_id, event_type, data)

    with monkeypatch.context() as patch:
        patch.setattr(apply_inbound_events, "_write_event_file", _write)
        with pytest.raises(type(error)):
            _run_pass(reconciler_modules, client, git_repo, monkeypatch, "failed")
    return seen


def _discard_uncommitted(git_repo: Path) -> None:
    """What the next bridge run sees after a failed pass: only the committed tracker."""
    tracker = _tracker(git_repo)
    _git(tracker, "reset", "-q", "--hard")
    _git(tracker, "clean", "-qfdx")


def _create_events(git_repo: Path, local_id: str) -> list[str]:
    ticket_dir = _tracker(git_repo) / local_id
    if not ticket_dir.is_dir():
        return []
    return sorted(p.name for p in ticket_dir.iterdir() if p.name.endswith("-CREATE.json"))


def _local_title(git_repo: Path, local_id: str) -> str | None:
    from rebar.reducer import reduce_ticket

    return (reduce_ticket(str(_tracker(git_repo) / local_id)) or {}).get("title")


def _identity_labels(state: _FakeJiraState, key: str) -> list[str]:
    return [lbl for lbl in state.issues[key].get("labels", []) if lbl.startswith("rebar-id")]


def _orphan_after_failed_pass(git_repo, reconciler_modules, monkeypatch, client, state, error):
    """Leave DIG-2 orphaned: Jira markers written, local import discarded."""
    _seed_new_issue(state)
    state.issues[_RENAMED_KEY]["summary"] = "Renamed in Jira"
    state.write_calls.clear()
    seen = _fail_pass_after_writeback(git_repo, reconciler_modules, monkeypatch, client, error)
    assert (_NEW_ID, "CREATE") in seen, "precondition: DIG-2's inbound create ran"
    assert state.write_calls == [
        f"add_label({_NEW_KEY},rebar-id:{_NEW_ID})",
        f"set_entity_property({_NEW_KEY},local_id)",
    ], "the failed pass's only Jira writes are DIG-2's identity markers (no outbound write)"
    _discard_uncommitted(git_repo)
    assert _NEW_ID not in _local_ticket_ids(git_repo)
    assert _NEW_ID not in _bindings(git_repo)


@pytest.mark.parametrize(
    "error",
    [
        SupersededStoreRefused("Error: ticket write refused: DIVERGED (mid-pass reclaim)"),
        RuntimeError("disk full"),
    ],
    ids=["reclaim-refusal", "runtime-error"],
)
def test_failed_pass_import_is_replayed_once_with_no_duplicate_jira_write(
    git_repo, reconciler_modules, monkeypatch, error
) -> None:
    state, client = _converged_store(git_repo, reconciler_modules, monkeypatch, _POPULATION)
    _orphan_after_failed_pass(git_repo, reconciler_modules, monkeypatch, client, state, error)

    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "replay")

    assert _bindings(git_repo)[_NEW_ID]["jira_key"] == _NEW_KEY
    assert _local_title(git_repo, _NEW_ID) == "New in Jira"
    assert _local_title(git_repo, f"jira-{_RENAMED_KEY.lower()}") == "Renamed in Jira"
    assert len(_create_events(git_repo, _NEW_ID)) == 1
    assert not [call for call in state.write_calls if call.startswith("create_issue")]
    assert _identity_labels(state, _NEW_KEY) == [f"rebar-id:{_NEW_ID}"]
    assert sorted(state.issues) == sorted([*(f"DIG-{100 + i}" for i in range(1, 21)), _NEW_KEY])

    # Converged: a further pass makes no Jira write and no new local event.
    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "steady")
    assert state.write_calls == []
    assert len(_create_events(git_repo, _NEW_ID)) == 1


def _logged_events(git_repo: Path, pass_id: str) -> list[dict]:
    path = sync_log_path(git_repo, pass_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_replay_above_the_blast_radius_cap_is_held_logged_and_later_released(
    git_repo, reconciler_modules, monkeypatch
) -> None:
    """With 1 confirmed binding, 1 replayed ADOPT is 1.0 of bindings: the breaker holds it."""
    state, client = _converged_store(git_repo, reconciler_modules, monkeypatch, 1)
    _orphan_after_failed_pass(
        git_repo,
        reconciler_modules,
        monkeypatch,
        client,
        state,
        SupersededStoreRefused("Error: ticket write refused: DIVERGED (mid-pass reclaim)"),
    )

    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "held")

    assert _NEW_ID not in _local_ticket_ids(git_repo)
    assert state.write_calls == []
    refusals = [
        e for e in _logged_events(git_repo, "held") if e["event"] == "binding_walk_breaker_refused"
    ]
    assert len(refusals) == 1 and "EXCEEDS" in refusals[0]["reason"], refusals

    # Held, not lost: the identity marker survives, and once the store has enough confirmed
    # bindings for one ADOPT to sit under the cap, a later pass imports DIG-2 exactly once.
    assert _identity_labels(state, _NEW_KEY) == [f"rebar-id:{_NEW_ID}"]
    for i in range(2, _POPULATION + 1):
        state.seed(
            f"DIG-{100 + i}",
            summary=f"Ticket {i}",
            status={"name": "In Progress"},
            issuetype={"name": "Story"},
            priority={"name": "High"},
        )
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "grow")
    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "released")
    assert _bindings(git_repo)[_NEW_ID]["jira_key"] == _NEW_KEY
    assert len(_create_events(git_repo, _NEW_ID)) == 1
    assert not [call for call in state.write_calls if call.startswith("create_issue")]
    assert _identity_labels(state, _NEW_KEY) == [f"rebar-id:{_NEW_ID}"]


def _orphan_markers(state: _FakeJiraState, local_id: str) -> None:
    _seed_new_issue(state)
    state.issues[_NEW_KEY]["labels"] = [f"rebar-id:{local_id}"]
    state.props.setdefault(_NEW_KEY, {})["local_id"] = local_id


def test_a_marker_naming_another_local_id_is_not_adopted(
    git_repo, reconciler_modules, monkeypatch
) -> None:
    """An outbound-origin issue (its own local id) is never adopted as Jira-native."""
    state, client = _converged_store(git_repo, reconciler_modules, monkeypatch, _POPULATION)
    _orphan_markers(state, "7f3a-0000-0000-0001")
    before = _local_ticket_ids(git_repo)

    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "guard")

    assert _local_ticket_ids(git_repo) == before
    assert _NEW_ID not in _bindings(git_repo)
    assert state.write_calls == []


def test_a_surviving_local_import_is_not_adopted_twice(
    git_repo, reconciler_modules, monkeypatch
) -> None:
    """A local import that survived but lost its binding row is not re-imported."""
    from rebar.reconciler.binding_store import BindingStore

    state, client = _converged_store(git_repo, reconciler_modules, monkeypatch, _POPULATION)
    _seed_new_issue(state)
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "import")
    assert len(_create_events(git_repo, _NEW_ID)) == 1
    store = BindingStore(_tracker(git_repo))
    store.unbind(_NEW_ID)
    store.save()
    assert _NEW_ID not in _bindings(git_repo)

    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "guard")

    assert len(_create_events(git_repo, _NEW_ID)) == 1
