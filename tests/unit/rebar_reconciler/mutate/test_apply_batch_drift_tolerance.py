"""Regression test for Bug f058: HEAD drift tolerates benign external writers.

Bug context:
  `_apply_batch` pins the `tickets` orphan-branch HEAD before its mutation
  loop and re-checks on each iteration. If HEAD advances, it raises
  HeadDriftError to abort the pass. The d822 fix (PR #425) removed the
  in-process `_file_conflict_bug_ticket` subprocess as a commit source.
  But the drift detector still aborts on EXTERNAL writers — e.g., a
  parallel Claude session running `dso ticket transition <id> closed`
  triggers an auto-compact (ticket-transition.sh) which commits
  `ticket: COMPACT <id>` to the tickets branch. The reconciler aborts
  mid-pass even though the external commit doesn't conflict with the
  in-flight mutations.

  Empirical confirmation (ADVANCED-tier historical investigator):
  drift SHAs `78392cd6→19da05f6` in field-probe-unassigned-1779984990
  matched a `ticket: COMPACT 6d43-a70d-871c-4973` commit emitted by a
  sibling Claude session — NOT a probe-created ticket.

Fix:
  Replace the unconditional raise with a tolerance check. When the
  intervening commit's subject matches benign external patterns
  (`ticket:`, `suggestion:`, `acquire lock`, `release lock`), refresh
  `head_pin` and continue. Only raise HeadDriftError if the subject
  doesn't match benign patterns (which would indicate a competing
  reconciler pass — the original concern the detector was built for).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_drift_tolerance", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_drift_tolerance"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def test_drift_is_benign_helper_exists(applier):
    """After the fix, applier.py must expose a `_drift_is_benign(subject)`
    helper used by `_apply_batch`'s drift detector to distinguish external
    benign writers (ticket-CLI auto-commits) from competing reconciler
    passes."""
    assert hasattr(applier, "_drift_is_benign"), (
        "After the fix, applier.py must expose a `_drift_is_benign(subject)` "
        "helper that classifies commit subjects as benign external writers "
        "or competing reconciler activity."
    )


def test_drift_tolerates_ticket_compact(applier):
    """The most common external drift trigger: parallel session running
    `dso ticket transition <id> closed` → auto-compact via
    ticket-transition.sh:594 → `ticket: COMPACT <id>` commit."""
    assert applier._drift_is_benign("ticket: COMPACT 6d43-a70d-871c-4973") is True


def test_drift_tolerates_ticket_status(applier):
    """Parallel session transitioning a ticket emits `ticket: STATUS`."""
    assert applier._drift_is_benign("ticket: STATUS abc-1234") is True


def test_drift_tolerates_ticket_create(applier):
    """A parallel session creating a new ticket emits `ticket: CREATE`."""
    assert applier._drift_is_benign("ticket: CREATE def-456") is True


def test_drift_tolerates_ticket_edit_comment_delete(applier):
    """All ticket-CLI write subcommands emit `ticket: <VERB>` commits."""
    assert applier._drift_is_benign("ticket: EDIT abc-1234") is True
    assert applier._drift_is_benign("ticket: COMMENT abc-1234") is True
    assert applier._drift_is_benign("ticket: DELETE abc-1234") is True
    assert applier._drift_is_benign("ticket: SNAPSHOT abc-1234") is True


def test_drift_tolerates_suggestion_record(applier):
    """The suggestion subsystem emits `suggestion: RECORD` commits."""
    assert applier._drift_is_benign("suggestion: RECORD") is True


def test_drift_tolerates_acquire_release_lock(applier):
    """Other reconciler passes acquiring/releasing their pass lock emit
    `acquire lock pass_id=...` / `release lock pass_id=...` commits.
    These are benign in the sense that they don't represent competing
    mutations to in-flight target tickets."""
    assert applier._drift_is_benign("acquire lock pass_id=xyz") is True
    assert applier._drift_is_benign("release lock pass_id=xyz") is True


def test_drift_rejects_competing_pass_record(applier):
    """A competing reconciler outbound write commits `pass_record: ...`.
    These represent actual concurrent reconciliation — the original
    intent of the drift detector. Must NOT be silenced."""
    assert applier._drift_is_benign("pass_record: 2026-05-28T16-31-54") is False


def test_drift_rejects_unknown_subjects(applier):
    """Unrecognized commit subjects (developer manual edits, hostile
    writes, etc.) must NOT be silently tolerated."""
    assert applier._drift_is_benign("feat: some manual commit") is False
    assert applier._drift_is_benign("fix: something") is False
    assert applier._drift_is_benign("") is False
    assert applier._drift_is_benign("WIP debugging") is False


def test_apply_batch_uses_drift_is_benign(applier):
    """The drift detector inside `_apply_batch` must consult
    `_drift_is_benign` before raising HeadDriftError. Without this, a
    benign external `ticket: COMPACT` commit during a reconciler pass
    aborts the pass (the bug f058 symptom).

    This test reads the applier source to verify the structural wiring —
    a behavioral integration test against the real `_apply_batch` loop
    would require a full git tickets-branch repo fixture, which is
    captured separately. The structural check ensures the helper is
    actually USED, not just defined.
    """
    import inspect

    src = inspect.getsource(applier._apply_batch)
    assert "_drift_is_benign" in src, (
        "After the fix, _apply_batch must call _drift_is_benign before "
        "raising HeadDriftError. The helper exists but isn't wired into "
        "the drift detector — the bug is not fixed."
    )
