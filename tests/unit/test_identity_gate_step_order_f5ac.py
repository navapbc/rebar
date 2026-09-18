"""The authorship gate must not be pre-empted by its own pack-size budget.

Bug ``f5ac-391c-ec70-4fa9``: ``verify-identity.yml`` ran a blobless-checkout pack-size
budget as the job's second step, ahead of ``setup-python`` and the install. While the
``tickets`` event log kept the pack over budget, that step aborted the job before the
authorship verification ran at all, so the scheduled whole-store sweep produced no
authorship verdict on any run.

The remedy keeps the budget, its limit, and its fail-closed behaviour, and moves only the
point at which the job aborts: the pack is MEASURED immediately after checkout (before the
``tickets`` fetch enlarges the object database, which would change the quantity under
budget) and JUDGED in the job's final step.

These tests pin that ordering structurally. They parse the workflow rather than observing a
CI run, so they hold under any CI provider and under none.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "verify-identity.yml"
)

_AUTHORSHIP_STEP = "verify-identity (gating)"
_VERDICT_STEP = "Fail closed if the blobless checkout pack exceeds its limit"
_MEASURE_STEP = "Measure the blobless checkout pack"
_MOUNT_STEP = "Mount tickets branch as a worktree"


def _steps() -> list[dict[str, object]]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return list(document["jobs"]["verify-identity"]["steps"])


def _index_of(name: str) -> int:
    for position, step in enumerate(_steps()):
        if step.get("name") == name:
            return position
    raise AssertionError(f"no step named {name!r} in {WORKFLOW}")


def test_authorship_verification_runs_before_the_pack_budget_verdict() -> None:
    """The regression guard: the budget verdict must not pre-empt the authorship gate."""
    assert _index_of(_AUTHORSHIP_STEP) < _index_of(_VERDICT_STEP)


def test_pack_is_measured_before_the_tickets_branch_is_fetched() -> None:
    """Deferring the verdict must not change WHAT is measured.

    The ``tickets`` fetch adds the event log to the object database, so a measurement taken
    after it would be a different, much larger quantity than the budget was set against.
    """
    assert _index_of(_MEASURE_STEP) < _index_of(_MOUNT_STEP)


def test_the_pack_budget_verdict_reports_even_when_an_earlier_step_failed() -> None:
    """Both verdicts are wanted from one run, so the budget step is ``always()``."""
    verdict = _steps()[_index_of(_VERDICT_STEP)]
    assert "always()" in str(verdict.get("if", ""))


def test_the_pack_budget_is_still_fail_closed_on_an_overage() -> None:
    """The remedy moves the abort point; it does not soften the budget."""
    verdict = _steps()[_index_of(_VERDICT_STEP)]
    body = str(verdict["run"])
    assert "exit 1" in body
    assert "REBAR_CHECKOUT_PACK_LIMIT_KIB" in body


def test_an_unmeasured_pack_is_treated_as_a_failure_not_a_pass() -> None:
    """An empty measurement must not arithmetic-compare its way to a silent pass."""
    verdict = _steps()[_index_of(_VERDICT_STEP)]
    assert 'if [[ -z "${SIZE_PACK_KIB:-}" ]]; then' in str(verdict["run"])
