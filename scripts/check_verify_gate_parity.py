#!/usr/bin/env python3
"""CI drift-guard: the Gerrit ``Verified`` gate must depend on every job that gates main.

WHY THIS EXISTS
---------------
The Gerrit ``Verified`` vote is THE landable CI gate (AGENTS.md: "build/test/lint/
typecheck"). It is cast by ``.github/workflows/gerrit-verify.yaml``'s ``vote`` job,
which folds the run conclusion of the jobs in its ``needs`` into a single +1/-1
(via im-open/workflow-conclusion). The push/PR "mirror" lanes each define the
unconditional jobs that gate ``main`` post-merge.

If a job gates ``main`` post-merge but is ABSENT from ``vote.needs``, a change that
breaks it earns ``Verified +1`` pre-merge yet reddens ``main`` after it lands — the
green-verify / red-main hole (jira-reb-1163: ``artifact-probe`` and ``eval-discipline``
gated main but were never in ``vote.needs``). Nothing structurally forbade the drift.

This PARITY gate fails the build when ``vote.needs`` is NOT a superset of every
unconditional gating job across the mirror lanes below. Style mirrors the
prompt-index / server.json / criteria-routing drift gates in ``_build-and-test.yml``.

To fix a failure: add the reported job(s) to ``vote.needs`` in ``gerrit-verify.yaml``
(and make sure the job actually RUNS in that lane — e.g. by calling the shared reusable
workflow with the Gerrit patchset inputs, the way ``build-and-test`` / ``optionality``
do). If a job legitimately must not gate the Verified vote (e.g. it only runs on a
manual ``workflow_dispatch`` / ``schedule``, never on the push/PR critical path), add it
to ``EXCLUDED_JOBS`` below with a one-line justification.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# The push/PR lanes that mirror the Verified gate. Each self-describes as a mirror lane
# that casts NO Verified vote (gerrit-verify.yaml is the sole Verified source); every
# unconditional job here must therefore be aggregated into that vote via vote.needs.
# (Deliberately EXCLUDES independent gates that are not routed through Verified, e.g.
# codeql.yml — its own required-check — and the ops/infra guards.)
SOURCE_WORKFLOWS = [
    "test.yml",
    "optionality.yml",
    "verify-identity.yml",
    "prompt-eval.yml",
]

GERRIT_WORKFLOW = "gerrit-verify.yaml"
VOTE_JOB = "vote"

# Gerrit-lane-only plumbing jobs that exist ONLY in gerrit-verify.yaml (no push/PR
# counterpart), so they never need to appear in a source lane.
#   clear-vote     — resets Verified->0 at run start (GerriScary-safe).
#   require-ticket — fail-fast rebar-ticket trailer gate on the patchset.
#   classify       — trusted-main path classifier for docs-only vs full Verify.
#   docs-only      — the reduced lane that runs the shared documentation gates.
GERRIT_ONLY_JOBS = frozenset({"clear-vote", "require-ticket", "classify", "docs-only"})
REQUIRED_GERRIT_VOTE_JOBS = frozenset({"classify", "docs-only"})

# Jobs in the mirror lanes that DELIBERATELY do not gate the Verified vote because they
# never run on the push/PR critical path (manual workflow_dispatch / weekly schedule
# only) — mirroring the greppable, justified-ignore idiom of pip-audit's --ignore-vuln.
EXCLUDED_JOBS = frozenset(
    {
        # test.yml: live, billable Jira+LLM tier; manual dispatch + canonical-repo only.
        "external",
        # prompt-eval.yml: live paid eval tier; manual dispatch / weekly schedule only,
        # and non-blocking (continue-on-error). The blocking eval DISCIPLINE runs in
        # `eval-discipline`, which IS required in vote.needs.
        "eval-live",
        # prompt-eval.yml: live paid changed-rubric eval tier (story 5169). Runs on same-repo
        # PR/push but is non-blocking (continue-on-error: true), so it can never redden main —
        # a rubric-eval regression is a visible advisory finding, not a merge gate. The
        # blocking eval DISCIPLINE (offline, deterministic) runs in `eval-discipline`, which
        # IS required in vote.needs.
        "eval-changed-rubrics",
        # test.yml: the branch-health report (last known-green SHA + bisect recipe). It runs
        # ONLY on the 6-hourly schedule and on manual dispatch, never on the push/PR critical
        # path, and it GATES NOTHING — it describes a run that has already finished. Its whole
        # input is `needs.<gate>.result`, so there is nothing for the Verified vote to
        # aggregate (ticket 03ef-6fb5-158b-4abd).
        "main-health-report",
        # test.yml: the scheduled breadth sweep (epic 5664 S5 / Tier 2). Each of these runs
        # ONLY on the 6-hourly schedule / manual dispatch (guarded by
        # `if: github.event_name == 'schedule' || 'workflow_dispatch'`), never on the push/PR
        # critical path, and casts NO Verified vote — they add interpreter / platform /
        # lower-bound / doc-link breadth the hot-path gate deliberately omits for latency. A red
        # sweep cell is a visible finding for the CI owner, never a merge gate.
        "sweep-interpreters",
        "sweep-windows",
        "sweep-macos-full",
        "sweep-lower-bounds",
        "sweep-doc-links",
    }
)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: not a mapping (could not parse workflow)")
    return data


def gating_job_names(workflow: dict, excluded: frozenset[str] | set[str]) -> set[str]:
    """Every job in a source lane that must be aggregated into the Verified vote."""
    jobs = workflow.get("jobs") or {}
    return {name for name in jobs if name not in excluded}


def vote_needs(gerrit_workflow: dict, vote_job: str = VOTE_JOB) -> set[str]:
    """The set of jobs the Verified vote depends on."""
    jobs = gerrit_workflow.get("jobs") or {}
    job = jobs.get(vote_job) or {}
    needs = job.get("needs") or []
    if isinstance(needs, str):
        needs = [needs]
    return set(needs)


def missing_gating_jobs(
    gerrit_workflow: dict,
    source_workflows: list[dict],
    excluded: frozenset[str] | set[str],
) -> set[str]:
    """Gating jobs the Verified vote fails to depend on (vote.needs is not a superset)."""
    required: set[str] = set()
    for wf in source_workflows:
        required |= gating_job_names(wf, excluded)
    return required - vote_needs(gerrit_workflow)


def missing_required_gerrit_jobs(
    gerrit_workflow: dict,
    required: frozenset[str] | set[str] = REQUIRED_GERRIT_VOTE_JOBS,
) -> set[str]:
    """Gerrit route jobs whose result is absent from Verified aggregation."""
    return set(required) - vote_needs(gerrit_workflow)


def evaluate(
    gerrit_workflow: dict,
    source_workflows: list[dict],
    excluded: frozenset[str] | set[str],
    required_gerrit_jobs: frozenset[str] | set[str] = REQUIRED_GERRIT_VOTE_JOBS,
) -> int:
    """Return 0 on parity, 1 on drift (printing a GitHub-annotated diagnosis)."""
    missing = missing_gating_jobs(gerrit_workflow, source_workflows, excluded)
    missing |= missing_required_gerrit_jobs(gerrit_workflow, required_gerrit_jobs)
    if missing:
        print(
            "::error::gerrit-verify.yaml vote.needs is NOT a superset of the jobs that "
            "gate main — a change breaking one would earn Verified +1 yet redden main."
        )
        print(f"  MISSING from vote.needs (gate main but not the Verified vote): {sorted(missing)}")
        print("  Add each to the `vote` job's `needs` in gerrit-verify.yaml AND ensure the")
        print("  job actually runs in that lane (call the shared reusable with the Gerrit")
        print("  patchset inputs, as build-and-test/optionality do). The classifier and")
        print("  docs-only route are Gerrit-only but still mandatory in vote.needs.")
        print("  If a job must NOT gate")
        print("  Verified, add it to EXCLUDED_JOBS in this script with a justification.")
        return 1
    covered = vote_needs(gerrit_workflow) - GERRIT_ONLY_JOBS
    print(f"Verified-gate parity: OK (vote.needs covers all {len(covered)} gating jobs).")
    return 0


def load_real() -> tuple[dict, list[dict]]:
    """Load the committed gerrit-verify workflow and the source mirror lanes."""
    gerrit = load_yaml(WORKFLOWS / GERRIT_WORKFLOW)
    sources = [load_yaml(WORKFLOWS / name) for name in SOURCE_WORKFLOWS]
    return gerrit, sources


def main() -> int:
    gerrit, sources = load_real()
    return evaluate(gerrit, sources, EXCLUDED_JOBS)


if __name__ == "__main__":
    sys.exit(main())
