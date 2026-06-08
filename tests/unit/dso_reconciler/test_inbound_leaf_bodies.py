"""Story bd19-d744-b8c7-4079 — observable-behaviour tests for the 5 inbound
applier leaves.

Each test builds a Mutation, invokes applier.apply() against an isolated
fixture tracker directory, and asserts the documented side effect (event
file, directory rename, follow-on payload, client call).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


def _load_mutation_module():
    # Load under the canonical key used by applier.py so that Mutation
    # objects created here share class identity with the applier-side enum
    # members (avoids _direction_guard 'is' check failures).
    canonical = "plugins.dso.scripts.dso_reconciler.mutation"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, MUTATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_under_test", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A minimal repo layout with an initialised .tickets-tracker dir.

    Removes TICKETS_TRACKER_DIR from the environment so a developer-set
    override in the host shell cannot leak into the test and steer writes
    away from the tmp tracker dir (PR #375 review thread 3306949620).

    Also removes DSO_ENV_ID and DSO_AUTHOR — both are read by
    ``applier._event_meta()`` and written into every event file. If
    developer-shell values leak into the test the assertions still pass
    locally but the event-file ``env_id``/``author`` diverge between local
    and CI runs (PR #375 review thread 3307104056).
    """
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.delenv("DSO_ENV_ID", raising=False)
    monkeypatch.delenv("DSO_AUTHOR", raising=False)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-env-id", encoding="utf-8")
    return tmp_path


def _make_mutation(
    mut_mod, *, direction, action, target, payload=None, provenance=None
):
    return mut_mod.Mutation(
        direction=direction,
        action=action,
        target=target,
        payload=payload or {},
        provenance=provenance or {"source": "test"},
    )


def _patch_apply_deps(applier, monkeypatch):
    """Stub applier.apply()'s lazy module loaders for an offline, all-inbound run.

    apply() calls ``_load_acli()`` unconditionally to construct the Jira client
    (applier.py ~2716). The real acli-integration.py does
    ``from dso_reconciler.adf import text_to_adf`` at import time, which is
    unresolvable under this file's spec_from_file_location loading scheme
    (modules live under the ``plugins.dso.scripts.dso_reconciler`` package, not a
    bare ``dso_reconciler`` package) — so the real load raises ModuleNotFoundError
    before any inbound dispatch happens. Mirror test_e2e_dedup_pass: stub
    ``_load_acli`` (offline client) and ``_load_concurrency`` (no git in tmp_path).
    """
    fake_acli = types.ModuleType("acli_integration_stub")
    fake_acli.AcliClient = lambda **_: MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setattr(applier, "_load_acli", lambda: fake_acli)

    fake_conc = types.ModuleType("concurrency_stub")
    fake_conc.snapshot_head = lambda _repo_root: "deadbeef" * 5  # type: ignore[attr-defined]

    class _Result:
        ok = True
        event = None
        value = None

    def _rebase_retry(_repo_root, write_fn, **_kwargs):
        write_fn()
        return _Result()

    fake_conc.rebase_retry = _rebase_retry  # type: ignore[attr-defined]
    monkeypatch.setattr(applier, "_load_concurrency", lambda: fake_conc)


def _event_files(tracker_dir: Path, local_id: str) -> list[Path]:
    return sorted((tracker_dir / local_id).glob("*.json"))


def _read_create(tracker_dir: Path, local_id: str) -> dict:
    for path in _event_files(tracker_dir, local_id):
        ev = json.loads(path.read_text())
        if ev.get("event_type") == "CREATE":
            return ev
    raise AssertionError(f"no CREATE event in {tracker_dir / local_id}")


# ---------------------------------------------------------------------------
# 1. inbound create
# ---------------------------------------------------------------------------


def test_inbound_create_writes_local_ticket(applier, mut_mod, fixture_repo):
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-123",
        payload={
            "fields": {
                "summary": "X",
                "priority": 2,
                "status": "To Do",
                "issuetype": "Task",
            },
            "labels": [],
        },
    )
    result = applier._apply_typed(mutation, repo_root=fixture_repo)

    tracker = fixture_repo / ".tickets-tracker"
    local_id = "jira-dig-123"
    assert (tracker / local_id).is_dir()
    create_ev = _read_create(tracker, local_id)
    data = create_ev["data"]
    assert data["title"] == "X"
    assert data["priority"] == 2
    assert data["ticket_type"] == "task"
    assert "imported:reconciler-bootstrap" in data["tags"]
    assert result.payload["local_id"] == local_id


def test_inbound_create_via_leaves_registry(applier, mut_mod):
    """Pin the dispatch path: _LEAVES[(inbound, create)] resolves to the leaf."""
    key = (mut_mod.MutationDirection.inbound, mut_mod.MutationAction.create)
    assert applier._LEAVES[key] is applier._apply_inbound_create


# ---------------------------------------------------------------------------
# 2. inbound update
# ---------------------------------------------------------------------------


def test_inbound_update_writes_edit_event(applier, mut_mod, fixture_repo):
    # Seed an existing ticket dir via inbound create.
    create_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-123",
        payload={"fields": {"summary": "Original", "issuetype": "Task"}},
    )
    applier._apply_typed(create_mut, repo_root=fixture_repo)

    update_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-123",
        payload={"fields": {"summary": "Y"}},
    )
    result = applier._apply_typed(update_mut, repo_root=fixture_repo)

    tracker = fixture_repo / ".tickets-tracker"
    edits = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-123")
        if "EDIT" in p.name
    ]
    assert edits, "expected at least one EDIT event"
    assert edits[-1]["data"]["fields"]["title"] == "Y"
    assert result.payload["local_id"] == "jira-dig-123"


def test_inbound_update_status_event_uses_previous_status_not_new(
    applier, mut_mod, fixture_repo
):
    """STATUS event's current_status must be the PREVIOUS state, not the new
    one (PR #375 review thread 3306949587). The reducer compares
    data['current_status'] against state['status'] to detect forks — setting
    current_status to the NEW state guarantees a false-positive fork mismatch
    whenever the ticket isn't already in that state.
    """
    # Seed via inbound create (no status -> stays at reducer default 'open').
    create_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-321",
        payload={"fields": {"summary": "S", "issuetype": "Task"}},
    )
    applier._apply_typed(create_mut, repo_root=fixture_repo)

    # Now inbound update transitions the Jira status to 'In Progress' ->
    # 'in_progress' locally (assuming config maps it; if config lacks the
    # entry, _jira_status_to_local returns 'open' and we still assert the
    # invariant: current_status != status when they differ).
    update_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-321",
        payload={"fields": {"status": "In Progress"}},
    )
    applier._apply_typed(update_mut, repo_root=fixture_repo)

    tracker = fixture_repo / ".tickets-tracker"
    status_events = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-321")
        if "STATUS" in p.name
    ]
    assert status_events, "expected a STATUS event from inbound update"
    latest = status_events[-1]["data"]
    # current_status must be the PREVIOUS state ('open' — seeded default),
    # NOT the new target. If new == previous (degenerate self-transition),
    # the two can coincide, but in this fixture they must differ.
    new_status = latest["status"]
    prev_status = latest["current_status"]
    assert prev_status == "open", (
        f"expected current_status='open' (prior state), got {prev_status!r}"
    )
    if new_status != "open":
        assert new_status != prev_status, (
            "current_status (previous state) must differ from status (new state)"
        )


# ---------------------------------------------------------------------------
# 3. inbound delete — four probe-outcome branches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    ["hard_delete", "redirect", "out_of_window", "trash"],
)
def test_inbound_delete_branches(applier, mut_mod, fixture_repo, outcome):
    # Seed the ticket dir.
    create_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-123",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._apply_typed(create_mut, repo_root=fixture_repo)

    payload = {"probe_outcome": outcome}
    if outcome == "redirect":
        payload["new_jira_key"] = "DIG-999"
    delete_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.delete,
        target="jira-dig-123",
        payload=payload,
    )
    result = applier._apply_typed(delete_mut, repo_root=fixture_repo)
    assert result.payload["branch"] == outcome

    tracker = fixture_repo / ".tickets-tracker"
    if outcome == "hard_delete":
        assert result.payload["follow_on"]["action"] == "create_after_hard_delete"
    elif outcome == "redirect":
        assert (tracker / "jira-dig-999").is_dir()
        assert not (tracker / "jira-dig-123").exists()
    else:
        # Comment-only branches: ticket dir still exists with a new COMMENT.
        events = _event_files(tracker, "jira-dig-123")
        assert any("COMMENT" in p.name for p in events)


def test_inbound_delete_redirect_raises_when_destination_exists(
    applier, mut_mod, fixture_repo
):
    """Redirect branch must NOT silently skip the rename when both src and
    dst already exist on disk (PR #375 review thread 3307104042). Silent
    skip leaves both directories present — an inconsistent state that
    propagates to later passes. Expect FileExistsError.
    """
    # Seed the source ticket dir (the one being redirected away).
    create_src = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-123",
        payload={"fields": {"summary": "src", "issuetype": "Task"}},
    )
    applier._apply_typed(create_src, repo_root=fixture_repo)

    # Pre-create the destination directory (simulates a prior failed pass
    # or collision with an already-imported ticket of the new key).
    tracker = fixture_repo / ".tickets-tracker"
    (tracker / "jira-dig-999").mkdir()

    delete_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.delete,
        target="jira-dig-123",
        payload={"probe_outcome": "redirect", "new_jira_key": "DIG-999"},
    )
    with pytest.raises(FileExistsError):
        applier._apply_typed(delete_mut, repo_root=fixture_repo)

    # Both directories must still exist — the leaf must not have partially
    # mutated state before raising.
    assert (tracker / "jira-dig-123").is_dir()
    assert (tracker / "jira-dig-999").is_dir()


# ---------------------------------------------------------------------------
# 4. inbound repair_property — wires through to inbound_repair_property
# ---------------------------------------------------------------------------


def test_inbound_repair_property_invokes_client(applier, mut_mod):
    client = MagicMock()
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.repair_property,
        target="DIG-X",
        payload={"local_id": "L"},
    )
    result = applier._apply_typed(mutation, client=client)
    client.set_issue_property.assert_called_once_with("DIG-X", "dso_local_id", "L")
    assert result.payload.get("status") == "ok"


# ---------------------------------------------------------------------------
# 5. inbound conflict — emits suppress_pair follow-on, files a bug
# ---------------------------------------------------------------------------


def test_inbound_conflict_emits_suppress_pair(
    applier, mut_mod, fixture_repo, monkeypatch
):
    # Make the bug-file subprocess a no-op so the test does not depend on the
    # ticket CLI being available inside the fixture tree.
    called = {}

    def fake_file_bug(cli_path, title, description, parent_id):
        called["title"] = title
        return "bug-id-1234"

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", fake_file_bug)

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-7",
        payload={"local_id": "jira-dig-7", "reason": "dual-write divergence"},
    )
    result = applier._apply_typed(mutation, repo_root=fixture_repo)
    follow_on = result.payload["follow_on"]
    assert follow_on["kind"] == "suppress_pair"
    assert follow_on["jira_key"] == "DIG-7"
    assert follow_on["local_id"] == "jira-dig-7"


def test_apply_honours_suppress_pair_drops_subsequent_inbound(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """reconcile_once → applier.apply contract: an inbound conflict on a pair
    must suppress later inbound mutations on the same pair in the same pass.
    """

    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", lambda *a, **k: "bug-1")
    _patch_apply_deps(applier, monkeypatch)

    # Seed the ticket dir so updates would otherwise apply.
    create = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-7",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._apply_typed(create, repo_root=fixture_repo)

    conflict = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-7",
        payload={"local_id": "jira-dig-7", "reason": "test"},
    )
    update_1 = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-7",
        payload={"fields": {"summary": "STOMP-1"}},
    )
    update_2 = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-7",
        payload={"fields": {"summary": "STOMP-2"}},
    )

    applier.apply(
        [conflict, update_1, update_2], pass_id="test-pass", repo_root=fixture_repo
    )

    # The two STOMP updates should NOT have produced EDIT events.
    tracker = fixture_repo / ".tickets-tracker"
    edits = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-7")
        if "EDIT" in p.name
    ]
    titles = [e["data"]["fields"].get("title", "") for e in edits]
    assert "STOMP-1" not in titles
    assert "STOMP-2" not in titles


def test_apply_honours_suppress_pair_drops_subsequent_inbound_via_computed_form(
    applier, mut_mod, fixture_repo, monkeypatch
):
    """Computed-form suppression contract (PR #375 review thread 3306949607):
    a suppress_pair on jira_key='DIG-7' must also drop later mutations whose
    target is the LOCAL-ID form of that key ('jira-dig-7'). Without the
    third match-arm, the later inbound update sneaks past.
    """
    monkeypatch.setattr(applier, "_file_conflict_bug_ticket", lambda *a, **k: "bug-1")
    _patch_apply_deps(applier, monkeypatch)

    # Seed the ticket dir so an EDIT could otherwise be written.
    create = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-7",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._apply_typed(create, repo_root=fixture_repo)

    # Conflict mutation targets the JIRA-key form...
    conflict = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.conflict,
        target="DIG-7",
        payload={"local_id": "", "reason": "test"},
    )
    # ...and the follow_on records local_id='' so only the jira_key arm
    # ('DIG-7') and its computed local-id form ('jira-dig-7') drive
    # suppression. The later mutation uses the computed-form target.
    later = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-7",
        payload={"fields": {"summary": "SHOULD-BE-DROPPED"}},
    )

    applier.apply([conflict, later], pass_id="test-pass", repo_root=fixture_repo)

    tracker = fixture_repo / ".tickets-tracker"
    edits = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-7")
        if "EDIT" in p.name
    ]
    titles = [e["data"]["fields"].get("title", "") for e in edits]
    assert "SHOULD-BE-DROPPED" not in titles


def test_inbound_create_dedups_against_binding_store(applier, mut_mod, fixture_repo):
    """ticket 1577: an inbound CREATE for a Jira key already bound in the binding
    store is deduped — mapping recorded, no phantom local ticket materialised.

    Covers the narrow transient the snapshot differ's 4354 label stand-down
    cannot: the fetched snapshot predates the dso-id:<local_id> label write-back,
    so the differ mis-emits an inbound CREATE, but bindings.json already records
    the binding. The applier-level guard catches it.
    """

    class _FakeBindingStore:
        def get_local_id(self, jira_key):
            return "uuid-bound-7" if jira_key == "DIG-77" else None

    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-77",
        payload={"fields": {"summary": "should NOT materialise", "issuetype": "Task"}},
    )
    result = applier._apply_typed(
        mutation, repo_root=fixture_repo, binding_store=_FakeBindingStore()
    )

    # 1. Result signals a dedup skip bound to the pre-existing local id.
    assert result.payload.get("dedup_skipped") is True
    assert result.payload.get("local_id") == "uuid-bound-7"

    # 2. mapping.json records uuid-bound-7 -> DIG-77.
    mapping_file = fixture_repo / "bridge_state" / "mapping.json"
    assert mapping_file.exists(), "dedup guard must write mapping.json"
    mapping = json.loads(mapping_file.read_text())
    assert mapping.get("uuid-bound-7") == "DIG-77"

    # 3. No phantom local ticket materialised under the jira-key-derived id.
    assert not (fixture_repo / ".tickets-tracker" / "jira-dig-77").exists(), (
        "must not materialise a phantom local ticket when already bound"
    )

    # Regression guard: an UNBOUND inbound create still materialises normally —
    # the stand-down is scoped to bound keys.
    unbound = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-88",
        payload={"fields": {"summary": "brand new", "issuetype": "Task"}},
    )
    applier._apply_typed(
        unbound, repo_root=fixture_repo, binding_store=_FakeBindingStore()
    )
    assert (fixture_repo / ".tickets-tracker" / "jira-dig-88").exists(), (
        "an unbound inbound create must still materialise a local ticket"
    )


# ---------------------------------------------------------------------------
# 6. Payload shape tolerance (Defect 1)
# ---------------------------------------------------------------------------


def test_inbound_create_accepts_flat_payload(applier, mut_mod, fixture_repo):
    """Differ emits top-level field keys (no nested 'fields' wrapper).
    _apply_inbound_create must accept both shapes.
    """
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-500",
        payload={
            "summary": "Flat payload title",
            "issuetype": "Task",
            "priority": 3,
            "status": "To Do",
        },
    )
    result = applier._apply_typed(mutation, repo_root=fixture_repo)
    tracker = fixture_repo / ".tickets-tracker"
    local_id = "jira-dig-500"
    create_ev = _read_create(tracker, local_id)
    assert create_ev["data"]["title"] == "Flat payload title"
    assert create_ev["data"]["ticket_type"] == "task"
    assert create_ev["data"]["priority"] == 3
    assert result.payload["local_id"] == local_id


def test_inbound_create_accepts_nested_fields_payload(applier, mut_mod, fixture_repo):
    """Batch-dict shape with nested 'fields' key must still work."""
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-501",
        payload={
            "fields": {
                "summary": "Nested payload title",
                "issuetype": "Bug",
                "priority": 1,
            },
        },
    )
    applier._apply_typed(mutation, repo_root=fixture_repo)
    tracker = fixture_repo / ".tickets-tracker"
    local_id = "jira-dig-501"
    create_ev = _read_create(tracker, local_id)
    assert create_ev["data"]["title"] == "Nested payload title"
    assert create_ev["data"]["ticket_type"] == "bug"


def test_inbound_update_accepts_flat_payload(applier, mut_mod, fixture_repo):
    """Differ emits top-level field keys for updates too."""
    # Seed ticket.
    create_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-502",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._apply_typed(create_mut, repo_root=fixture_repo)

    update_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-502",
        payload={"summary": "Updated flat"},
    )
    applier._apply_typed(update_mut, repo_root=fixture_repo)
    tracker = fixture_repo / ".tickets-tracker"
    edits = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-502")
        if "EDIT" in p.name
    ]
    assert edits
    assert edits[-1]["data"]["fields"]["title"] == "Updated flat"


# ---------------------------------------------------------------------------
# 7. Complex object extraction (Defect 2)
# ---------------------------------------------------------------------------


def test_inbound_create_extracts_nested_jira_objects(applier, mut_mod, fixture_repo):
    """Jira REST API returns issuetype/status/priority/assignee as nested dicts."""
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-600",
        payload={
            "summary": "Complex objects",
            "issuetype": {"name": "Bug", "id": "10002"},
            "status": {"name": "Done", "id": "10001"},
            "priority": {"name": "High", "id": "2"},
            "assignee": {"displayName": "Joe", "accountId": "abc123"},
        },
    )
    applier._apply_typed(mutation, repo_root=fixture_repo)
    tracker = fixture_repo / ".tickets-tracker"
    local_id = "jira-dig-600"
    create_ev = _read_create(tracker, local_id)
    data = create_ev["data"]
    assert data["ticket_type"] == "bug"
    assert data["title"] == "Complex objects"
    assert data["assignee"] == "Joe"
    # Priority name "High" is mapped to integer 1 via _JIRA_PRIORITY_MAP.
    assert data["priority"] == 1


def test_inbound_update_extracts_nested_jira_objects(applier, mut_mod, fixture_repo):
    """Same complex-object extraction must work for updates."""
    # Seed ticket.
    create_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-601",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._apply_typed(create_mut, repo_root=fixture_repo)

    update_mut = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target="jira-dig-601",
        payload={
            "summary": "Updated complex",
            "priority": {"name": "Low", "id": "4"},
            "assignee": {"displayName": "Alice"},
        },
    )
    applier._apply_typed(update_mut, repo_root=fixture_repo)
    tracker = fixture_repo / ".tickets-tracker"
    edits = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-601")
        if "EDIT" in p.name
    ]
    assert edits
    fields = edits[-1]["data"]["fields"]
    assert fields["title"] == "Updated complex"
    assert fields["priority"] == 3
    assert fields["assignee"] == "Alice"


# ---------------------------------------------------------------------------
# 8. Jira-side dedup write-back (Defect 3)
# ---------------------------------------------------------------------------


def test_inbound_create_writes_back_jira_dedup_markers(applier, mut_mod, fixture_repo):
    """After creating the local ticket, inbound_create must write dso-id label
    and dso_local_id property back to Jira so the differ recognizes the issue
    as mirrored on subsequent passes.
    """
    client = MagicMock()
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-700",
        payload={
            "summary": "Write-back test",
            "issuetype": "Task",
        },
    )
    result = applier._apply_typed(mutation, client=client, repo_root=fixture_repo)
    local_id = "jira-dig-700"
    assert result.payload["local_id"] == local_id
    client.add_label.assert_called_once_with("DIG-700", f"dso-id:{local_id}")
    client.set_entity_property.assert_called_once_with(
        "DIG-700", "dso_local_id", local_id
    )


@pytest.mark.parametrize(
    "raw_pri, expected",
    [
        (0, 0),
        (4, 4),
        (99, 2),    # out-of-range clamps to default
        (-1, 2),    # negative clamps to default
    ],
)
def test_inbound_create_clamps_out_of_range_integer_priority(
    applier, mut_mod, fixture_repo, raw_pri, expected
):
    """Integer priorities outside 0-4 must clamp to 2 (Medium)."""
    seq = 800 + raw_pri + 100  # unique per parametrize
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target=f"DIG-{seq}",
        payload={
            "summary": f"Priority clamp {raw_pri}",
            "issuetype": "Task",
            "priority": raw_pri,
        },
    )
    applier._apply_typed(mutation, repo_root=fixture_repo)
    local_id = f"jira-dig-{seq}"
    tracker = fixture_repo / ".tickets-tracker"
    create_ev = _read_create(tracker, local_id)
    assert create_ev["data"]["priority"] == expected


def test_inbound_create_no_writeback_without_client(applier, mut_mod, fixture_repo):
    """When client is None, no Jira write-back calls are attempted."""
    mutation = _make_mutation(
        mut_mod,
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target="DIG-701",
        payload={
            "summary": "No client test",
            "issuetype": "Task",
        },
    )
    # Should not raise -- no client calls attempted.
    result = applier._apply_typed(mutation, client=None, repo_root=fixture_repo)
    assert result.payload["local_id"] == "jira-dig-701"
