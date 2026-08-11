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
    """Every ``git fetch`` of the tickets branch must name its destination ref (bug 35f7).

    A bare ``git fetch origin tickets`` always writes ``FETCH_HEAD`` but writes
    ``refs/remotes/origin/tickets`` only OPPORTUNISTICALLY — when the remote's CONFIGURED
    refspec covers that branch. The workflow mount consumes the result as
    ``origin/tickets``; delivery now delegates to the core push entrypoint, whose own
    real-git suite pins the same explicit destination-ref contract. A narrow configured
    refspec must not leave either consumer reading an absent or stale ref.

    ``actions/checkout`` builds its workspace with ``git remote add``, which installs the
    wildcard ``+refs/heads/*:refs/remotes/origin/*``, so these workflows are not currently
    exposed. That is an implementation detail of a third-party action, not a guarantee this
    repo controls — naming the destination ref makes the fetch correct without depending on
    it, and matches the form already used to mount the worktree. Same defect class as
    ``_store/sync.py`` (bug 5546) and ``_store/push.py`` (bug 35f7).
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
