"""Story d6bd (convergence rollout retired → hardcoded always-on): _diff_fields
ALWAYS arbitrates against the per-binding baseline (get_baseline) instead of
prev_snapshot. The former ``reconciler.baseline_consumer_swap`` flag is gone; the
always-on behavior is what the flag=True path used to do (this project ran both
rollout flags ``true`` in prod), so these tests pin the flags-true == hardcoded
equivalence (AC6) and the cold-start observability.

Pinned asymmetry: ``summary`` (local ``title``) is NOT in ``_INBOUND_MIRRORED_FIELDS``,
so Site-A direction-suppression never fires for it — a local title edit is always
emitted.
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
    return _load("outbound_differ_baseline_arb", DIFFER_PATH)


class _FakeStore:
    """Minimal binding_store: get_baseline plus the confirmed/pending predicates
    the cold-start diagnostic consults."""

    def __init__(self, baseline, *, bound: bool = True, pending: bool = False):
        self._baseline = baseline
        self._bound = bound
        self._pending = pending

    def get_baseline(self, local_id):
        return self._baseline

    def is_bound(self, local_id):
        return self._bound

    def is_pending(self, local_id):
        return self._pending


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


# NOTE (story d6bd): the former ``test_flag_off_ignores_baseline_byte_for_byte``
# exercised the flag=OFF prev_snapshot-arbitration path, which no longer exists —
# removed. Arbitration is now unconditionally baseline-based.


def test_conflict_keys_on_baseline(differ):
    """Both-sides-conflict (Site B) is arbitrated against the baseline: local and
    Jira both differ from the baseline ancestor -> conflict recorded."""
    store = _FakeStore({"description": "D"})  # true ancestor: neither side matches
    conflict_sink: list[tuple[str, str]] = []
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-2",
        prev_jira_fields={"description": "local-edit"},  # would NOT conflict if consulted
        conflict_sink=conflict_sink,
        local_id="loc-1",
    )
    assert ("KEY-2", "description") in conflict_sink


def test_dryrun_arbitration_pinned_to_baseline(differ):
    """AC6 dry-run zero-diff pin: with a baseline present that DIFFERS from
    prev_jira_fields, the arbitration outcome must be the BASELINE one (the
    flags-true behavior this project ran in prod). Direction-suppression fires only
    because the baseline (not prev_snapshot) is consulted as ``arbitration_prev``.

    local.description == baseline.description, Jira.description differs, and
    prev_jira_fields.description is STALE (!= local). If arbitration used
    prev_snapshot the description would be EMITTED; using the baseline it is
    SUPPRESSED so the inbound differ mirrors Jira's edit. Any behavior change
    (e.g. reverting to prev_snapshot arbitration) flips this pinned decision.
    """
    store = _FakeStore({"description": "local-edit"})
    changed = differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-3",
        prev_jira_fields={"description": "STALE"},  # differs from baseline+local
        local_id="loc-1",
    )
    assert "description" not in changed, "baseline is the arbitration ancestor (Jira wins)"


def test_summary_asymmetry_never_suppressed(differ):
    """Pinned: ``summary`` is not in _INBOUND_MIRRORED_FIELDS, so a local title edit
    is ALWAYS emitted even when the local title == the baseline summary."""
    store = _FakeStore({"summary": "SAME", "description": "D"})
    changed = differ._diff_fields(
        _ticket(title="SAME", description="D"),  # local title == baseline summary
        _jira(summary="jira-changed", description="D"),
        binding_store=store,
        jira_key="KEY-4",
        prev_jira_fields={"summary": "SAME"},
        local_id="loc-1",
    )
    assert "summary" in changed, "summary never direction-suppressed (title/summary asymmetry)"


def test_none_baseline_local_wins(differ):
    """get_baseline returns None (no ancestor recorded): local-wins — the changed
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
    )
    assert changed.get("description") == "local-edit"  # local-wins
    assert ("KEY-5", "description") not in conflict_sink  # no ancestor -> no conflict


# --- Cold-start observability (story d6bd) -------------------------------------


def test_cold_start_recon_fires_for_confirmed_none_baseline(differ, capsys):
    """A CONFIRMED binding whose baseline is still None is in the warm-up window:
    emit exactly one ``RECON: baseline_cold_start local_id=<id>`` line to stderr."""
    store = _FakeStore(None, bound=True, pending=False)
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-6",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    err = capsys.readouterr().err
    assert "RECON: baseline_cold_start local_id=loc-1" in err
    assert err.count("baseline_cold_start") == 1  # once per binding per pass


def test_cold_start_recon_silent_once_baseline_exists(differ, capsys):
    """No cold-start line once a baseline exists for the binding."""
    store = _FakeStore({"description": "D"}, bound=True, pending=False)
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-7",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    assert "baseline_cold_start" not in capsys.readouterr().err


def test_cold_start_recon_silent_for_pending_binding(differ, capsys):
    """A still-PENDING binding (not yet confirmed) is not counted as cold-start."""
    store = _FakeStore(None, bound=True, pending=True)
    differ._diff_fields(
        _ticket(description="local-edit"),
        _jira(description="jira-edit"),
        binding_store=store,
        jira_key="KEY-8",
        prev_jira_fields=None,
        local_id="loc-1",
    )
    assert "baseline_cold_start" not in capsys.readouterr().err
