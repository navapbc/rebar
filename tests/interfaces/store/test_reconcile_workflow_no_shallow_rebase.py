"""The Jira reconciler workflows must reconcile the tickets branch with FULL history
+ merge, never shallow + rebase.

Regression guard for bug saggy-pupil-plant / f193 (RC1). A shallow (``--depth=1``)
tickets history defeats git's merge-base computation; reconciling a compaction
(which deletes source event files) with ``git rebase`` then re-applies the stale
worktree over the compaction and resurrects the deleted files —
SNAPSHOT_INCONSISTENT corruption. The controlled experiment showed only
shallow+rebase corrupts; full+merge is clean. There is no in-process seam for a
GitHub Actions workflow, so this asserts the operative config invariants directly.

Covers BOTH reconciler workflows (primary + canary) — both mount and push the
tickets branch and both had the defect.
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


@pytest.mark.parametrize("workflow", RECONCILE_WORKFLOWS, ids=lambda p: p.name)
def test_reconcile_uses_merge_not_rebase(workflow: Path) -> None:
    """Reconciliation of origin/tickets must use ``git merge``, never ``git
    rebase`` (rebase over a remote compaction resurrects deleted event files)."""
    text = workflow.read_text(encoding="utf-8")
    rebase_hits = re.findall(r"git rebase[^\n]*origin/tickets", text)
    assert not rebase_hits, (
        f"{workflow.name} must reconcile with 'git merge', not 'git rebase "
        f"origin/tickets' (bug f193); found:\n{rebase_hits}"
    )
    assert "git merge --no-edit origin/tickets" in text, (
        f"{workflow.name} should reconcile the tickets branch with "
        "'git merge --no-edit origin/tickets'"
    )
