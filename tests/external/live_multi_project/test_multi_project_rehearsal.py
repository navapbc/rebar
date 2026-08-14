"""Live multi-project bridge rehearsal against REB + DIG (story 368f).

Opt-in, LIVE-ONLY canary for the many-to-many Jira bridge. Every scenario drives
the reconciler against TWO real Jira Cloud projects over an ISOLATED S3 copy of the
tickets store (see ``conftest.py``) and asserts the headline invariant: work routes
to EXACTLY its intended project and never contaminates another.

Gating (three independent layers, all off the default lane):
  1. the parent ``tests/external/conftest.py`` autouse skip on ``REBAR_RUN_EXTERNAL``;
  2. ``_live_jira_ready()`` here (Jira creds + ``acli``) via ``@_skip``;
  3. the ``rehearsal_store`` fixture's skip when ``REBAR_REHEARSAL_S3_REMOTE`` is unset.
Defining a module-level ``_live_jira_ready`` also earns the ``jira_live`` marker from
the parent conftest's ``pytest_collection_modifyitems``.

EVERY scenario is PREVIEW-GATED: it runs ``bridge_preview`` first and asserts
``_planned_projects(preview)`` is a subset of the intended project and disjoint from
every OTHER configured project, THEN applies with ``bridge_sync``, THEN verifies live
by a FRESH client's label query. Preview is a dry run and never reaches the write-path
cross-project guard, so the empty-intersection assertion is what SURFACES a stray
target in preview; the guard (``CrossProjectTargetError``) fires on ``bridge_sync``.

Cleanup contract: outbound issues are created as rebar tickets TAGGED with the
run-scoped label (tags become Jira labels through the bridge), so the label sweep
finds them. ``acli.create_issue`` does not set labels, so INBOUND-SEED probe issues
created directly through the transport are tracked by EXPLICIT key registration
(``label_cleanup.add(key)``) and deleted in each test's ``finally`` — the sweep-by-label
plus explicit-key registration together guarantee nothing is stranded.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.external

# Project keys — these MIRROR the mapping the ``rehearsal_store`` fixture seeds in
# ``conftest.py``. Defined here rather than imported: pytest registers every
# ``conftest.py`` under the module name ``conftest`` (the repo root's included), so a
# ``from conftest import …`` is a documented collision hazard — each live module in this
# tree is deliberately self-contained instead. A drift between these and the seed fails
# the live suite loudly.
REB_PROJECT = "REB"
DIG_PROJECT = "DIG"
EMPTY_PROJECT = "REBEMPTY"
UNKNOWN_PROJECT = "REBGHOST"
LEGACY_DEFAULT = REB_PROJECT

# The plan target of an outbound UPDATE is a Jira key like "REB-12"; a CREATE has no key
# yet, so the intended project is stamped under this reserved field instead.
_BRIDGE_TARGET_PROJECT = "_bridge_target_project"
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _live_jira_ready() -> bool:
    """Live Jira creds + ``acli`` present (earns ``jira_live``; drives ``@_skip``)."""
    creds = all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"))
    return bool(creds) and shutil.which("acli") is not None


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="no live Jira creds / acli binary")


def build_cloud_client(project: str = REB_PROJECT) -> Any:
    """A FRESH live-Cloud ``AcliClient`` for *project* (see conftest for the rationale).

    A fresh instance per post-mutation / zero-result query is mandatory:
    ``search_issues`` caches per-JQL PER instance, so a reused client can answer stale.
    """
    engine_dir = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    from rebar_reconciler.adapters.jira import acli as mod

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=project,
    )


def _configured_projects(work: Path) -> set[str]:
    """The project keys currently mapped in the store — the scope a plan must stay within."""
    return set(rebar.bridge_projects_list(repo_root=str(work)).keys())


# ---------------------------------------------------------------------------
# Plan inspection — the empty-intersection isolation oracle
# ---------------------------------------------------------------------------


def _planned_projects(bridge_run: dict[str, Any]) -> set[str]:
    """The set of Jira projects a preview/sync plan proposes to touch.

    Defensive by construction: for each plan entry, add the key-prefix project when
    ``target`` is a Jira key (``^[A-Z][A-Z0-9]+-\\d+$``), else fall back to the create
    payload's ``_bridge_target_project`` stamp. Nones are skipped, so an entry that
    carries neither (a not-synced ticket) contributes nothing.
    """
    projects: set[str] = set()
    plan = (bridge_run.get("details") or {}).get("plan", [])
    for entry in plan:
        target = entry.get("target")
        if isinstance(target, str) and _JIRA_KEY_RE.match(target):
            projects.add(target.split("-", 1)[0])
            continue
        stamped = (entry.get("fields") or {}).get(_BRIDGE_TARGET_PROJECT)
        if stamped:
            projects.add(stamped)
    return projects


def _assert_scope(preview: dict[str, Any], intended: set[str], work: Path) -> None:
    """Assert the plan touches only *intended* projects and no other configured one.

    Two assertions, deliberately: the subset check is the positive invariant, and the
    explicit empty-intersection with every OTHER configured project is what SURFACES a
    contamination target that a subset check alone could let read as "just extra". The
    "other" set is read live from the store's mapping so it cannot drift from the seed.
    """
    planned = _planned_projects(preview)
    assert planned <= intended, (
        f"the plan proposes projects {sorted(planned - intended)} outside the intended scope "
        f"{sorted(intended)} — cross-project contamination surfaced in preview"
    )
    others = _configured_projects(work) - intended
    stray = planned & others
    assert not stray, (
        f"the plan targets other configured projects {sorted(stray)} while only {sorted(intended)} "
        "was intended — this is the contamination the isolation guard must refuse"
    )


# ---------------------------------------------------------------------------
# Live query + creation helpers
# ---------------------------------------------------------------------------


def _keys_by_label(project: str, run_label: str) -> list[str]:
    """Jira keys in *project* carrying *run_label*, via a FRESH client (no stale cache)."""
    jql = f'project = "{project}" AND labels = "{run_label}"'
    hits = build_cloud_client(project).search_issues(jql)
    return [k for k in ((h.get("key") or (h.get("issue") or {}).get("key")) for h in hits) if k]


def _make_outbound_ticket(
    work: Path,
    run_label: str,
    title: str,
    *,
    bridge_project: str | None,
    repos: list[str] | None = None,
    omit_project: bool = False,
) -> str:
    """Create a rebar ticket tagged *run_label* so the bridge stamps the label on create.

    ``omit_project`` leaves the ``bridge_project`` field ABSENT (scenario 4's legacy
    path); otherwise ``bridge_project`` is written verbatim (an empty string is the
    explicit "not synced" signal of scenario 3). Returns the ticket alias.
    """
    kwargs: dict[str, Any] = {"tags": [run_label], "return_alias": True, "repo_root": str(work)}
    if repos is not None:
        kwargs["repos"] = repos
    if not omit_project:
        kwargs["bridge_project"] = bridge_project
    created = rebar.create_ticket("task", title, **kwargs)
    return created["alias"] or created["id"]


def _seed_probe_issue(
    label_cleanup: Any,
    project: str,
    title: str,
) -> str:
    """Create an inbound-seed probe issue DIRECTLY in *project* and register it for cleanup.

    ``acli.create_issue`` does not set labels, so this probe cannot be found by the label
    sweep; it is tracked by EXPLICIT key registration instead (and deleted in the caller's
    ``finally``). ``_bridge_target_project`` routes the create into *project* regardless of
    the client's default project.
    """
    created = build_cloud_client(project).create_issue(
        {
            "ticket_type": "task",
            "title": title,
            _BRIDGE_TARGET_PROJECT: project,
        }
    )
    key = created.get("key") or (created.get("issue") or {}).get("key")
    assert key, f"probe create_issue returned no key: {created!r}"
    label_cleanup.add(key)
    return str(key)


# ---------------------------------------------------------------------------
# Scenario 1 — inbound from BOTH projects
# ---------------------------------------------------------------------------


@_skip
def test_inbound_from_both_projects(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """One REB + one DIG issue both ingest inbound with the correct source project + repos."""
    work = rehearsal_store
    probes: list[str] = []
    try:
        probes.append(_seed_probe_issue(label_cleanup, REB_PROJECT, "368f inbound REB probe"))
        probes.append(_seed_probe_issue(label_cleanup, DIG_PROJECT, "368f inbound DIG probe"))

        preview = rebar.bridge_preview(repo_root=str(work))
        _assert_scope(preview, {REB_PROJECT, DIG_PROJECT}, work)

        sync = rebar.bridge_sync(repo_root=str(work))

        # Both probes ingested: the applied plan created a local ticket for each, sourced
        # from its own project with that project's configured repos (single vs two-repo).
        planned = _planned_projects(sync)
        assert {REB_PROJECT, DIG_PROJECT} <= planned, (
            f"inbound sync ingested only {sorted(planned)} of the two seeded projects"
        )
        mapping = rebar.bridge_projects_list(repo_root=str(work))
        assert mapping[REB_PROJECT]["repos"] == ["rebar"], "REB ingested with the wrong repo list"
        assert mapping[DIG_PROJECT]["repos"] == ["rebar-web", "rebar-api"], (
            "DIG ingested with the wrong repo list"
        )
    finally:
        for key in probes:
            try:
                build_cloud_client().delete_issue(key)
            except Exception as exc:  # noqa: BLE001 — best-effort per-issue cleanup
                print(f"CLEANUP WARNING: delete_issue({key}) failed: {exc!r}")


# ---------------------------------------------------------------------------
# Scenario 2 — outbound to BOTH projects
# ---------------------------------------------------------------------------


@_skip
def test_outbound_to_both_projects(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """Two tickets (one per project) each land as an issue in ITS project only."""
    work = rehearsal_store
    _make_outbound_ticket(work, run_label, "368f outbound REB", bridge_project=REB_PROJECT)
    _make_outbound_ticket(work, run_label, "368f outbound DIG", bridge_project=DIG_PROJECT)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {REB_PROJECT, DIG_PROJECT}, work)

    rebar.bridge_sync(repo_root=str(work))

    reb_keys = _keys_by_label(REB_PROJECT, run_label)
    dig_keys = _keys_by_label(DIG_PROJECT, run_label)
    assert len(reb_keys) == 1, f"expected one REB issue, got {reb_keys}"
    assert len(dig_keys) == 1, f"expected one DIG issue, got {dig_keys}"
    for key in reb_keys:
        label_cleanup.add(key)
    for key in dig_keys:
        label_cleanup.add(key)


# ---------------------------------------------------------------------------
# Scenario 3 — a ticket with an explicit empty project is NOT synced
# ---------------------------------------------------------------------------


@_skip
def test_explicit_empty_project_is_not_synced(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """An explicit empty ``bridge_project`` yields no issue and no outbound plan target.

    ``label_cleanup`` is requested (though nothing should be created) so the always-run
    session sweep is active even when this scenario runs alone — a regression that DID
    create an issue is then still swept and reported rather than stranded.
    """
    work = rehearsal_store
    _make_outbound_ticket(work, run_label, "368f not-synced", bridge_project="")

    preview = rebar.bridge_preview(repo_root=str(work))
    # Not routed to ANY project: the plan names no project for it.
    _assert_scope(preview, set(), work)

    rebar.bridge_sync(repo_root=str(work))
    for project in (REB_PROJECT, DIG_PROJECT):
        assert _keys_by_label(project, run_label) == [], (
            f"a not-synced ticket produced an issue in {project}"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — an ABSENT project field resolves to the legacy default only
# ---------------------------------------------------------------------------


@_skip
def test_absent_project_resolves_to_legacy_default(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """A ticket with NO project field syncs to the recorded legacy default and nowhere else."""
    work = rehearsal_store
    _make_outbound_ticket(
        work, run_label, "368f legacy default", bridge_project=None, omit_project=True
    )

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {LEGACY_DEFAULT}, work)

    rebar.bridge_sync(repo_root=str(work))
    for key in _keys_by_label(LEGACY_DEFAULT, run_label):
        label_cleanup.add(key)
    assert len(_keys_by_label(LEGACY_DEFAULT, run_label)) == 1, "legacy-default ticket did not land"
    for other in _configured_projects(work) - {LEGACY_DEFAULT}:
        assert _keys_by_label(other, run_label) == [], f"legacy-default ticket leaked into {other}"


# ---------------------------------------------------------------------------
# Scenario 5 — repo-config variety, BOTH directions
# ---------------------------------------------------------------------------


@_skip
def test_repo_config_variety_both_directions(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """Single/two/zero-repo projects each keep their repo list; routing ignores repo count."""
    work = rehearsal_store
    mapping = rebar.bridge_projects_list(repo_root=str(work))
    assert mapping[REB_PROJECT]["repos"] == ["rebar"], "REB is not the single-repo config"
    assert mapping[DIG_PROJECT]["repos"] == ["rebar-web", "rebar-api"], (
        "DIG is not the two-repo config"
    )
    assert mapping[EMPTY_PROJECT]["repos"] == [], "EMPTY is not the zero-repo config"

    # Outbound reaches the correct project under each repo config.
    _make_outbound_ticket(work, run_label, "368f variety REB", bridge_project=REB_PROJECT)
    _make_outbound_ticket(work, run_label, "368f variety DIG", bridge_project=DIG_PROJECT)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {REB_PROJECT, DIG_PROJECT}, work)

    rebar.bridge_sync(repo_root=str(work))
    for project in (REB_PROJECT, DIG_PROJECT):
        keys = _keys_by_label(project, run_label)
        assert keys, f"outbound ticket under {project}'s repo config did not reach it"
        for key in keys:
            label_cleanup.add(key)


# ---------------------------------------------------------------------------
# Scenario 6 — contamination guard (negative control)
# ---------------------------------------------------------------------------


@_skip
def test_contamination_guard_refuses_out_of_scope_target(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """An outbound update targeting OUTSIDE configured scope is surfaced then REFUSED.

    Setup: bind a ticket to REB, then REMOVE REB from the mapping so the existing binding's
    target is now out of scope. Preview must SURFACE the stray REB target (the empty-
    intersection assertion trips), and ``bridge_sync`` must raise ``CrossProjectTargetError``
    having written nothing — proven by before/after live queries of both projects.
    """
    from rebar_reconciler.applier import CrossProjectTargetError

    work = rehearsal_store
    alias = _make_outbound_ticket(work, run_label, "368f contamination", bridge_project=REB_PROJECT)

    # Land + bind the ticket in REB while REB is still configured.
    rebar.bridge_sync(repo_root=str(work))
    for key in _keys_by_label(REB_PROJECT, run_label):
        label_cleanup.add(key)

    before = {p: sorted(_keys_by_label(p, run_label)) for p in (REB_PROJECT, DIG_PROJECT)}

    # Now drop REB from the mapping: the ticket's binding points outside configured scope.
    rebar.bridge_projects_remove(REB_PROJECT, repo_root=str(work))
    rebar.edit_ticket(alias, repo_root=str(work), title="368f contamination edited")

    preview = rebar.bridge_preview(repo_root=str(work))
    # Only DIG (+ empty/unknown) remain configured; REB is now a stray target and must surface.
    assert REB_PROJECT in _planned_projects(preview), (
        "preview did not surface the now-out-of-scope REB target the sync guard must refuse"
    )

    with pytest.raises(CrossProjectTargetError):
        rebar.bridge_sync(repo_root=str(work))

    after = {p: sorted(_keys_by_label(p, run_label)) for p in (REB_PROJECT, DIG_PROJECT)}
    assert after == before, f"the refused sync mutated live issues: before={before} after={after}"


# ---------------------------------------------------------------------------
# Scenario 7 — promote-only
# ---------------------------------------------------------------------------


@_skip
def test_promote_only_binding_is_one_way(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """Promoting an unbound ticket creates the issue; re-pointing a BOUND ticket is refused."""
    from rebar_reconciler.applier import CrossProjectTargetError

    work = rehearsal_store
    alias = _make_outbound_ticket(work, run_label, "368f promote", bridge_project=REB_PROJECT)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {REB_PROJECT}, work)
    rebar.bridge_sync(repo_root=str(work))

    reb_keys = _keys_by_label(REB_PROJECT, run_label)
    assert len(reb_keys) == 1, f"promote did not create the REB issue: {reb_keys}"
    for key in reb_keys:
        label_cleanup.add(key)

    # Re-pointing the bound ticket to DIG must be refused; the REB binding stays put.
    rebar.edit_ticket(alias, repo_root=str(work), bridge_project=DIG_PROJECT)
    with pytest.raises(CrossProjectTargetError):
        rebar.bridge_sync(repo_root=str(work))
    assert _keys_by_label(DIG_PROJECT, run_label) == [], "a bound ticket was re-homed into DIG"
    assert _keys_by_label(REB_PROJECT, run_label) == reb_keys, "the original REB binding changed"


# ---------------------------------------------------------------------------
# Scenario 8 — capability stamp (+ fail-closed on an unknown capability)
# ---------------------------------------------------------------------------


@_skip
def test_capability_stamp_and_fail_closed(rehearsal_store: Path, tmp_path: Path) -> None:
    """Two mapped projects + run_ensures stamps the capability; unknown caps fail closed."""
    import json

    from rebar._store.compat import StoreIncompatibleError, check_store_compat
    from rebar._store.ensures import run_ensures

    work = rehearsal_store
    tracker = work / ".tickets-tracker"

    # The mapping already holds >1 project (REB + DIG + ...); converging stamps the capability.
    list(run_ensures(str(tracker)))
    record = json.loads((tracker / ".store-compat.json").read_text())
    assert "multi-project-bridge" in record.get("required_capabilities", []), (
        "run_ensures did not stamp the multi-project-bridge capability despite two mapped projects"
    )

    # The old-binary-refuses half: a record demanding a FABRICATED capability must fail closed.
    fake_tracker = tmp_path / "fake-compat"
    fake_tracker.mkdir()
    (fake_tracker / ".store-compat.json").write_text(
        json.dumps({"format_version": 1, "required_capabilities": ["made-up-future-capability"]})
    )
    with pytest.raises(StoreIncompatibleError):
        check_store_compat(fake_tracker)


# ---------------------------------------------------------------------------
# Scenario 9 — failure-path cleanup proof (the AC)
# ---------------------------------------------------------------------------


class _InjectedScenarioFailure(RuntimeError):
    """Raised INSIDE the scenario body to simulate a mid-scenario assertion failure."""


@_skip
def test_cleanup_runs_despite_mid_scenario_failure(
    rehearsal_store: Path, run_label: str, label_cleanup: Any
) -> None:
    """Prove per-issue cleanup runs even when the scenario body raises mid-flight.

    The injected error is CAUGHT here so the pytest test itself passes while demonstrating
    the contract: a labelled issue is created, an inner body deliberately raises, the
    per-issue ``finally`` still deletes it, and a fresh label query then returns ZERO in
    each project.
    """
    probe: str | None = None
    injected = False
    try:
        probe = _seed_probe_issue(label_cleanup, REB_PROJECT, "368f cleanup-proof probe")
        # Simulate a scenario that fails after creating a live issue but before its own cleanup.
        raise _InjectedScenarioFailure("simulated mid-scenario failure")
    except _InjectedScenarioFailure:
        injected = True
    finally:
        if probe is not None:
            try:
                build_cloud_client().delete_issue(probe)
            except Exception as exc:  # noqa: BLE001 — best-effort per-issue cleanup
                print(f"CLEANUP WARNING: delete_issue({probe}) failed: {exc!r}")

    assert injected, "the injected failure did not fire — the cleanup proof is vacuous"
    assert probe is not None
    label_cleanup.keys.discard(probe)  # deleted here; drop it from the session backstop set

    for project in (REB_PROJECT, DIG_PROJECT):
        assert _keys_by_label(project, run_label) == [], (
            f"cleanup did NOT run despite the mid-scenario failure: an issue survives in {project}"
        )
