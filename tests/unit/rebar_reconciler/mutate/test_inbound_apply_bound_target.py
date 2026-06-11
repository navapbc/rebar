"""Tests for `_apply_inbound_update` field/target pipeline (bug 1bb2-5da5).

Behaviour under test (observable via on-disk events):

  1. When payload includes ``local_id`` (set by reconcile.py for bound
     tickets), the EDIT event is written under that local UUID directory —
     NOT a ``jira-dig-NNN/`` directory derived from ``mutation.target``.
     This prevents duplicate ticket creation + silent data loss when the
     reconciler updates a bound UUID ticket from Jira.
  2. The applier accepts the differ's LOCAL-keyed field shape
     (``title``, ``ticket_type``) — the inbound differ has already mapped
     Jira → local names. Both the new local-keyed names AND the legacy
     ``summary`` (Jira-keyed) name are accepted for back-compat.
  3. ADF (Atlassian Document Format) description dicts are normalized to
     plain text before being written. Without normalization, the EDIT
     event would carry the raw ADF dict and the reducer would store the
     description as a `dict`, not a string.
  4. When ``status`` arrives already mapped to a local value
     (``"open"`` / ``"in_progress"`` / ``"done"`` / ``"closed"``), the
     applier does NOT re-run the Jira→local mapper (which would double-
     map e.g. ``"in_progress"`` → empty/garbage).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture(autouse=True)
def _reset_ticket_reducer_module_cache(applier):
    yield
    # The ticket-reducer lazy cache now lives in inbound_translate (its owner);
    # reset it at point-of-use rather than on the applier facade.
    from rebar_reconciler import inbound_translate

    inbound_translate._TICKET_REDUCER_MODULE = None


def _make_mutation(applier, target: str, payload: dict):
    mut_mod = applier._load_mutation_module()
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.update,
        target=target,
        payload=payload,
        provenance={"source": "test", "jira_key": target},
    )


def _read_events(ticket_dir: Path) -> list[dict]:
    events = []
    for ef in sorted(ticket_dir.glob("*.json")):
        events.append(json.loads(ef.read_text(encoding="utf-8")))
    return events


# ---------------------------------------------------------------------------
# 1. EDIT event written under bound UUID, not jira-dig-* derived from target
# ---------------------------------------------------------------------------


def test_edit_written_under_bound_local_uuid_not_jira_target(tmp_path, applier):
    """When payload.local_id is a UUID (bound ticket), the EDIT event is
    written under <tracker>/<uuid>/, not <tracker>/jira-dig-9999/."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "abcd1234-5678-9012-3456-7890abcdef00"

    # Pre-create the bound ticket directory so the test is realistic.
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-9999",
        payload={
            "local_id": local_uuid,
            "fields": {
                "title": "Updated title",
                "description": "Updated body",
                "priority": 1,
                "ticket_type": "task",
            },
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    # Affirmative: events landed under the bound UUID dir.
    bound_events = _read_events(tracker_dir / local_uuid)
    assert bound_events, (
        f"Expected EDIT event under bound UUID dir {local_uuid}, found none. "
        f"Tracker contents: {sorted(p.name for p in tracker_dir.iterdir())}"
    )
    edit_events = [e for e in bound_events if e.get("event_type") == "EDIT"]
    assert len(edit_events) == 1, (
        f"Expected exactly 1 EDIT event under bound UUID dir, got "
        f"{[e.get('event_type') for e in bound_events]}"
    )

    # Negative: the Jira-key-derived dir was NOT created (the duplicate-
    # ticket / silent-data-loss symptom).
    jira_derived = tracker_dir / "jira-dig-9999"
    assert not jira_derived.exists(), (
        f"_apply_inbound_update wrote to jira-key-derived dir {jira_derived} "
        "instead of the bound UUID — this is the duplicate-ticket bug."
    )


# ---------------------------------------------------------------------------
# 2. Differ-emitted local field keys are honoured
# ---------------------------------------------------------------------------


def test_edit_fields_use_local_keyed_shape_from_differ(tmp_path, applier):
    """The inbound differ emits ``title`` (not ``summary``) and ``ticket_type``.
    The applier must write both into the EDIT event."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "11111111-2222-3333-4444-555555555555"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1001",
        payload={
            "local_id": local_uuid,
            "fields": {
                "title": "X",
                "description": "Y",
                "priority": 1,
                "ticket_type": "story",
            },
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit = next(e for e in events if e.get("event_type") == "EDIT")
    fields = edit["data"]["fields"]
    assert fields.get("title") == "X", (
        f"Expected title='X' in EDIT data.fields, got {fields}"
    )
    assert fields.get("description") == "Y"
    assert fields.get("priority") == 1
    assert fields.get("ticket_type") == "story", (
        f"Expected ticket_type='story' (differ-emitted local key) to be "
        f"forwarded, got {fields}"
    )


def test_legacy_summary_key_still_accepted_for_backcompat(tmp_path, applier):
    """A legacy caller passing the Jira-keyed ``summary`` (not ``title``) must
    still produce an EDIT with ``title`` set — back-compat for callers that
    bypass the differ."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1002",
        payload={
            "local_id": local_uuid,
            "fields": {"summary": "Legacy summary"},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit = next(e for e in events if e.get("event_type") == "EDIT")
    assert edit["data"]["fields"].get("title") == "Legacy summary"


# ---------------------------------------------------------------------------
# 3. ADF description is normalized to plain text
# ---------------------------------------------------------------------------


def test_adf_description_dict_normalized_to_plain_text(tmp_path, applier):
    """A description supplied as an ADF dict must be written as plain text."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "12121212-3434-5656-7878-909090909090"
    (tracker_dir / local_uuid).mkdir()

    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
    }

    mutation = _make_mutation(
        applier,
        target="DIG-1003",
        payload={
            "local_id": local_uuid,
            "fields": {"description": adf},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit = next(e for e in events if e.get("event_type") == "EDIT")
    desc = edit["data"]["fields"].get("description")
    assert isinstance(desc, str), (
        f"Expected description normalized to str, got {type(desc).__name__}: {desc!r}"
    )
    assert "hello" in desc, f"Expected normalized text to contain 'hello', got {desc!r}"


# ---------------------------------------------------------------------------
# 4. Local-mapped status is NOT double-mapped
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Inbound labels — payload['labels'] applied as EDIT(fields.tags) (bug 57b0)
# ---------------------------------------------------------------------------


def _seed_create_event(tracker_dir: Path, ticket_id: str, tags: list[str]) -> None:
    """Seed a minimal CREATE event so reduce_ticket returns a usable state."""
    import time
    import uuid as _uuid

    (tracker_dir / ticket_id).mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    event = {
        "timestamp": ts,
        "uuid": str(_uuid.uuid4()),
        "event_type": "CREATE",
        "env_id": "test-env",
        "author": "test",
        "data": {
            "id": ticket_id,
            "ticket_type": "task",
            "title": "seed",
            "description": "",
            "parent_id": "",
            "tags": tags,
        },
    }
    fname = f"{ts}-{event['uuid']}-CREATE.json"
    (tracker_dir / ticket_id / fname).write_text(
        json.dumps(event, ensure_ascii=False), encoding="utf-8"
    )


def test_inbound_label_add_writes_edit_with_new_tag(tmp_path, applier):
    """payload['labels']=[{action:add,label:X}] must write an EDIT event whose
    fields.tags includes X alongside the pre-existing tags."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "57b00001-0000-0000-0000-000000000001"
    _seed_create_event(tracker_dir, local_uuid, ["existing-tag"])

    mutation = _make_mutation(
        applier,
        target="DIG-5701",
        payload={
            "local_id": local_uuid,
            "fields": {},
            "labels": [{"action": "add", "label": "new-tag"}],
        },
    )
    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit_events = [e for e in events if e.get("event_type") == "EDIT"]
    assert edit_events, (
        f"Expected an EDIT event for the label add, got events "
        f"{[e.get('event_type') for e in events]}"
    )
    tags_written = None
    for e in edit_events:
        f = e.get("data", {}).get("fields", {})
        if "tags" in f:
            tags_written = f["tags"]
            break
    assert tags_written is not None, (
        f"No EDIT event carried fields.tags; EDIT events: {edit_events}"
    )
    assert "new-tag" in tags_written, (
        f"Expected 'new-tag' in EDIT.fields.tags, got {tags_written}"
    )
    assert "existing-tag" in tags_written, (
        f"Expected pre-existing 'existing-tag' preserved in EDIT.fields.tags, "
        f"got {tags_written}"
    )


def test_inbound_label_remove_writes_edit_without_removed_tag(tmp_path, applier):
    """payload['labels']=[{action:remove,label:X}] must write an EDIT event
    whose fields.tags omits X but preserves other pre-existing tags."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "57b00002-0000-0000-0000-000000000002"
    _seed_create_event(tracker_dir, local_uuid, ["keep-me", "drop-me"])

    mutation = _make_mutation(
        applier,
        target="DIG-5702",
        payload={
            "local_id": local_uuid,
            "fields": {},
            "labels": [{"action": "remove", "label": "drop-me"}],
        },
    )
    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit_events = [e for e in events if e.get("event_type") == "EDIT"]
    assert edit_events, (
        f"Expected an EDIT event for the label remove, got events "
        f"{[e.get('event_type') for e in events]}"
    )
    tags_written = None
    for e in edit_events:
        f = e.get("data", {}).get("fields", {})
        if "tags" in f:
            tags_written = f["tags"]
            break
    assert tags_written is not None, (
        f"No EDIT event carried fields.tags; EDIT events: {edit_events}"
    )
    assert "drop-me" not in tags_written, (
        f"Expected 'drop-me' removed from EDIT.fields.tags, got {tags_written}"
    )
    assert "keep-me" in tags_written, (
        f"Expected 'keep-me' preserved in EDIT.fields.tags, got {tags_written}"
    )


def test_inbound_label_add_does_not_wipe_tags_when_reducer_fails(
    tmp_path, applier, monkeypatch
):
    """Bug bc8f-775e-9a34-44d1: when the reducer raises (or returns None) the
    labels-apply block previously fell back to an empty current_tags list and
    wrote an EDIT whose fields.tags contained ONLY the newly-added label —
    wiping ALL pre-existing tags. Reproduces the live probe symptom where
    ticket b2e9 lost its 'labelprobe-...' tag after T1's bidirectional pass.

    Fix: when current_tags cannot be reliably read, the labels EDIT must
    NOT be written; the next reconciler pass will retry.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "bc8f0001-0000-0000-0000-000000000001"
    _seed_create_event(tracker_dir, local_uuid, ["existing-tag"])

    # Simulate the failure mode observed in the live probe: the reducer
    # raises mid-apply (e.g., transient I/O error, malformed event during
    # concurrent write). Patch the reducer module's reduce_ticket to raise.
    reducer_mod = applier._load_ticket_reducer()
    monkeypatch.setattr(
        reducer_mod,
        "reduce_ticket",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("simulated reducer failure")),
    )

    mutation = _make_mutation(
        applier,
        target="DIG-BC8F",
        payload={
            "local_id": local_uuid,
            "fields": {},
            "labels": [{"action": "add", "label": "new-tag"}],
        },
    )
    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit_events = [e for e in events if e.get("event_type") == "EDIT"]
    # The bug: an EDIT with fields.tags=['new-tag'] (wiping 'existing-tag')
    # was being written. Assert NO tags-EDIT was written under reducer failure.
    for e in edit_events:
        f = e.get("data", {}).get("fields", {})
        if "tags" in f:
            # If a tags-EDIT IS written, it must preserve existing-tag.
            assert "existing-tag" in f["tags"], (
                f"REGRESSION (bc8f): labels-apply wrote EDIT.fields.tags={f['tags']} "
                f"without pre-existing 'existing-tag' — this wipes local tags. "
                f"Fix: skip the labels EDIT when current_tags cannot be read."
            )


def test_inbound_label_add_does_not_wipe_tags_when_ticket_dir_missing(
    tmp_path, applier
):
    """Companion to bc8f RED test: when the ticket dir doesn't exist yet
    (e.g., race with concurrent CREATE), reduce_ticket returns None and the
    pre-fix code wrote an EDIT containing ONLY the new label. The fix must
    not write such an EDIT, since there are no pre-existing tags to preserve
    AND the directory itself doesn't exist — the next pass will retry once
    the CREATE has landed.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "bc8f0002-0000-0000-0000-000000000002"
    # NOTE: do NOT seed a CREATE event — directory doesn't exist.

    mutation = _make_mutation(
        applier,
        target="DIG-BC8F2",
        payload={
            "local_id": local_uuid,
            "fields": {},
            "labels": [{"action": "add", "label": "new-tag"}],
        },
    )
    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    # No CREATE seed, no tags-EDIT should be written (it would be unsafe —
    # the EDIT would land in a not-yet-existing ticket dir and the next
    # CREATE pass would reduce it without the differ's prior context).
    ticket_dir = tracker_dir / local_uuid
    if ticket_dir.exists():
        events = _read_events(ticket_dir)
        tag_edits = [
            e for e in events
            if e.get("event_type") == "EDIT"
            and "tags" in e.get("data", {}).get("fields", {})
        ]
        assert not tag_edits, (
            f"Expected NO tags-EDIT when ticket dir is unseeded (reducer "
            f"returns None), but got: {tag_edits}"
        )


def test_inbound_label_noop_when_labels_empty(tmp_path, applier):
    """When payload['labels'] is empty/absent, no extra EDIT for tags is written.

    Guard against spurious EDIT events on every inbound pass.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "57b00003-0000-0000-0000-000000000003"
    _seed_create_event(tracker_dir, local_uuid, ["tag-a"])

    mutation = _make_mutation(
        applier,
        target="DIG-5703",
        payload={
            "local_id": local_uuid,
            "fields": {},  # no scalar field changes either
            "labels": [],
        },
    )
    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit_events = [e for e in events if e.get("event_type") == "EDIT"]
    # Pre-existing CREATE event is fine; the assertion is that no new tags-EDIT
    # was written (the labels list is empty).
    for e in edit_events:
        f = e.get("data", {}).get("fields", {})
        assert "tags" not in f, (
            f"Unexpected tags-EDIT event written for empty labels list: {e}"
        )


def test_status_already_local_not_double_mapped(tmp_path, applier):
    """When the differ supplies status='in_progress' (already local-mapped),
    the applier must write that exact value — not re-run it through
    _jira_status_to_local (which would turn it into '' / garbage)."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1004",
        payload={
            "local_id": local_uuid,
            "fields": {"status": "in_progress"},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    status_events = [e for e in events if e.get("event_type") == "STATUS"]
    assert len(status_events) == 1, (
        f"Expected 1 STATUS event, got {[e.get('event_type') for e in events]}"
    )
    assert status_events[0]["data"].get("status") == "in_progress", (
        f"Expected status='in_progress' (no double-map), got {status_events[0]['data']}"
    )


# ---------------------------------------------------------------------------
# 5. Finding 3 — title takes precedence over summary when both are present
# ---------------------------------------------------------------------------


def test_title_takes_precedence_over_summary(tmp_path, applier):
    """When a payload contains BOTH ``title`` (differ-emitted) and ``summary``
    (legacy back-compat key), the EDIT event must use ``title`` — not
    ``summary``. The differ is the source of truth; the legacy key is only
    honoured when ``title`` is absent."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "cccccccc-dddd-eeee-ffff-000011112222"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1005",
        payload={
            "local_id": local_uuid,
            "fields": {
                "title": "New title from differ",
                "summary": "Legacy summary value",
            },
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    edit = next(e for e in events if e.get("event_type") == "EDIT")
    assert edit["data"]["fields"].get("title") == "New title from differ", (
        f"Expected title='New title from differ' (title wins over summary), "
        f"got {edit['data']['fields']}"
    )


# ---------------------------------------------------------------------------
# 6. Finding 2 / 4 — type-based status guard (not value-membership)
# ---------------------------------------------------------------------------


def test_status_dict_shape_invokes_mapper(tmp_path, applier):
    """A status arriving as a dict (Jira-shaped, e.g. ``{"name": "In Progress"}``)
    is from a legacy/back-compat caller that bypassed the differ. The applier
    must invoke the Jira→local mapper for the dict case."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "33333333-4444-5555-6666-777777777777"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1006",
        payload={
            "local_id": local_uuid,
            "fields": {"status": {"name": "In Progress"}},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    status_events = [e for e in events if e.get("event_type") == "STATUS"]
    assert len(status_events) == 1
    # The mapper should produce a non-empty local status; the exact value
    # depends on config.local_to_jira_status. Critical assertion: it is
    # a string and not the raw dict.
    written = status_events[0]["data"].get("status")
    assert isinstance(written, str), (
        f"Expected mapped string status for dict input, got {type(written).__name__}: {written!r}"
    )


def test_status_unknown_string_value_trusts_differ(tmp_path, applier):
    """A string status that is NOT in the local-set (e.g. 'pending') must
    still be trusted as-is on the typed-mutation path — the type guard
    (str → trust, dict → map) replaces the value-membership heuristic so
    that a Jira tenant with a custom-named status like 'pending' cannot
    accidentally trigger double-mapping back to ''."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "88888888-9999-aaaa-bbbb-cccccccccccc"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1007",
        payload={
            "local_id": local_uuid,
            "fields": {"status": "pending"},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    status_events = [e for e in events if e.get("event_type") == "STATUS"]
    assert len(status_events) == 1
    assert status_events[0]["data"].get("status") == "pending", (
        f"Expected 'pending' written verbatim (trust differ contract on "
        f"typed-mutation path), got {status_events[0]['data']}"
    )


def test_status_empty_string_trusts_differ_no_double_map(tmp_path, applier):
    """An empty string status from the differ is written as-is (no fallback
    through _jira_status_to_local which would coerce '' → 'open')."""
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    local_uuid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
    (tracker_dir / local_uuid).mkdir()

    mutation = _make_mutation(
        applier,
        target="DIG-1008",
        payload={
            "local_id": local_uuid,
            "fields": {"status": ""},
        },
    )

    applier._apply_inbound_update(mutation, client=None, repo_root=tmp_path)

    events = _read_events(tracker_dir / local_uuid)
    status_events = [e for e in events if e.get("event_type") == "STATUS"]
    assert len(status_events) == 1
    assert status_events[0]["data"].get("status") == "", (
        f"Expected '' written verbatim (type-based guard trusts string "
        f"input from differ), got {status_events[0]['data']}"
    )
