"""J11 — the thin vertical slice: a REAL store copy, scrubbed and isolated, round-tripping
against the Dockerized Data Center harness (epic e369, ticket 5200-e04e-246e-4aae).

WHY THIS EXISTS. Every earlier DC run in this epic converged over an EMPTY or unbound store,
which is indistinguishable from working: the pass exits 0, prints a reassuring "converged"
line, and proves nothing. This module runs the bridge against a copy of the project's ACTUAL
ticket store — real ticket shapes, real link graphs, real volume — and asserts that data
MOVES in both directions.

ISOLATION IS THE PRECONDITION, AND IT IS ASSERTED, NOT ASSUMED. rebar's store auto-commits and
auto-pushes to `sync.remote` on every write, so a test that mutates tickets could push into the
project's real tickets branch, and a misconfigured backend could write into the project's real
Jira. Three independent layers, all verified by the tests below rather than trusted:
  1. BOTH repos — the outer working repo and the `.tickets-tracker/` STORE repo, which is the
     one `sync.remote` would actually push — are fresh `git init`s with NO remote, so there is
     physically nowhere to push;
  2. `REBAR_SYNC_PUSH=off`;
  3. no Cloud credential is present in the environment.
Layer 1 is the primary one because it cannot be defeated by a mis-read setting.

THE STORE COPY MUST LAND IN `.tickets-tracker/`, NOT AT THE REPO ROOT. The orphan `tickets`
branch holds ticket files and `.bridge_state` at ITS OWN root, while the reconciler reads
`repo_root / ".tickets-tracker"` (`reconcile.py:265`). Extracting to the root would put every
ticket one directory above where the pass looks — a store that is empty as far as the
reconciler is concerned, produced by the setup rather than by the product.

THE SHARED MACHINERY MOVED. `dc_store_copy_repo` and `bound_dc_issue` now live in `conftest.py`
and the plain helpers in `_dc_support.py`, because `test_dc_mutations.py` needs the same set
and a module-local fixture is invisible to a sibling module. Keeping a second copy here would
reproduce, inside this suite, the duplicated-and-drifted defect class the epic is fixing
elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

# Imported by bare name: pytest inserts this directory on sys.path (there is no `__init__.py`
# anywhere under `tests/`), which is also why `_dc_support` is not a dotted path.
from _dc_support import CLOUD_CREDENTIAL_VARS, live_jira_ready, read_inherited_env
from _dc_support import envelope as _envelope
from _dc_support import is_ticket_entry as _is_ticket_entry
from _dc_support import run_reconcile as _run_reconcile
from _dc_support import seed_searchable_issue as _seed_searchable_issue
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# THE ALL-SKIP CANARY KEYS ON THIS NAME. `tests/external/conftest.py`'s
# `pytest_collection_modifyitems` applies the `jira_live` marker only to modules that define a
# module-level `_live_jira_ready`, and the canary then fails a run in which live tests were
# COLLECTED but none EXECUTED. Refactoring this helper into `_dc_support` silently removed this
# module from that bookkeeping — so the cells carrying the epic's headline evidence could all-skip
# and the run would be green and silent. Re-exported under the name the canary looks for.
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
    # FIXTURE REGRESSION GUARD, and labelled as one (bug 59b2, Finding A). `dc_store_copy_repo`
    # itself sets REBAR_SYNC_PUSH=off, so this CANNOT detect a job environment that failed to set
    # it — it can only detect the fixture ceasing to. That is worth keeping, because losing the
    # fixture's setenv would re-enable pushes from the copy; it is NOT the environment check the
    # original wording implied.
    assert os.environ.get("REBAR_SYNC_PUSH") == "off", (
        "the dc_store_copy_repo fixture no longer sets REBAR_SYNC_PUSH=off — a store write from "
        "this copy could push"
    )

    # THE ACTUAL ENVIRONMENT CHECK: what the JOB supplied, recorded by the fixture BEFORE it
    # deleted anything. Asserting on os.environ here would be circular — the fixture delenv's
    # exactly these names, so the post-fixture environment is guaranteed clean whatever the job
    # did. The recorded snapshot is a value this cell did not author, so it can fail.
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
    """DOES THE FETCH+DIFFER EVEN SEE THE ISSUE? Asserted separately from applying it.

    Split out because the round-trip cell below failed three times for three DIFFERENT
    reasons, and each time its one assertion ("the ticket did not appear") could not say
    whether the differ never planned the create or the pass failed to apply it. This cell
    answers only the first question, so the two failures stop being indistinguishable.

    UNFILTERED and DRY-RUN, both deliberately. Unfiltered because `--filter-local-ids` is a
    POST filter — a pass reported "1640 mutations computed, 0 match filter" — so a filtered
    run cannot show whether the create was planned. Dry-run because unfiltered is only SAFE
    in dry-run: the scrub leaves every copied ticket unbound, so an unfiltered WRITING pass
    would file production tickets into the harness (bootstrap-strict's cap=10 would file ten).
    Dry-run computes the plan and writes nothing.
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

    # PASS BOTH THE LOCAL ID AND THE JIRA KEY. The flag is named --filter-local-ids, but
    # `_build_filter_target_set` (reconcile_helpers.py:419-434) seeds the target set with the
    # strings VERBATIM and can only add a Jira key via `binding_store.get_jira_key(lid)` — a
    # binding that does not exist yet for an issue arriving inbound for the first time. An
    # inbound create's `target` IS the Jira key, so filtering on the local id alone matches
    # NOTHING: an earlier run reported "1640 mutations computed, 0 match filter" and the pass
    # then reported `inbound_differ total=0` POST-filter, which read like the differ had planned
    # nothing at all. It had — the companion dry-run cell above proves it.
    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"unhandled exception in the pass:\n{cp.stderr}"

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
    """A dry-run over the scrubbed copy must plan ZERO deletions and ZERO outbound updates.

    Both are exactly zero because the scrub removed every binding: a surviving `bindings.json`
    would show up as deletions (its production keys do not exist in the harness), and an
    outbound UPDATE is only ever emitted for a ticket that HAS a binding
    (`outbound_differ.py:518-520`). Any non-zero value here means the scrub failed.
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
    """THE EPIC'S UNPROVEN HALF: a local edit surfaces on the DC issue after an outbound pass.

    Everything before this proved DATA ARRIVES (inbound). This is the other direction, and it is
    the criterion the epic has never had evidence for — three live tests existed and none mutated
    a local ticket then asserted the change on the DC issue.

    The assertion reads the value THIS cell wrote, so it is a genuine round-trip rather than a
    re-read of an unchanged field.
    """
    import rebar

    local_id, key = bound_dc_issue
    new_title = f"rebar J11 outbound proof {key}"

    rebar.edit_ticket(local_id, repo_root=dc_store_copy_repo, title=new_title)

    cp = _run_reconcile(dc_store_copy_repo, "bootstrap-strict", only=f"{local_id},{key}")
    assert "Traceback" not in cp.stderr, f"outbound pass raised:\n{cp.stderr[-2000:]}"

    remote = dc_transport.get_issue_by_rest(key)
    summary = (remote.get("fields") or {}).get("summary")
    assert summary == new_title, (
        f"the local edit did NOT surface on {key}: fields.summary is {summary!r}, expected "
        f"{new_title!r}. This is the epic's headline outbound criterion.\n"
        f"stdout:\n{cp.stdout[-1500:]}\nstderr:\n{cp.stderr[-1500:]}"
    )
