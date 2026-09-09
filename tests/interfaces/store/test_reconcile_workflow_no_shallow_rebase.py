"""Require Jira reconciler workflows to use full tickets history and merge.

A shallow fetch defeats merge-base calculation, allowing rebase to replay stale events
over compaction and resurrect deleted files. Both primary and canary workflows therefore
fetch full history and delegate merge-based delivery.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
RECONCILE_WORKFLOWS = [
    _WORKFLOW_DIR / "reconcile-bridge.yml",
    _WORKFLOW_DIR / "reconcile-bridge-canary.yml",
]


@pytest.mark.parametrize("workflow", RECONCILE_WORKFLOWS, ids=lambda p: p.name)
def test_no_shallow_fetch_of_tickets(workflow: Path) -> None:
    """No shallow fetch anywhere in a reconciler workflow — neither an explicit
    ``--depth=<n>`` on ``git fetch`` nor a shallow ``fetch-depth:`` on the checkout
    action. Both leave the tickets history shallow and defeat merge-base."""
    text = workflow.read_text(encoding="utf-8")
    offenders = [
        ln
        for ln in text.splitlines()
        if re.search(r"--depth=\d", ln)
        or re.search(r"fetch-depth:\s*[1-9]", ln)  # any positive depth is shallow; 0 = full
    ]
    assert not offenders, (
        f"{workflow.name} must not shallow-fetch the tickets branch "
        f"(shallow history defeats merge-base → SNAPSHOT_INCONSISTENT); found:\n{offenders}"
    )
    assert re.search(r"fetch-depth:\s*0", text), (
        f"{workflow.name} checkout must use 'fetch-depth: 0' (full history)"
    )


@pytest.mark.parametrize(
    ("workflow", "delegate"),
    [
        (RECONCILE_WORKFLOWS[0], "rebar bridge run"),
        (RECONCILE_WORKFLOWS[1], "python -m rebar._store.push"),
    ],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_reconcile_delegates_merge_not_rebase_to_supported_seam(
    workflow: Path, delegate: str
) -> None:
    """Workflow delivery delegates to the merge-based core and never rebases."""
    text = workflow.read_text(encoding="utf-8")
    rebase_hits = re.findall(r"git rebase[^\n]*origin/tickets", text)
    assert not rebase_hits, (
        f"{workflow.name} must reconcile with 'git merge', not 'git rebase "
        f"origin/tickets' (bug f193); found:\n{rebase_hits}"
    )
    assert delegate in text, (
        f"{workflow.name} must delegate tickets reconvergence through {delegate!r}; "
        "the executable runner/core suite owns merge-vs-rebase behavior"
    )


@pytest.mark.parametrize("workflow", RECONCILE_WORKFLOWS, ids=lambda p: p.name)
def test_tickets_fetch_always_names_the_destination_ref(workflow: Path) -> None:
    """Require every tickets fetch to write an explicit destination ref.

    ``FETCH_HEAD`` alone does not guarantee ``origin/tickets`` under a narrow configured
    refspec, yet mounting and delivery read that tracking ref. Explicit source/destination
    refspecs keep both workflows independent of checkout's wildcard configuration.
    """
    text = workflow.read_text(encoding="utf-8")
    offenders = [
        ln.strip()
        for ln in text.splitlines()
        if re.search(r"git fetch\s+\S+\s+tickets(\s|$|\s*[|2>])", ln) and "refs/remotes/" not in ln
    ]
    assert not offenders, (
        f"{workflow.name} bare-fetches the tickets branch and then consumes it as "
        "'origin/tickets'; use the explicit "
        '"+tickets:refs/remotes/origin/tickets" refspec instead (bug 35f7). Found:\n'
        + "\n".join(offenders)
    )
