"""Unit tests for local_label_intent.py (bug a06c).

The intent-set helper reads a ticket's event directory and returns the
union of every tag that ever appeared in:

* a CREATE event's ``data.tags``
* an EDIT event's ``data.fields.tags``
* a SNAPSHOT event's ``data.compiled_state.tags``

That union is the "ever-seen" set used by the outbound differ to gate
REMOVE emissions (Approach 1, decision bug a06c).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCAL_INTENT_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "local_label_intent.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def local_label_intent() -> ModuleType:
    return _load_module("local_label_intent", LOCAL_INTENT_PATH)


def _write_event(ticket_dir: Path, event: dict) -> None:
    ticket_dir.mkdir(parents=True, exist_ok=True)
    # Filenames sort by timestamp prefix per the v3 schema; preserve
    # write order with monotonic nanosecond stamps.
    fname = f"{event['timestamp']}-{event['uuid']}-{event['event_type']}.json"
    (ticket_dir / fname).write_text(json.dumps(event), encoding="utf-8")
    # Ensure unique timestamps for ordering across multiple events
    time.sleep(0.001)


def _make_event(event_type: str, data: dict) -> dict:
    return {
        "timestamp": time.time_ns(),
        "uuid": str(uuid.uuid4()),
        "event_type": event_type,
        "env_id": "test-env",
        "author": "test",
        "data": data,
    }


def test_unions_tags_across_create_and_edits(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """CREATE.data.tags ∪ EDIT.data.fields.tags ∪ ... = intent set."""
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "ticket-1"
    _write_event(ticket_dir, _make_event("CREATE", {"tags": ["initial"]}))
    _write_event(ticket_dir, _make_event("EDIT", {"fields": {"tags": ["initial", "added"]}}))
    _write_event(ticket_dir, _make_event("EDIT", {"fields": {"tags": ["initial"]}}))  # removed "added"

    result = local_label_intent.compute_label_intent_set("ticket-1", tracker)

    assert result == {"initial", "added"}


def test_snapshot_compiled_state_tags_included(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """SNAPSHOT events fold older history; their compiled_state.tags must
    be unioned in (otherwise tags removed pre-snapshot disappear from
    the intent set — false negatives for legitimate REMOVEs)."""
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "ticket-2"
    _write_event(
        ticket_dir,
        _make_event(
            "SNAPSHOT",
            {
                "compiled_state": {"tags": ["from-snap-a", "from-snap-b"]},
                "source_event_uuids": [],
            },
        ),
    )
    _write_event(ticket_dir, _make_event("EDIT", {"fields": {"tags": ["from-snap-a"]}}))

    result = local_label_intent.compute_label_intent_set("ticket-2", tracker)

    assert "from-snap-a" in result
    assert "from-snap-b" in result


def test_edit_events_without_tags_field_ignored(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """EDIT events that modify other fields (status, priority) without a
    tags entry must not contribute to the intent set."""
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "ticket-3"
    _write_event(ticket_dir, _make_event("CREATE", {"tags": ["only-tag"]}))
    _write_event(ticket_dir, _make_event("EDIT", {"fields": {"priority": 1}}))
    _write_event(ticket_dir, _make_event("EDIT", {"fields": {"status": "in_progress"}}))

    result = local_label_intent.compute_label_intent_set("ticket-3", tracker)

    assert result == {"only-tag"}


def test_missing_ticket_directory_returns_empty_set(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """Lazy first-pass safety: nonexistent ticket dir -> empty set
    (which downstream gates as 'suppress all REMOVEs')."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()

    result = local_label_intent.compute_label_intent_set("nonexistent", tracker)

    assert result == set()


def test_inbound_origin_edit_excluded_from_intent(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """EDIT events with ``data.source == "inbound"`` reflect Jira side
    mutations applied by the reconciler, not user intent. They must
    NOT contribute to the intent set — otherwise a subsequent Jira-side
    REMOVE is cancelled by a spurious outbound ADD (T4 IB-REMOVE
    regression). The applier writes ``data.source = "inbound"`` on
    every inbound labels EDIT (see applier._apply_inbound_update).
    """
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "ticket-5"
    _write_event(ticket_dir, _make_event("CREATE", {"tags": ["labelprobe"]}))
    # User-intent EDIT
    _write_event(
        ticket_dir, _make_event("EDIT", {"fields": {"tags": ["labelprobe", "user-added"]}})
    )
    # Inbound-origin EDIT — must be excluded
    _write_event(
        ticket_dir,
        _make_event(
            "EDIT",
            {
                "fields": {"tags": ["labelprobe", "user-added", "ib-added"]},
                "source": "inbound",
            },
        ),
    )

    result = local_label_intent.compute_label_intent_set("ticket-5", tracker)

    assert "labelprobe" in result
    assert "user-added" in result
    assert "ib-added" not in result, (
        "inbound-origin EDIT leaked into intent set"
    )


def test_malformed_event_json_skipped_not_raised(
    local_label_intent: ModuleType, tmp_path: Path
) -> None:
    """A corrupt event file must not crash the intent computation —
    failure mode is 'skip the bad event', not 'abort the pass'."""
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "ticket-4"
    _write_event(ticket_dir, _make_event("CREATE", {"tags": ["ok"]}))
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "9999999999999999999-bad-EDIT.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    result = local_label_intent.compute_label_intent_set("ticket-4", tracker)

    assert result == {"ok"}
