"""READ-ONLY, S3-backed live Jira Cloud multi-project rehearsal (REB + DIG).

Opt-in, LIVE-ONLY canary for the many-to-many Jira bridge over the S3 store backend
and real Cloud volume. See ``conftest.py`` for the design, the gating layers, and the
STRUCTURAL read-only guard (``readonly_jira_guard``, autouse) that makes it impossible
for any scenario here to write to Jira Cloud.

Every scenario is read-only against Jira: the inbound side uses
``rebar_reconciler.fetcher.compute_snapshot`` (the no-write fetch) and
``rebar.bridge_preview`` (a dry run), the guard forbids every mutating transport
method, and the Jira-touching scenarios assert per-project issue counts are identical
before and after. The five validations the story requires map to the scenarios:

  1. one S3 store maps BOTH projects  -> test_one_store_maps_both_projects
  2. inbound pulls BOTH at real volume -> test_inbound_fetch_pulls_both_projects
  3. read-only preview, scoped per project (no cross-project contamination)
     -> test_fetch_is_scoped_per_project + test_bridge_preview_read_only_and_scoped
  4. the S3 backend round-trips with the mapping intact
     -> test_s3_store_roundtrips_with_mapping_intact
  5. ZERO Jira mutations (structural + before/after counts)
     -> enforced on every test; proven non-vacuous by test_read_only_guard_is_real

Plus a negative control from the prior-failure lesson: an UNKNOWN mapped project (among
several) is SKIPPED and the pass continues over the others
(``test_unknown_project_skips_and_continues``).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from _cloud_s3_support import (
    DIG_PROJECT,
    DIG_REPOS,
    REB_PROJECT,
    REB_REPOS,
    JiraWriteForbidden,
    engine_on_path,
    git_run,
    live_jira_ready,
    project_issue_count,
)

import rebar

pytestmark = pytest.mark.external

#: Real Cloud volume is in the low thousands per project; require a healthy floor so a
#: silently truncated or empty fetch cannot read as "both projects present".
_MIN_ISSUES_PER_PROJECT = 50


def _live_jira_ready() -> bool:
    """Live Jira creds + ``acli`` present (earns the ``jira_live`` auto-marker).

    The parent conftest's ``pytest_collection_modifyitems`` scans the test MODULE for
    this exact ``_live_jira_ready`` sentinel; it delegates to the shared predicate.
    """
    return live_jira_ready()


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="no live Jira creds / acli binary")


# ---------------------------------------------------------------------------
# Read-only fetch + plan helpers
# ---------------------------------------------------------------------------


def _snapshot(work: Path, pass_id: str) -> dict[str, Any]:
    """The read-only Jira snapshot for the store's current mapping (writes nothing)."""
    engine_on_path()
    from rebar_reconciler import fetcher

    return fetcher.compute_snapshot(pass_id, work)


def _projects_of(keys: Any) -> Counter:
    """Count Jira keys by their project prefix (``REB-12`` -> ``REB``)."""
    counter: Counter = Counter()
    for key in keys:
        if isinstance(key, str) and "-" in key:
            counter[key.split("-", 1)[0]] += 1
    return counter


def _plan_projects(preview: dict[str, Any]) -> set[str]:
    """The set of Jira projects a preview plan proposes to touch (by target prefix)."""
    projects: set[str] = set()
    plan = (preview.get("details") or {}).get("plan") or preview.get("plan") or []
    for entry in plan:
        target = entry.get("target")
        if isinstance(target, str) and "-" in target:
            projects.add(target.split("-", 1)[0])
    return projects


# ---------------------------------------------------------------------------
# Scenario 1 — one S3 store maps BOTH projects
# ---------------------------------------------------------------------------


@_skip
def test_one_store_maps_both_projects(rehearsal_store: Path) -> None:
    """One S3-backed store maps REB + DIG with the expected repo lists (validation 1)."""
    work = rehearsal_store
    mapping = rebar.bridge_projects_list(repo_root=str(work))
    assert set(mapping) == {REB_PROJECT, DIG_PROJECT}, (
        f"the store must map exactly REB + DIG; got {sorted(mapping)}"
    )
    assert mapping[REB_PROJECT]["repos"] == REB_REPOS, "REB has the wrong repo list"
    assert mapping[DIG_PROJECT]["repos"] == DIG_REPOS, "DIG has the wrong repo list"

    # The mapping record lives on the S3-backed tickets branch of one store.
    record = json.loads((work / ".tickets-tracker" / ".bridge_state" / "projects.json").read_text())
    assert set(record["projects"]) == {REB_PROJECT, DIG_PROJECT}


# ---------------------------------------------------------------------------
# Scenario 2 — inbound fetch pulls BOTH projects at real volume
# ---------------------------------------------------------------------------


@_skip
def test_inbound_fetch_pulls_both_projects(rehearsal_store: Path) -> None:
    """The inbound fetch pulls real tickets from BOTH projects with per-project
    attribution, at real volume (validation 2)."""
    work = rehearsal_store
    before = {p: project_issue_count(p) for p in (REB_PROJECT, DIG_PROJECT)}

    snapshot = _snapshot(work, "inbound-both")
    by_project = _projects_of(snapshot.keys())

    # Both projects fetched into the one store, each at real volume.
    assert by_project[REB_PROJECT] >= _MIN_ISSUES_PER_PROJECT, (
        f"REB fetched only {by_project[REB_PROJECT]} issues (< {_MIN_ISSUES_PER_PROJECT})"
    )
    assert by_project[DIG_PROJECT] >= _MIN_ISSUES_PER_PROJECT, (
        f"DIG fetched only {by_project[DIG_PROJECT]} issues (< {_MIN_ISSUES_PER_PROJECT})"
    )
    # Per-project attribution: EVERY fetched key belongs to a mapped project, so no
    # foreign project leaked into the multi-project fan-out.
    assert set(by_project) == {REB_PROJECT, DIG_PROJECT}, (
        f"the fetch pulled keys outside the mapped projects: {sorted(by_project)}"
    )

    # ZERO Jira mutations: the read-only fetch left both projects' counts unchanged.
    after = {p: project_issue_count(p) for p in (REB_PROJECT, DIG_PROJECT)}
    assert after == before, (
        f"the read-only fetch changed Jira counts: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Scenario 3a — the mapping scopes the fetch (no cross-project contamination)
# ---------------------------------------------------------------------------


@_skip
def test_fetch_is_scoped_per_project(rehearsal_store: Path) -> None:
    """A single-project mapping fetches ONLY that project — the mapping scopes the
    fetch, so neither project contaminates the other (validation 3)."""
    work = rehearsal_store

    rebar.bridge_projects_remove(DIG_PROJECT, repo_root=str(work))
    reb_only = _projects_of(_snapshot(work, "scope-reb").keys())
    assert reb_only.get(DIG_PROJECT, 0) == 0, "REB-only mapping leaked DIG issues"
    assert reb_only.get(REB_PROJECT, 0) >= _MIN_ISSUES_PER_PROJECT, "REB-only fetch was empty"

    rebar.bridge_projects_set(DIG_PROJECT, DIG_REPOS, repo_root=str(work))
    rebar.bridge_projects_remove(REB_PROJECT, repo_root=str(work))
    dig_only = _projects_of(_snapshot(work, "scope-dig").keys())
    assert dig_only.get(REB_PROJECT, 0) == 0, "DIG-only mapping leaked REB issues"
    assert dig_only.get(DIG_PROJECT, 0) >= _MIN_ISSUES_PER_PROJECT, "DIG-only fetch was empty"


# ---------------------------------------------------------------------------
# Scenario 3b — bridge_preview is read-only and scoped to the mapped projects
# ---------------------------------------------------------------------------


@_skip
def test_bridge_preview_read_only_and_scoped(rehearsal_store: Path) -> None:
    """``bridge_preview`` over REB + DIG is read-only and touches ONLY mapped
    projects — no cross-project contamination in the plan (validations 3 + 5)."""
    work = rehearsal_store
    before = {p: project_issue_count(p) for p in (REB_PROJECT, DIG_PROJECT)}

    preview = rebar.bridge_preview(repo_root=str(work))

    planned = _plan_projects(preview)
    assert planned <= {REB_PROJECT, DIG_PROJECT}, (
        f"the preview plan targets projects outside the mapping: {sorted(planned)}"
    )

    after = {p: project_issue_count(p) for p in (REB_PROJECT, DIG_PROJECT)}
    assert after == before, (
        f"the dry-run preview changed Jira counts: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — the S3 store round-trips with the mapping intact
# ---------------------------------------------------------------------------


@_skip
def test_s3_store_roundtrips_with_mapping_intact(rehearsal_store: Path, tmp_path: Path) -> None:
    """Push the store's tickets branch to S3, clone it back, and confirm the REB + DIG
    mapping survives the S3 round-trip (validation 4)."""
    work = rehearsal_store
    tracker = work / ".tickets-tracker"

    remote_url = git_run(["git", "remote", "get-url", "rehearsal-s3"], cwd=tracker).stdout.strip()
    assert remote_url.startswith("s3://"), f"the store remote is not S3: {remote_url!r}"

    # Push the mapping-bearing tickets branch to the throwaway S3 prefix.
    git_run(["git", "push", "rehearsal-s3", "tickets"], cwd=tracker)

    # Clone it straight back out of S3 into a fresh checkout.
    clone = tmp_path / "s3-clone"
    git_run(["git", "clone", remote_url, str(clone)], cwd=tmp_path)

    cloned_record = json.loads((clone / ".bridge_state" / "projects.json").read_text())
    assert set(cloned_record["projects"]) == {REB_PROJECT, DIG_PROJECT}, (
        f"the S3 round-trip lost the mapping; cloned projects={sorted(cloned_record['projects'])}"
    )
    assert cloned_record["projects"][REB_PROJECT]["repos"] == REB_REPOS
    assert cloned_record["projects"][DIG_PROJECT]["repos"] == DIG_REPOS


# ---------------------------------------------------------------------------
# Scenario 5 — an UNKNOWN mapped project fails CLOSED (prior-failure lesson)
# ---------------------------------------------------------------------------


@_skip
def test_unknown_project_skips_and_continues(rehearsal_store: Path) -> None:
    """Mapping a project Jira does not know is SKIPPED, and the pass CONTINUES over the
    other mapped projects — the intended per-project resilience (ticket f643).

    The store already maps REB + DIG, so adding a non-existent third key exercises the
    multi-project skip path (fetcher ``_isolate_projects``): the base JQL search for the
    absent project errors in the acli subprocess, that error is caught and the key is
    dropped from the snapshot, and the fetch still returns the REB/DIG results rather than
    aborting. (The single-project boundary still fails closed — see the non-external
    ``tests/unit/rebar_reconciler/test_fetch_multi_project.py``.)
    """
    work = rehearsal_store
    rebar.bridge_projects_set("REBGHOST", ["rebar-ghost"], repo_root=str(work))

    # The unknown project must NOT abort the pass: the read-only snapshot succeeds and
    # covers only the real mapped projects — REBGHOST contributes no keys.
    snapshot = _snapshot(work, "unknown-skip-and-continue")
    assert "REBGHOST" not in _projects_of(snapshot.keys())


# ---------------------------------------------------------------------------
# Scenario 6 — the structural read-only guard is real, not vacuous
# ---------------------------------------------------------------------------


@_skip
def test_read_only_guard_is_real(readonly_jira_guard: type) -> None:
    """Prove the autouse guard actually forbids outbound Jira mutations — so the
    ZERO-mutation invariant the other scenarios rely on is not vacuous (validation 5)."""
    cls: Any = readonly_jira_guard
    client = cls.__new__(cls)
    with pytest.raises(JiraWriteForbidden):
        client.create_issue({"ticket_type": "task", "title": "must never reach Jira"})
    with pytest.raises(JiraWriteForbidden):
        client.add_label("REB-1", "must-never-be-written")
    with pytest.raises(JiraWriteForbidden):
        client.delete_issue("REB-1")
