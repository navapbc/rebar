"""Round-trip a real scrubbed store copy through the Dockerized DC harness (J11, epic e369).

Unlike empty-store convergence, this proves real tickets and links move both directions.
Isolation is asserted: outer and tracker repos have no remotes, sync push is off, and inherited
Cloud credentials are absent. The copied tickets branch lives under ``.tickets-tracker`` where
the reconciler reads it; shared fixtures and helpers prevent sibling-test drift.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

# Imported by bare name: pytest inserts this directory on sys.path (there is no `__init__.py`
# anywhere under `tests/`), which is also why `_dc_support` is not a dotted path.
from _child_diag import assert_child_ran_clean
from _dc_support import CLOUD_CREDENTIAL_VARS, live_jira_ready, read_inherited_env
from _dc_support import envelope as _envelope
from _dc_support import is_ticket_entry as _is_ticket_entry
from _dc_support import run_reconcile as _run_reconcile
from _dc_support import seed_searchable_issue as _seed_searchable_issue
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# Re-export under the name that marks this module live and rejects all-skipped evidence.
_live_jira_ready = live_jira_ready

# ---------------------------------------------------------------------------
# Isolation — the precondition for everything below it
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_the_working_repo_is_isolated_from_this_project(dc_store_copy_repo: Path) -> None:
    """All three isolation layers, asserted together because they defend one thing.

    Deliberately NOT asserted via `sync.remote`, which defaults to "origin" whether or not
    that remote exists — reading it would prove nothing about where a push could actually go.
    """
    # BOTH repos, and the tracker is the one that actually matters: it is the store, so it is
    # what `sync.remote` would push. Checking only the outer repo would leave the real hazard
    # unasserted while looking thorough.
    for repo, what in (
        (dc_store_copy_repo, "the working repo"),
        (dc_store_copy_repo / ".tickets-tracker", "the STORE repo"),
    ):
        remotes = subprocess.run(
            ["git", "remote"], cwd=repo, text=True, capture_output=True, check=True
        ).stdout.strip()
        assert remotes == "", (
            f"{what} has git remote(s) {remotes!r} — a store write here could push into this "
            "project's real tickets branch"
        )
    # Fixture regression guard: this proves the fixture still disables pushes, not what the
    # inherited job environment supplied (bug 59b2, Finding A).
    assert os.environ.get("REBAR_SYNC_PUSH") == "off", (
        "the dc_store_copy_repo fixture no longer sets REBAR_SYNC_PUSH=off — a store write from "
        "this copy could push"
    )

    # Check the snapshot captured before the fixture stripped credentials; current os.environ
    # would be circular evidence.
    inherited = read_inherited_env(dc_store_copy_repo)
    leaked = {
        name: value for name, value in inherited.items() if name in CLOUD_CREDENTIAL_VARS and value
    }
    assert not leaked, (
        f"the JOB environment supplied real-Jira credentials/URLs {sorted(leaked)} — the fixture "
        f"strips them from this copy's environment, so this run is safe, but their presence means "
        f"a sibling job or a future cell that does NOT use dc_store_copy_repo could reach a real "
        f"instance. Names checked: {list(CLOUD_CREDENTIAL_VARS)}"
    )


@_skip
@_skip_no_extra
def test_the_store_copy_is_complete_and_scrubbed(dc_store_copy_repo: Path) -> None:
    """The copy is REAL (count matches the source) and carries no bindings.

    Counting against the source rather than asserting a bare `> 0` is what catches a PARTIAL
    extraction — the failure a floor check waves through. And the count is read from the
    filesystem, NOT from the pass's `scanned` number: `scanned` is `len(curr_snapshot)`, the
    count of REMOTE Jira issues, which says nothing about the local store.
    """
    tracker = dc_store_copy_repo / ".tickets-tracker"
    copied = {p.name for p in tracker.iterdir() if _is_ticket_entry(p.name)}
    expected = set(json.loads((dc_store_copy_repo / ".j11-expected-entries.json").read_text()))

    assert copied, "the store copy is EMPTY — extraction landed somewhere the reconciler cannot see"
    assert copied == expected, (
        f"the store copy does not match the branch: {len(copied)} entries vs {len(expected)}; "
        f"missing {sorted(expected - copied)[:5]}; unexpected {sorted(copied - expected)[:5]}"
    )
    survivors = sorted(str(p.relative_to(tracker)) for p in tracker.rglob(".bridge_state*"))
    assert survivors == [], f"binding/snapshot artifacts survived the scrub: {survivors}"


# ---------------------------------------------------------------------------
# The thin vertical slice — one round-trip each way, over the real store copy
# ---------------------------------------------------------------------------


@_skip
@_skip_no_extra
def test_the_inbound_create_is_PLANNED_for_a_new_dc_issue(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """Prove fetch and differ plan a new DC issue independently of apply.

    Use an unfiltered dry-run because the post-filter could hide the create, while an
    unfiltered writing pass would create scrubbed production tickets in the harness.
    """
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = _seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 slice — planned"
    )
    local_id = _jira_key_to_local_id(key)

    cp = _run_reconcile(dc_store_copy_repo, "dry-run")
    plan = _envelope(cp).get("plan", [])
    inbound_creates = [
        e for e in plan if e.get("direction") == "inbound" and e.get("action") == "create"
    ]
    mine = [
        e for e in inbound_creates if key in str(e.get("target")) or e.get("local_id") == local_id
    ]

    assert mine, (
        f"the differ planned NO inbound create for {key} even though the issue is searchable. "
        f"inbound creates planned: {len(inbound_creates)}; plan size: {len(plan)}. "
        f"stderr:\n{cp.stderr[-2000:]}"
    )


@_skip
@_skip_no_extra
def test_a_dc_issue_reaches_the_local_store_inbound(
    dc_store_copy_repo: Path, dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """INBOUND round-trip over the real store copy: an issue created in DC appears locally."""
    from rebar_reconciler.inbound_translate import _jira_key_to_local_id

    key = _seed_searchable_issue(
        dc_transport, jira_dc_project, track_issue, "rebar J11 slice — inbound"
    )
    local_id = _jira_key_to_local_id(key)

    # Include both local ID and Jira key: a first inbound create has no binding from which the
    # literal filter can derive its Jira target.
    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="the inbound bootstrap-strict pass")

    ticket_dir = dc_store_copy_repo / ".tickets-tracker" / local_id
    assert ticket_dir.exists(), (
        f"the DC issue {key} did not reach the local store as {local_id}; "
        f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
    )


@_skip
@_skip_no_extra
def test_the_scrubbed_copy_plans_no_deletions_or_outbound_updates(
    dc_store_copy_repo: Path,
) -> None:
    """Require zero deletions and outbound updates after binding scrub.

    Either action needs a surviving binding, so any planned instance proves scrub failure.
    """
    cp = _run_reconcile(dc_store_copy_repo, "dry-run")
    plan = _envelope(cp).get("plan", [])
    deletions = [e for e in plan if e.get("action") == "delete"]
    updates = [e for e in plan if e.get("direction") == "outbound" and e.get("action") == "update"]
    assert deletions == [], f"the scrub left bindings behind: {len(deletions)} deletions planned"
    assert updates == [], f"unexpected outbound updates over an unbound store: {len(updates)}"


@_skip
@_skip_no_extra
def test_a_local_edit_reaches_the_dc_issue_outbound(
    dc_store_copy_repo: Path, dc_transport: Any, bound_dc_issue: Any
) -> None:
    """Prove a local edit reaches the bound DC issue through an outbound pass.

    Direct readback must equal this test's new value, not an unchanged remote field.
    """
    import rebar

    local_id, key = bound_dc_issue
    new_title = f"rebar J11 outbound proof {key}"

    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=new_title)

    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what="outbound pass")

    remote = dc_transport.get_issue_by_rest(key)
    summary = (remote.get("fields") or {}).get("summary")
    assert summary == new_title, (
        f"the local edit did NOT surface on {key}: fields.summary is {summary!r}, expected "
        f"{new_title!r}. This is the epic's headline outbound criterion.\n"
        f"stdout:\n{cp.stdout[-1500:]}\nstderr:\n{cp.stderr[-1500:]}"
    )
