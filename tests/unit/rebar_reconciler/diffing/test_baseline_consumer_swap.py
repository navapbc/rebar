"""Story a118 (Phase-3 consumer swap): _diff_fields arbitrates against the
per-binding baseline (get_baseline) instead of prev_snapshot when
`baseline_consumer_swap=True`.

Direction-preservation (RED-first): when local == baseline (local did NOT change
from the last-synced ancestor) but Jira changed, the outbound is SUPPRESSED — Jira's
edit is preserved. This holds ONLY under the swap using the baseline as ancestor;
with the flag OFF the differ is byte-for-byte the prev_snapshot path.

Pinned asymmetry: `summary` (local `title`) is NOT in `_INBOUND_MIRRORED_FIELDS`, so
Site-A direction-suppression never fires for it — a local title edit is always emitted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return _load("outbound_differ_baseline_swap", DIFFER_PATH)


class _FakeStore:
    """Minimal binding_store exposing only get_baseline (all _diff_fields needs here)."""

    def __init__(self, baseline):
        self._baseline = baseline

    def get_baseline(self, local_id):
        return self._baseline


def _ticket(**ov) -> dict:
    t = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    t.update(ov)
    return t


def _jira(**ov) -> dict:
    f = {
        "summary": "T",
        "description": "D",
        "issuetype": {"name": "Task"},
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    f.update(ov)
    return f


def test_flag_off_ignores_baseline_byte_for_byte(differ):
    """Flag OFF: get_baseline is NEVER consulted; arbitration uses prev_jira_fields."""
    store = _FakeStore({"description": "BASELINE-DESC"})  # would change the outcome if consulted
    conflict_sink: list[tuple[str, str]] = []
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-1",
        prev_jira_fields={"description": "local-edit"},  # OFF ancestor: local == prev
        conflict_sink=conflict_sink,
        local_id="loc-1",
        baseline_consumer_swap=False,
    )
    # local-wins pushes the description outbound regardless; but the CONFLICT sink keys
    # on the ancestor: local == prev ("local-edit") -> _local_matches_prev True -> NO conflict.
    assert ("KEY-1", "description") not in conflict_sink


def test_flag_on_conflict_keys_on_baseline(differ):
    """Flag ON: the both-sides-conflict (Site B) is arbitrated against the baseline."""
    store = _FakeStore({"description": "D"})  # true ancestor: neither side matches it
    conflict_sink: list[tuple[str, str]] = []
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-2",
        prev_jira_fields={"description": "local-edit"},  # OFF would suppress the conflict
        conflict_sink=conflict_sink,
        local_id="loc-1",
        baseline_consumer_swap=True,
    )
    # baseline "D": local "local-edit" != D AND jira "jira-edit" != D -> both-sides conflict.
    assert ("KEY-2", "description") in conflict_sink


def test_direction_preservation_local_equals_baseline_suppressed(differ):
    """Site A: when local == baseline (unchanged) but Jira changed, the field is
    SUPPRESSED (not emitted) — Jira's edit is direction-preserved."""
    store = _FakeStore({"description": "D"})
    changed = differ._diff_fields(
        _ticket(description="D"),  # local == baseline
        _jira(description="jira-edit"),  # Jira changed
        binding_store=store,
        jira_key="KEY-3",
        prev_jira_fields={"description": "STALE"},  # OFF ancestor would NOT suppress
        local_id="loc-1",
        baseline_consumer_swap=True,
    )
    assert "description" not in changed, "local==baseline must suppress the outbound (Jira wins)"


def test_summary_asymmetry_never_suppressed(differ):
    """Pinned: `summary` is not in _INBOUND_MIRRORED_FIELDS, so a local title edit is
    ALWAYS emitted even when local title == baseline summary."""
    store = _FakeStore({"summary": "SAME", "description": "D"})
    changed = differ._diff_fields(
        _ticket(title="SAME", description="D"),  # local title == baseline summary
        _jira(summary="jira-changed", description="D"),
        binding_store=store,
        jira_key="KEY-4",
        prev_jira_fields={"summary": "SAME"},
        local_id="loc-1",
        baseline_consumer_swap=True,
    )
    assert "summary" in changed, "summary is never direction-suppressed (title/summary asymmetry)"


def test_none_baseline_local_wins(differ):
    """Flag ON but get_baseline returns None (no ancestor): local-wins — the changed
    field is emitted and NO both-sides conflict is recorded."""
    store = _FakeStore(None)
    conflict_sink: list[tuple[str, str]] = []
    changed = differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-5",
        prev_jira_fields={"description": "prev"},
        conflict_sink=conflict_sink,
        local_id="loc-1",
        baseline_consumer_swap=True,
    )
    assert changed.get("description") == "local-edit"  # local-wins
    assert ("KEY-5", "description") not in conflict_sink  # no ancestor -> no conflict


def test_outbound_diff_config_flag_defaults_off_and_carries(differ):
    """OutboundDiffConfig carries the baseline_consumer_swap flag (default off)."""
    assert differ.OutboundDiffConfig().baseline_consumer_swap is False
    assert differ.OutboundDiffConfig(baseline_consumer_swap=True).baseline_consumer_swap is True
