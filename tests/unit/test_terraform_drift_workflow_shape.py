"""Structural guards on the Terraform Drift workflow's shape.

Two bugs, one shape: **absence of execution reported as an outcome that reads like health.**

``chiselled-glandulous-fantail`` (bug ``fd2b-7af3-3d6e-48cf``)
    ``terraform fmt -check`` ran as the FIRST step of the drift ``plan`` job. ``fmt -check``
    exits 3 on any formatting deviation, so a cosmetic whitespace difference aborted the job
    and ``terraform plan`` NEVER RAN. Drift detection was off for two days (runs of 22s and
    29s against a healthy 6m07s) while an entire declared-but-unapplied EBS volume and two
    CloudWatch alarms sat unreported. The run was red, but red for a nit — nothing
    distinguished "formatting is wrong" from "we have not looked for drift in 48 hours".

``jaded-pugnacious-isopod`` (bug ``eebc-6aa1-e45b-4325``)
    ``terraform plan`` from a tree behind ``origin/main`` reports "No changes" — byte-identical
    to converged infrastructure — because the declaration it would have flagged is not in the
    tree. The reverse is worse: a tree that PREDATES a deletion proposes RECREATING the deleted
    resource. The drift workflow inherits this exposure whole: it fails on a non-empty plan, so
    it is only meaningful against current ``main``.

These tests pin the structural fixes so a later edit cannot silently restore either failure:
formatting must not sit upstream of the plan, and the plan must be preceded by an assertion
that the checked-out tree is the tree the answer is supposed to be about.

Pure-Python and offline — no terraform binary, no AWS, no CI provider (``project.portability``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "terraform-drift.yml"

CURRENCY_GATE = "check_terraform_tree_currency.py"


def _workflow() -> dict[str, Any]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{_WORKFLOW} did not parse as a mapping"
    return doc


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _workflow().get("jobs") or {}
    assert jobs, "terraform-drift.yml declares no jobs"
    return jobs


def _runs(step: dict[str, Any]) -> str:
    return str(step.get("run") or "")


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _is_plan(step: dict[str, Any]) -> bool:
    run = _runs(step)
    return "terraform plan" in run


def _is_fmt(step: dict[str, Any]) -> bool:
    return "terraform fmt" in _runs(step)


def _is_currency_assert(step: dict[str, Any]) -> bool:
    return CURRENCY_GATE in _runs(step)


def _job_containing_plan() -> tuple[str, dict[str, Any]]:
    for name, job in _jobs().items():
        if any(_is_plan(step) for step in _steps(job)):
            return name, job
    raise AssertionError("no job in terraform-drift.yml runs `terraform plan`")


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs") or []
    if isinstance(needs, str):
        return {needs}
    return {str(n) for n in needs}


def test_a_job_still_runs_terraform_plan() -> None:
    """The whole point of the workflow: something must actually plan."""
    name, _job = _job_containing_plan()
    assert name


def test_no_fmt_check_shares_the_plan_job(  # bug fd2b-7af3 AC1/AC4
) -> None:
    """A formatting defect must not be able to abort the drift plan.

    Any ``terraform fmt`` step in the SAME job as the plan can fail that job before the plan
    step is reached — which is exactly what blinded drift detection for two days.
    """
    name, job = _job_containing_plan()
    offenders = [str(s.get("name") or _runs(s)) for s in _steps(job) if _is_fmt(s)]
    assert not offenders, (
        f"job '{name}' runs both `terraform fmt` and `terraform plan`: {offenders}. "
        "A formatting-only defect would abort the job before the plan runs (bug fd2b-7af3); "
        "keep fmt in an independent job."
    )


def test_plan_job_is_not_gated_on_a_formatting_job(  # bug fd2b-7af3 AC1
) -> None:
    """`needs:` on a formatting job would reinstate the blocking order across jobs."""
    plan_name, plan_job = _job_containing_plan()
    jobs = _jobs()
    fmt_jobs = {n for n, j in jobs.items() if any(_is_fmt(s) for s in _steps(j))}
    blocking = _needs(plan_job) & fmt_jobs
    assert not blocking, (
        f"job '{plan_name}' declares needs={sorted(blocking)}, which are formatting jobs; "
        "a formatting failure would again prevent the drift plan from running (bug fd2b-7af3)"
    )


def test_formatting_is_still_checked_and_still_fails_loudly(  # bug fd2b-7af3 AC2
) -> None:
    """Un-gating formatting must not mean dropping it: some job must still run fmt -check,
    and it must not swallow its own failure with ``continue-on-error``."""
    fmt_steps = [
        (name, step) for name, job in _jobs().items() for step in _steps(job) if _is_fmt(step)
    ]
    assert fmt_steps, "no `terraform fmt` check survives anywhere in terraform-drift.yml"
    assert any("-check" in _runs(step) for _name, step in fmt_steps), (
        "`terraform fmt` is present but never with `-check`; it would rewrite files rather "
        "than report the defect"
    )
    for job_name, step in fmt_steps:
        assert not step.get("continue-on-error"), (
            f"the fmt step in job '{job_name}' sets continue-on-error, so a formatting "
            "defect would no longer be surfaced at all (bug fd2b-7af3 AC2)"
        )


def test_formatting_and_drift_report_as_distinct_jobs(  # bug fd2b-7af3 AC3
) -> None:
    """A reader must tell "whitespace is wrong" from "we did not look for drift" WITHOUT
    opening logs — i.e. from the job-level outcomes alone."""
    plan_name, _plan_job = _job_containing_plan()
    fmt_jobs = {n for n, j in _jobs().items() if any(_is_fmt(s) for s in _steps(j))}
    assert fmt_jobs, "no job runs `terraform fmt`"
    assert plan_name not in fmt_jobs, (
        "formatting and drift report under the same job name, so their failures are "
        "indistinguishable in the checks list (bug fd2b-7af3 AC3)"
    )


def test_currency_assertion_precedes_the_plan(  # bug eebc-6aa1 AC3
) -> None:
    """The plan job must PROVE it is planning the current tracked tree before it plans.

    Without this the workflow can plan a stale ref and report "no drift" for a tree that
    simply lacks the declaration — the failure mode bug eebc-6aa1 records.
    """
    name, job = _job_containing_plan()
    steps = _steps(job)
    currency = [i for i, s in enumerate(steps) if _is_currency_assert(s)]
    plan = [i for i, s in enumerate(steps) if _is_plan(s)]
    assert currency, (
        f"job '{name}' never invokes {CURRENCY_GATE}; nothing asserts the checked-out tree "
        "is the tracked branch tip, so a stale ref would report 'no drift' (bug eebc-6aa1)"
    )
    assert min(currency) < min(plan), (
        f"job '{name}' runs {CURRENCY_GATE} only AFTER `terraform plan`; the assertion must "
        "gate the plan, not follow it"
    )


def test_currency_assertion_is_not_advisory(  # bug eebc-6aa1 AC1/AC2
) -> None:
    """A staleness check that cannot fail the job is the same silent success it replaces."""
    _name, job = _job_containing_plan()
    for step in _steps(job):
        if _is_currency_assert(step):
            assert not step.get("continue-on-error"), (
                "the tree-currency assertion sets continue-on-error, so planning a stale "
                "tree would once again be reported as success (bug eebc-6aa1)"
            )


def test_plan_job_checks_out_enough_history_for_the_currency_check(  # bug eebc-6aa1 AC2
) -> None:
    """The assertion compares HEAD against the freshly fetched remote tip; a shallow
    checkout has no merge base to compare with, which would degrade the check to
    'cannot establish currency' on every run."""
    name, job = _job_containing_plan()
    checkouts = [s for s in _steps(job) if "actions/checkout" in str(s.get("uses") or "")]
    assert checkouts, f"job '{name}' has no actions/checkout step"
    depths = {str((s.get("with") or {}).get("fetch-depth")) for s in checkouts}
    assert "0" in depths, (
        f"job '{name}' checks out with fetch-depth={sorted(depths)}; the tree-currency "
        "assertion needs full history (fetch-depth: 0) to compute a merge base"
    )
