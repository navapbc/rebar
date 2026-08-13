"""NO workflow runs on a push to the ``tickets`` branch (ticket refractive-conceptual-sunbear).

``tickets`` is rebar's own event log, and the store **auto-pushes it on every write** — a
comment, an edit, a claim, a close. It carries no code. So any workflow that triggers on a
push to that branch turns each ticket event into a CI run: during a 2026-08-12/13 bug bash
that starved the org-wide GitHub Actions pool (navapbc is on GitHub Free — 20 concurrent
jobs, 5 macOS, shared across every repo in the org) and queued a Gerrit ``Verified`` pytest
matrix roughly two hours behind tickets-push runs.

The operator directive is absolute: *"The tickets branch should never trigger CI."* Anything
that must read the store runs on a **schedule** instead — reconcile-bridge every 20 minutes,
verify-identity every 6 hours, and so on — so this change removes triggers, never cadence.

The sibling module :mod:`tests.unit.test_ci_main_push_policy` pins the same class of policy
for ``main``, but against a hard-coded list of workflow NAMES. That shape cannot catch a
workflow added *later* with a bare ``push:`` key. This module therefore **enumerates the
workflow directory from disk**, so a new file inherits the invariant automatically.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"

# The branch whose pushes must never reach CI.
_TICKETS = "tickets"

# The workflows that keep push CI for ordinary topic branches. A scratch-branch push to the
# mirror is the ONLY pre-merge validation lane for a workflow edit — the Gerrit `Verified`
# lane resolves `uses: ./.github/workflows/...` from the DEFAULT branch under
# workflow_dispatch, so it cannot exercise a change to a workflow file.
_PUSH_LANES = ("test.yml", "optionality.yml", "prompt-eval.yml", "verify-identity.yml")

# Schedules that carry the function the removed triggers used to provide. Pinned literally so
# a future "simplification" cannot quietly widen the store's worst-case sync latency.
_UNCHANGED_CRONS = {
    "reconcile-bridge.yml": ["*/20 * * * *"],
    "verify-identity.yml": ["53 */6 * * *"],
    "test.yml": ["41 */6 * * *"],
    "optionality.yml": ["47 */6 * * *"],
    "prompt-eval.yml": ["0 7 * * 1"],
}


def _workflow_files() -> list[Path]:
    files = sorted(p for p in _WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))
    assert files, f"no workflows found under {_WORKFLOWS}"
    return files


def _on(path: Path) -> dict[str, Any]:
    """The workflow's ``on:`` block.

    PyYAML resolves the bare key ``on`` to the boolean ``True`` (YAML 1.1 truthy), so read
    both spellings rather than assuming either.
    """
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    block = data.get("on", data.get(True))
    assert isinstance(block, dict), f"{path.name} has no mapping-shaped `on:` block"
    return block


def _push_matches_branch(on_block: dict[str, Any], branch: str) -> bool:
    """Would a push to ``refs/heads/<branch>`` trigger a workflow with this ``on:`` block?

    Models GitHub's documented branch filtering: no ``push`` key => never; ``push`` with
    neither filter => EVERY branch; ``branches`` => allow-list; ``branches-ignore`` =>
    deny-list. The "neither filter" case is the one that makes a newly added workflow a
    regression by default, which is why this module enumerates the directory.
    """
    push = on_block.get("push", "__absent__")
    if push == "__absent__":
        return False
    push = push or {}
    allow = push.get("branches")
    deny = push.get("branches-ignore")
    if allow is not None:
        return any(fnmatch.fnmatch(branch, pattern) for pattern in allow)
    if deny is not None:
        return not any(fnmatch.fnmatch(branch, pattern) for pattern in deny)
    return True


# --- AC1: nothing anywhere runs on a tickets-branch push ----------------------------------


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_no_workflow_runs_on_a_push_to_the_tickets_branch(path: Path) -> None:
    assert not _push_matches_branch(_on(path), _TICKETS), (
        f"{path.name} triggers on a push to refs/heads/{_TICKETS}. That branch is rebar's "
        "auto-pushed event log — every comment would become a CI run. Add it to "
        "`branches-ignore` (the idiom used repo-wide) or drop the push trigger; if this "
        "workflow must read the store, put it on a `schedule:` instead "
        "(ticket refractive-conceptual-sunbear)."
    )


def test_the_filter_model_would_catch_a_regression() -> None:
    """The guard above is only meaningful if these two shapes are classified as matching."""
    assert _push_matches_branch({"push": None}, _TICKETS), "a bare `push:` matches every branch"
    assert _push_matches_branch({"push": {"branches": [_TICKETS]}}, _TICKETS)
    assert _push_matches_branch({"push": {"branches-ignore": ["main"]}}, _TICKETS)


# --- AC5: ordinary push CI survives -------------------------------------------------------


@pytest.mark.parametrize("name", _PUSH_LANES)
def test_push_ci_for_ordinary_topic_branches_is_preserved(name: str) -> None:
    on_block = _on(_WORKFLOWS / name)
    assert _push_matches_branch(on_block, "wip-some-topic"), (
        f"{name} must still run on a push to a scratch/topic branch — that push is the only "
        "pre-merge validation lane a workflow change has"
    )
    for excluded in ("feature/big-thing", "main"):
        assert not _push_matches_branch(on_block, excluded), (
            f"{name} must still skip {excluded!r} (ticket 03ef-6fb5-158b-4abd)"
        )


# --- AC3: the replacement cadences are untouched ------------------------------------------


@pytest.mark.parametrize(("name", "crons"), sorted(_UNCHANGED_CRONS.items()))
def test_scheduled_coverage_is_unchanged(name: str, crons: list[str]) -> None:
    schedule = _on(_WORKFLOWS / name).get("schedule") or []
    assert [entry["cron"] for entry in schedule] == crons, (
        f"{name}'s schedule changed. Dropping the tickets-branch push trigger leaves the "
        "schedule as the only remaining coverage, so it must not be weakened alongside it"
    )
