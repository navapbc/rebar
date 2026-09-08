"""Rehearse multi-project routing against disposable Jira Data Center projects (story 368f).

Opt-in live scenarios use an isolated no-remote store copy and require every preview to
target only the intended projects before syncing. Live label reads absorb index lag;
write-path guards reject contamination. ``_live_jira_ready`` enrolls the all-skip canary,
and project plus per-issue cleanup removes all probes even on scenario failure.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.external

# The plan target of an outbound UPDATE is a Jira key like "RBJABCD-12"; a CREATE has no
# key yet, so the intended project is stamped under this reserved field instead.
_BRIDGE_TARGET_PROJECT = "_bridge_target_project"
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _live_jira_ready() -> bool:
    """Reachable DC ``serverInfo`` (earns ``jira_live``; drives ``@_skip``).

    Mirrors ``_dc_support.live_jira_ready`` deliberately, and is defined at module level
    so the parent conftest's sentinel→marker map assigns ``jira_live`` to this module.
    """
    from _dc_support import live_jira_ready

    return live_jira_ready()


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="Jira DC harness not reachable")


# ---------------------------------------------------------------------------
# Plan inspection — the empty-intersection isolation oracle (ported verbatim)
# ---------------------------------------------------------------------------


def _configured_projects(work: Path) -> set[str]:
    """The project keys currently mapped in the store — the scope a plan must stay within."""
    return set(rebar.bridge_projects_list(repo_root=str(work)).keys())


def _planned_projects(bridge_run: dict[str, Any]) -> set[str]:
    """Extract targets from Jira-key prefixes or create payload project stamps.

    Entries carrying neither target form are not synced and contribute nothing.
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
    """Require planned targets within ``intended`` and outside every live-mapped remainder.

    Separate checks make cross-project contamination explicit rather than merely extra.
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
# Live query + creation helpers (DC transport, not Cloud AcliClient)
# ---------------------------------------------------------------------------


def _keys_by_label(transport: Any, project: str, run_label: str) -> list[str]:
    """Jira keys in *project* carrying *run_label*, via a DC transport JQL search.

    The DC transport's ``search_issues`` calls the live client on every invocation (no
    per-JQL instance cache the Cloud ``AcliClient`` had), so a shared transport is safe
    here; index lag after a create is absorbed by ``_wait_label_count`` at the call sites.
    """
    jql = f'project = "{project}" AND labels = "{run_label}"'
    hits = transport.search_issues(jql)
    return [k for k in (h.get("key") for h in hits) if k]


def _wait_label_count(
    transport: Any,
    project: str,
    run_label: str,
    expected: int,
    *,
    timeout: float = 90.0,
) -> list[str]:
    """Poll live label search until *expected* issues are indexed or *timeout* expires.

    ``bridge_sync`` exposes no plan, so the label read absorbs Lucene lag. Return partial
    keys on timeout so the caller's count assertion supplies the diagnostic.
    """
    deadline = time.monotonic() + timeout
    keys = _keys_by_label(transport, project, run_label)
    while len(keys) < expected and time.monotonic() < deadline:
        time.sleep(2.0)
        keys = _keys_by_label(transport, project, run_label)
    return keys


def _make_outbound_ticket(
    work: Path,
    run_label: str,
    title: str,
    *,
    bridge_project: str | None,
    repos: list[str] | None = None,
    omit_project: bool = False,
    return_id: bool = False,
) -> str:
    """Create a run-labeled ticket with explicit, empty, or omitted project routing.

    Return the canonical ID when selection via ``--only`` is required; aliases are not
    accepted by that resolver.
    """
    kwargs: dict[str, Any] = {"tags": [run_label], "return_alias": True, "repo_root": str(work)}
    if repos is not None:
        kwargs["repos"] = repos
    if not omit_project:
        kwargs["bridge_project"] = bridge_project
    created = rebar.create_ticket("task", title, **kwargs)
    if return_id:
        return created["id"]
    return created["alias"] or created["id"]


# ---------------------------------------------------------------------------
# Legacy-default stamping — no library setter exists (ported from the Cloud harness)
# ---------------------------------------------------------------------------


def _projects_record_path(work: Path) -> Path:
    return work / ".tickets-tracker" / ".bridge_state" / "projects.json"


def _set_legacy_default(work: Path, key: str) -> None:
    """Set ``legacy_default`` directly because the library only preserves that field.

    Keep the committed JSON shape consumed by the reconciler.
    """
    path = _projects_record_path(work)
    record = json.loads(path.read_text())
    record["legacy_default"] = key
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def run_label() -> str:
    """Return and print a per-test issue label for cleanup recovery."""
    label = f"rebar-dc-rehearsal-{uuid.uuid4().hex[:12]}"
    print(f"\n[live_jira_dc/multi_project] run label: {label}")
    return label


@pytest.fixture
def store_copy(
    tmp_path: Path,
    scratch_projects: dict[str, str],
    jira_dc_pat: str,
    jira_dc_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Build an isolated, no-remote store copy mapped to four scratch projects.

    Archive the outer and tracker repositories, scrub bindings, run ensures, then scrub and
    commit again. Pushes and cloud credentials are disabled. ``legacy_default`` remains
    unset so copied production tickets cannot flood the create plan; its scoped scenario
    sets it locally. The unknown project is also scenario-local to avoid polluting fan-out.
    """
    from _dc_fixtures import fetch_tickets, run_git, scrub_bridge_state
    from _dc_support import CLOUD_CREDENTIAL_VARS, source_repo_root

    from rebar._store.ensures import run_ensures

    source = source_repo_root()
    work = tmp_path / "dc-multi-store-copy"
    tracker = work / ".tickets-tracker"
    tracker.mkdir(parents=True)

    def _init(repo: Path, branch: str) -> None:
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "rehearsal@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "rebar 368f DC rehearsal"], cwd=repo, check=True
        )

    _init(work, "main")
    (work / ".gitignore").write_text(".tickets-tracker/\n")

    # Extract a snapshot of the live tickets branch into the inner store repo.
    fetch_tickets(source)
    archive = run_git(["git", "archive", "FETCH_HEAD"], cwd=source).stdout
    subprocess.run(["tar", "-x", "-C", str(tracker)], input=archive, check=True)

    scrub_bridge_state(tracker)
    _init(tracker, "tickets")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--no-verify", "-m", "scrubbed store copy for 368f DC rehearsal"],
        cwd=tracker,
        check=True,
    )

    # Converge into a writable store (creates the `.env-id` marker) and re-scrub the
    # cache the `projects-seed` ensure resurrects (bug 91aa).
    for _outcome in run_ensures(str(tracker)):
        pass
    assert (tracker / ".env-id").is_file(), (
        "ensure-registry did not create the store marker `.env-id`; every library write "
        "against this copy would fail with 'ticket system not initialized'"
    )
    scrub_bridge_state(tracker, commit=True)

    legacy_key = scratch_projects["legacy"]
    (work / "rebar.toml").write_text(
        textwrap.dedent(f"""
        [reconciler]
        backend = "jira-datacenter"
        base_url = "{jira_dc_base_url}"
        allow_insecure = true

        [jira]
        project = "{legacy_key}"
        """).lstrip()
    )
    monkeypatch.setenv("JIRA_PAT", jira_dc_pat)
    monkeypatch.setenv("JIRA_PROJECT", legacy_key)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_ROOT", str(work))
    for cloud_var in CLOUD_CREDENTIAL_VARS:
        monkeypatch.delenv(cloud_var, raising=False)

    # Seed the many-to-many mapping: single / two / zero-repo + the legacy default.
    rebar.bridge_projects_set(scratch_projects["one"], ["rebar"], repo_root=str(work))
    rebar.bridge_projects_set(
        scratch_projects["two"], ["rebar-web", "rebar-api"], repo_root=str(work)
    )
    rebar.bridge_projects_set(scratch_projects["zero"], [], repo_root=str(work))
    rebar.bridge_projects_set(legacy_key, ["rebar-legacy"], repo_root=str(work))
    # Leave legacy_default unset so scrubbed production tickets cannot flood the create plan.
    # Its one scenario sets it locally and scopes every pass to that ticket.
    return work


# ---------------------------------------------------------------------------
# Scenario 1 — inbound from BOTH projects
# ---------------------------------------------------------------------------


@_skip
def test_inbound_from_both_projects(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, track_issue: Any
) -> None:
    """One issue in each of ``one`` + ``two`` ingests inbound from BOTH projects."""
    from _dc_support import seed_searchable_issue

    work = store_copy
    one, two = scratch_projects["one"], scratch_projects["two"]

    seed_searchable_issue(dc_transport, one, track_issue, "368f inbound one probe")
    seed_searchable_issue(dc_transport, two, track_issue, "368f inbound two probe")

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {one, two}, work)
    assert {one, two} <= _planned_projects(preview), (
        f"inbound preview planned only {sorted(_planned_projects(preview))} of the two "
        "seeded projects — both should ingest"
    )

    sync = rebar.bridge_sync(repo_root=str(work))
    # A LIVE sync carries no plan in its details, so assert the observable apply outcome:
    # both seeded issues ingested as local-store mutations (the preview above already
    # pinned that BOTH projects are in the plan this sync applies).
    applied = (sync.get("details") or {}).get("mutations_applied", 0)
    assert applied >= 2, f"inbound sync applied only {applied} mutations for two seeded issues"


# ---------------------------------------------------------------------------
# Scenario 2 — outbound to BOTH projects
# ---------------------------------------------------------------------------


@_skip
def test_outbound_to_both_projects(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """Two tickets (one per project) each land as an issue in ITS project only."""
    work = store_copy
    one, two = scratch_projects["one"], scratch_projects["two"]

    _make_outbound_ticket(work, run_label, "368f outbound one", bridge_project=one)
    _make_outbound_ticket(work, run_label, "368f outbound two", bridge_project=two)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {one, two}, work)

    rebar.bridge_sync(repo_root=str(work))
    one_keys = _wait_label_count(dc_transport, one, run_label, 1)
    two_keys = _wait_label_count(dc_transport, two, run_label, 1)
    assert len(one_keys) == 1, f"expected one issue in `one`, got {one_keys}"
    assert len(two_keys) == 1, f"expected one issue in `two`, got {two_keys}"


# ---------------------------------------------------------------------------
# Scenario 3 — a ticket with an explicit empty project is NOT synced
# ---------------------------------------------------------------------------


@_skip
def test_explicit_empty_project_is_not_synced(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """An explicit empty ``bridge_project`` yields no issue and no outbound plan target."""
    work = store_copy
    _make_outbound_ticket(work, run_label, "368f not-synced", bridge_project="")

    preview = rebar.bridge_preview(repo_root=str(work))
    # Not routed to ANY project: the plan names no project for it.
    _assert_scope(preview, set(), work)

    rebar.bridge_sync(repo_root=str(work))
    for project in scratch_projects.values():
        assert _keys_by_label(dc_transport, project, run_label) == [], (
            f"a not-synced ticket produced an issue in {project}"
        )


# ---------------------------------------------------------------------------
# Scenario 4 — an ABSENT project field resolves to the legacy default only
# ---------------------------------------------------------------------------


@_skip
def test_absent_project_resolves_to_legacy_default(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """A ticket with NO project field syncs to the recorded legacy default and nowhere else."""
    work = store_copy
    legacy = scratch_projects["legacy"]
    tid = _make_outbound_ticket(
        work,
        run_label,
        "368f legacy default",
        bridge_project=None,
        omit_project=True,
        return_id=True,
    )
    # Set legacy_default HERE (the base copy leaves it None to avoid flooding production
    # tickets). Because setting it makes every absent-bridge_project production ticket
    # resolve to `legacy` too, scope every pass to THIS ticket so only it is planned.
    _set_legacy_default(work, legacy)

    preview = rebar.bridge_preview(only=[tid], repo_root=str(work))
    _assert_scope(preview, {legacy}, work)
    # Require the legacy target explicitly: scope alone passes on an empty plan and would
    # miss the None-to-never-sync regression (obsolete-lax-siamang).
    assert legacy in _planned_projects(preview), (
        f"the absent-project ticket was not planned into the legacy default {legacy}; "
        f"planned={sorted(_planned_projects(preview))} — resolve_project suppressed the "
        "create instead of routing the None sentinel to legacy_default"
    )

    rebar.bridge_sync(only=[tid], repo_root=str(work))

    assert len(_wait_label_count(dc_transport, legacy, run_label, 1)) == 1, (
        "legacy-default ticket did not land in the legacy project"
    )
    for other in _configured_projects(work) - {legacy}:
        assert _keys_by_label(dc_transport, other, run_label) == [], (
            f"legacy-default ticket leaked into {other}"
        )


# ---------------------------------------------------------------------------
# Scenario 5 — repo-config variety, BOTH directions
# ---------------------------------------------------------------------------


@_skip
def test_repo_config_variety_both_directions(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """Single/two/zero-repo projects each keep their repo list; routing ignores repo count."""
    work = store_copy
    one, two, zero = scratch_projects["one"], scratch_projects["two"], scratch_projects["zero"]
    mapping = rebar.bridge_projects_list(repo_root=str(work))
    assert mapping[one]["repos"] == ["rebar"], "`one` is not the single-repo config"
    assert mapping[two]["repos"] == ["rebar-web", "rebar-api"], "`two` is not the two-repo config"
    assert mapping[zero]["repos"] == [], "`zero` is not the zero-repo config"

    _make_outbound_ticket(work, run_label, "368f variety one", bridge_project=one)
    _make_outbound_ticket(work, run_label, "368f variety two", bridge_project=two)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {one, two}, work)

    rebar.bridge_sync(repo_root=str(work))
    for project in (one, two):
        keys = _wait_label_count(dc_transport, project, run_label, 1)
        assert keys, f"outbound ticket under {project}'s repo config did not reach it"


# ---------------------------------------------------------------------------
# Scenario 6 — contamination guard (negative control)
# ---------------------------------------------------------------------------


@_skip
def test_contamination_guard_refuses_out_of_scope_target(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """Surface and refuse an outbound target removed from configured scope.

    Bind while configured, remove the mapping, then require preview to expose the stale
    target and sync to fail closed without changing the remaining live projects.
    """
    work = store_copy
    one, two = scratch_projects["one"], scratch_projects["two"]
    alias = _make_outbound_ticket(work, run_label, "368f contamination", bridge_project=one)

    # Land + bind the ticket in `one` while `one` is still configured. Preview-gate this
    # setup sync too, so EVERY mutating pass in the harness is dry-run-evaluated first.
    setup_preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(setup_preview, {one}, work)
    rebar.bridge_sync(repo_root=str(work))
    # Wait the created issue into the index so `before` captures it (LIVE sync has no plan).
    assert len(_wait_label_count(dc_transport, one, run_label, 1)) == 1, (
        "setup sync did not land the contamination probe in `one`"
    )

    before = {p: sorted(_keys_by_label(dc_transport, p, run_label)) for p in (one, two)}

    # Now drop `one` from the mapping: the ticket's binding points outside configured scope.
    rebar.bridge_projects_remove(one, repo_root=str(work))
    rebar.edit_ticket(alias, repo_root=str(work), title="368f contamination edited")

    preview = rebar.bridge_preview(repo_root=str(work))
    assert one in _planned_projects(preview), (
        "preview did not surface the now-out-of-scope `one` target the sync guard must refuse"
    )

    # The library wraps the applier's CrossProjectTargetError: run_pass_result catches it and
    # _bridge_run re-raises it as a RebarError whose message carries the guard's text. Assert
    # BOTH that sync fails closed AND that it fails for the cross-project reason (not some
    # unrelated error), so a coincidental failure cannot pass this as fail-closed.
    with pytest.raises(rebar.RebarError, match="outside the configured scope"):
        rebar.bridge_sync(repo_root=str(work))

    after = {p: sorted(_keys_by_label(dc_transport, p, run_label)) for p in (one, two)}
    assert after == before, f"the refused sync mutated live issues: before={before} after={after}"


# ---------------------------------------------------------------------------
# Scenario 7 — unknown-project SKIP-and-continue (the pivot's key scenario)
# ---------------------------------------------------------------------------


@_skip
def test_unknown_project_skips_and_continues(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """Skip one nonexistent project while continuing the other mappings (ticket f643).

    In multi-project fan-out, only the bad key is logged, alerted, and removed from the
    snapshot. The single-project fail-closed boundary is covered offline.
    """
    work = store_copy
    one, two = scratch_projects["one"], scratch_projects["two"]

    # An RBJ-format key guaranteed distinct from every provisioned scratch key.
    unknown = "RBJGHOST"
    while unknown in set(scratch_projects.values()):
        unknown += "X"
    rebar.bridge_projects_set(unknown, ["rebar-ghost"], repo_root=str(work))

    before = {p: sorted(_keys_by_label(dc_transport, p, run_label)) for p in (one, two)}

    # Neither the read-only preview NOR the mutating sync aborts over the non-existent project
    # key: the unknown project is skipped and the pass proceeds. Run both legs so the sync leg
    # — the one that could damage live data — is genuinely exercised.
    rebar.bridge_preview(repo_root=str(work))
    rebar.bridge_sync(repo_root=str(work))

    # The skip is safe: the real scratch projects are untouched by the aborted-then-resumed
    # unknown-key handling (no partial read reached them as deletions).
    after = {p: sorted(_keys_by_label(dc_transport, p, run_label)) for p in (one, two)}
    assert after == before, (
        f"a skip-and-continue pass over an unknown project mutated live issues: "
        f"before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Scenario 8 — promote-only
# ---------------------------------------------------------------------------


@_skip
def test_promote_only_binding_is_one_way(
    store_copy: Path, scratch_projects: dict[str, str], dc_transport: Any, run_label: str
) -> None:
    """Promoting an unbound ticket creates the issue; re-pointing a BOUND ticket is refused."""
    work = store_copy
    one, two = scratch_projects["one"], scratch_projects["two"]
    alias = _make_outbound_ticket(work, run_label, "368f promote", bridge_project=one)

    preview = rebar.bridge_preview(repo_root=str(work))
    _assert_scope(preview, {one}, work)
    rebar.bridge_sync(repo_root=str(work))

    one_keys = _wait_label_count(dc_transport, one, run_label, 1)
    assert len(one_keys) == 1, f"promote did not create the `one` issue: {one_keys}"

    # Repointing a bound ticket must fail with the promote-only reason at edit time, before
    # the applier can change either project's issue.
    with pytest.raises(rebar.RebarError, match="promote-only"):
        rebar.edit_ticket(alias, repo_root=str(work), bridge_project=two)
    assert _keys_by_label(dc_transport, two, run_label) == [], (
        "a bound ticket was re-homed into `two`"
    )
    assert _keys_by_label(dc_transport, one, run_label) == one_keys, (
        "the original `one` binding changed"
    )


# ---------------------------------------------------------------------------
# Scenario 9 — capability stamp (+ fail-closed on an unknown capability)
# ---------------------------------------------------------------------------


@_skip
def test_capability_stamp_and_fail_closed(
    store_copy: Path, scratch_projects: dict[str, str], tmp_path: Path
) -> None:
    """Two+ mapped projects + run_ensures stamps the capability; unknown caps fail closed."""
    from rebar._store.compat import StoreIncompatibleError, check_store_compat
    from rebar._store.ensures import run_ensures

    work = store_copy
    tracker = work / ".tickets-tracker"

    # `bridge_projects_set` already commits the mapping, so ensures retains all projects and
    # sees the multi-project precondition. A manual commit would be empty (tickets fea4/b783).

    # The mapping now holds >1 project (one + two + zero + legacy); converging stamps it.
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
# Scenario 10 — failure-path cleanup proof (the AC)
# ---------------------------------------------------------------------------


class _InjectedScenarioFailure(RuntimeError):
    """Raised INSIDE the scenario body to simulate a mid-scenario assertion failure."""


@_skip
def test_cleanup_runs_despite_mid_scenario_failure(
    store_copy: Path,
    scratch_projects: dict[str, str],
    dc_transport: Any,
    track_issue: Any,
    run_label: str,
) -> None:
    """Prove per-issue cleanup survives a caught mid-scenario failure.

    Delete the live probe in ``finally``, then require direct readback to return 404. This
    isolates per-issue cleanup from project teardown and avoids search-index lag.
    """
    from _dc_support import seed_searchable_issue

    one = scratch_projects["one"]
    probe: str | None = None
    injected = False
    try:
        probe = seed_searchable_issue(
            dc_transport,
            one,
            track_issue,
            "368f cleanup-proof probe",
            extra={"labels": [run_label]},
        )
        # Simulate a scenario that fails after creating a live issue but before its own cleanup.
        raise _InjectedScenarioFailure("simulated mid-scenario failure")
    except _InjectedScenarioFailure:
        injected = True
    finally:
        if probe is not None:
            try:
                dc_transport.delete_issue(probe)
            except Exception as exc:  # noqa: BLE001 — best-effort per-issue cleanup
                print(f"CLEANUP WARNING: delete_issue({probe}) failed: {exc!r}")

    assert injected, "the injected failure did not fire — the cleanup proof is vacuous"
    assert probe is not None
    # Require a direct 404 to prove deletion; label search is unreliable during index lag.
    with pytest.raises(Exception):  # noqa: B017 — the backend 404 error type is not pinned
        dc_transport.get_issue(probe)

    for project in scratch_projects.values():
        assert _keys_by_label(dc_transport, project, run_label) == [], (
            f"cleanup did NOT run despite the mid-scenario failure: an issue survives in {project}"
        )
