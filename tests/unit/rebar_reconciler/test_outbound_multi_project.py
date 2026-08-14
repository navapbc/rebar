"""Outbound multi-project create routing + guard relaxation (story d19d, epic 0e68).

Outbound sync historically wrote every created issue to ONE construction-time
project and its pre-flight guard aborted the pass on any key outside that one
project. This module pins the many-to-many behaviour:

* the create mutation is stamped with the ticket's RESOLVED project
  (``resolve_project`` against the store mapping) under the reserved payload key
  ``_bridge_target_project`` — resolved once in the differ, above both transports;
* a ticket whose resolved project is NOT in the mapping (a stale/typo binding), or
  which resolves to "not synced", emits NO create mutation (creates are guard-exempt,
  so this is the only place that gap is closed);
* BOTH transports honour the stamped project — Cloud ``AcliClient`` and the Data
  Center ``_IssuesMixin`` — falling back to their construction-time project when the
  key is absent, so single-project stores are unchanged;
* the Data Center transport DROPS the reserved key before splatting the translated
  field dict into ``jira.JIRA.create_issue(**fields)`` — otherwise it is sent as a
  bogus Jira field id and the create 400s;
* the guard ``_cross_project_targets`` accepts the mapping's project SET (still a
  bare string for the single-project case) and only flags keys outside it.

The transports are exercised at their real seams (``AcliClient.create_issue`` and
``JiraDataCenterTransport.create_issue``) with the vendor call recorded, because the
destination project carried on that call is the contract under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pyt_reserved_key = "_bridge_target_project"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def od() -> Any:
    from rebar_reconciler import outbound_differ

    return outbound_differ


@pytest.fixture()
def projects_store() -> Any:
    from rebar_reconciler import projects_store as ps

    return ps


@pytest.fixture()
def binding_store_mod() -> Any:
    from rebar_reconciler import binding_store

    return binding_store


@pytest.fixture()
def applier_mod() -> Any:
    from rebar_reconciler import applier

    return applier


def _ticket(tid: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticket_id": tid,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    base.update(over)
    return base


def _fresh_store(binding_store_mod: Any, repo_root: Path) -> Any:
    """A real, empty BindingStore — every local ticket is unbound, so it creates."""
    return binding_store_mod.BindingStore(repo_root / ".tickets-tracker")


def _mapping(projects_store: Any) -> Any:
    return projects_store.Mapping(
        legacy_default="A",
        projects={"A": {"repos": ["rebar"]}, "B": {"repos": ["api"]}},
    )


def _creates(mutations: list[Any]) -> list[Any]:
    return [m for m in mutations if getattr(m, "action", "") == "create"]


def _run_diff(od: Any, projects_store: Any, store: Any, tickets: list[dict], mapping: Any):
    return od.compute_outbound_mutations(
        local_tickets=tickets,
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p1", projects_mapping=mapping),
    )


# ===========================================================================
# Differ: the create mutation carries the ticket's resolved project
# ===========================================================================


def test_create_mutation_is_stamped_with_the_resolved_project(
    od, projects_store, binding_store_mod, tmp_path
):
    store = _fresh_store(binding_store_mod, tmp_path)
    mutations, _ = _run_diff(
        od, projects_store, store, [_ticket("loc-1", bridge_project="B")], _mapping(projects_store)
    )
    creates = _creates(mutations)
    assert len(creates) == 1, (
        f"expected one create, got {[(m.local_id, m.action) for m in mutations]}"
    )
    assert creates[0].fields.get(pyt_reserved_key) == "B", (
        "the create mutation must carry the ticket's RESOLVED project under "
        f"{pyt_reserved_key!r}; got {creates[0].fields.get(pyt_reserved_key)!r}"
    )


def test_create_suppressed_when_resolved_project_not_in_mapping(
    od, projects_store, binding_store_mod, tmp_path
):
    store = _fresh_store(binding_store_mod, tmp_path)
    mutations, _ = _run_diff(
        od, projects_store, store, [_ticket("loc-z", bridge_project="Z")], _mapping(projects_store)
    )
    assert _creates(mutations) == [], (
        "a ticket whose bridge_project names a project NOT in the mapping "
        "(stale/typo binding) must NOT create against an unsynced project; "
        f"got {[(m.local_id, m.fields.get(pyt_reserved_key)) for m in _creates(mutations)]}"
    )


def test_create_suppressed_when_ticket_is_not_synced(
    od, projects_store, binding_store_mod, tmp_path
):
    store = _fresh_store(binding_store_mod, tmp_path)
    mutations, _ = _run_diff(
        od, projects_store, store, [_ticket("loc-off", bridge_project="")], _mapping(projects_store)
    )
    assert _creates(mutations) == [], (
        "a ticket that explicitly opts out of sync (bridge_project == '') must emit "
        "no create mutation"
    )


def test_single_project_store_is_unchanged_when_no_mapping_is_seeded(
    od, projects_store, binding_store_mod, tmp_path
):
    """Backward compat: an empty mapping keeps legacy behaviour — create, no stamp."""
    store = _fresh_store(binding_store_mod, tmp_path)
    empty = projects_store.Mapping()  # no legacy_default, no projects
    mutations, _ = _run_diff(od, projects_store, store, [_ticket("loc-legacy")], empty)
    creates = _creates(mutations)
    assert len(creates) == 1, "an unseeded (single-project) store must still create as before"
    assert pyt_reserved_key not in creates[0].fields, (
        "with no mapping seeded the differ must not stamp a target project — the "
        "transport's construction-time project applies unchanged"
    )


# ===========================================================================
# Cloud transport: create honours the stamped project, falls back otherwise
# ===========================================================================


@pytest.fixture()
def acli_mod() -> Any:
    from rebar_reconciler.adapters.jira import acli

    return acli


def _cloud_client(acli_mod: Any) -> Any:
    return acli_mod.AcliClient(
        jira_url="https://example.invalid",
        user="u",
        api_token="t",
        jira_project="A",
    )


def test_cloud_create_targets_the_stamped_project(acli_mod, monkeypatch):
    from rebar_reconciler.adapters.jira import acli_cli_ops

    recorded: dict[str, Any] = {}

    def _fake_create(project, issue_type, summary, *, acli_cmd=None, client=None, **kwargs):
        recorded["project"] = project
        return {"key": f"{project}-1"}

    monkeypatch.setattr(acli_cli_ops, "create_issue", _fake_create)
    client = _cloud_client(acli_mod)
    client.create_issue({"title": "t", "ticket_type": "task", pyt_reserved_key: "B"})
    assert recorded["project"] == "B", (
        "Cloud create_issue must target the stamped _bridge_target_project, not "
        f"self.jira_project; got {recorded['project']!r}"
    )


def test_cloud_create_falls_back_to_construction_project(acli_mod, monkeypatch):
    from rebar_reconciler.adapters.jira import acli_cli_ops

    recorded: dict[str, Any] = {}

    def _fake_create(project, issue_type, summary, *, acli_cmd=None, client=None, **kwargs):
        recorded["project"] = project
        return {"key": f"{project}-1"}

    monkeypatch.setattr(acli_cli_ops, "create_issue", _fake_create)
    client = _cloud_client(acli_mod)
    client.create_issue({"title": "t", "ticket_type": "task"})
    assert recorded["project"] == "A", (
        "with no stamped project the Cloud transport must fall back to its "
        f"construction-time jira_project; got {recorded['project']!r}"
    )


# ===========================================================================
# Data Center transport: honours the stamped project AND drops the reserved key
# ===========================================================================


class _RecordingDCClient:
    """Records the fields dict handed to ``create_issue`` (jira.JIRA seam)."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    def create_issue(self, **fields: Any) -> Any:
        self.create_calls.append(dict(fields))
        key = fields.get("project", {}).get("key", "X") + "-1"
        return type(
            "Issue",
            (),
            {"key": key, "fields": type("F", (), {})(), "raw": {"key": key, "fields": {}}},
        )()


def _dc_transport(client: Any) -> Any:
    from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    return JiraDataCenterTransport(client=client, project="A")


def test_dc_create_targets_the_stamped_project(monkeypatch):
    client = _RecordingDCClient()
    transport = _dc_transport(client)
    transport.create_issue({"summary": "t", "issuetype": "Task", pyt_reserved_key: "B"})
    assert client.create_calls, "the DC transport must have called create_issue"
    project = client.create_calls[0].get("project")
    assert project == {"key": "B"}, (
        "the DC create must target the stamped _bridge_target_project, not self.project; "
        f"got {project!r}"
    )


def test_dc_create_drops_the_reserved_key_from_the_field_set(monkeypatch):
    client = _RecordingDCClient()
    transport = _dc_transport(client)
    transport.create_issue({"summary": "t", "issuetype": "Task", pyt_reserved_key: "B"})
    fields = client.create_calls[0]
    assert pyt_reserved_key not in fields, (
        "the reserved key must be DROPPED before the field dict is splatted into "
        f"jira.JIRA.create_issue(**fields) — otherwise Jira 400s; got keys {sorted(fields)}"
    )


def test_dc_create_falls_back_to_construction_project(monkeypatch):
    client = _RecordingDCClient()
    transport = _dc_transport(client)
    transport.create_issue({"summary": "t", "issuetype": "Task"})
    assert client.create_calls[0].get("project") == {"key": "A"}, (
        "with no stamped project the DC transport must fall back to its construction-time project"
    )


# ===========================================================================
# Guard: set membership, still a bare string for single-project
# ===========================================================================


def _update(key: str) -> dict[str, Any]:
    return {"direction": "outbound", "action": "update", "key": key, "local_id": "l"}


def test_guard_flags_only_keys_outside_the_project_set(applier_mod):
    offenders = applier_mod._cross_project_targets(
        [_update("B-1"), _update("C-1"), _update("A-9")], {"A", "B"}
    )
    keys = {k for k, _ in offenders}
    assert keys == {"C-1"}, (
        "with a project set {A, B}, an update to B-1 or A-9 must pass and only C-1 "
        f"(outside the set) must be flagged; got {offenders}"
    )


def test_guard_single_project_string_still_flags_foreign_keys(applier_mod):
    offenders = applier_mod._cross_project_targets([_update("DIG-1"), _update("REB-1")], "REB")
    keys = {k for k, _ in offenders}
    assert keys == {"DIG-1"}, (
        "single-project behaviour (a bare string) is unchanged: a foreign key is "
        f"flagged, the configured one is not; got {offenders}"
    )


# ===========================================================================
# Bug 7b9a finding 1: the create-path membership check is CASE-INSENSITIVE and
# agrees with the applier guard (which normalizes both sides to uppercase). A
# bridge_project that matches a mapping key only by case must still create, and
# the stamped key must be the CANONICAL mapping key (so the transport routes to
# the real Jira project), not the ticket's raw-case value.
# ===========================================================================


def test_create_membership_check_is_case_insensitive_and_stamps_canonical_key(
    od, projects_store, binding_store_mod, tmp_path
):
    store = _fresh_store(binding_store_mod, tmp_path)
    # mapping keys are canonical uppercase {"A", "B"}; the ticket names "b" (lower)
    mutations, _ = _run_diff(
        od, projects_store, store, [_ticket("loc-lc", bridge_project="b")], _mapping(projects_store)
    )
    creates = _creates(mutations)
    assert len(creates) == 1, (
        "a bridge_project that matches a mapping key case-insensitively ('b' vs 'B') "
        "must still emit a create — the create path must agree with the applier guard, "
        f"which uppercases both sides; got {[(m.local_id, m.action) for m in mutations]}"
    )
    assert creates[0].fields.get(pyt_reserved_key) == "B", (
        "the create must be stamped with the CANONICAL mapping key ('B'), not the "
        f"ticket's raw case ('b'), so the transport routes to the real project; got "
        f"{creates[0].fields.get(pyt_reserved_key)!r}"
    )


def test_create_still_suppressed_when_project_absent_even_case_folded(
    od, projects_store, binding_store_mod, tmp_path
):
    """The case-insensitive relaxation must not resurrect a genuinely-absent project:
    a bridge_project with no case-folded match in the mapping still emits no create."""
    store = _fresh_store(binding_store_mod, tmp_path)
    mutations, _ = _run_diff(
        od,
        projects_store,
        store,
        [_ticket("loc-zz", bridge_project="zz")],
        _mapping(projects_store),
    )
    assert _creates(mutations) == [], (
        "'zz' has no case-insensitive match in {A, B}; the create must still be "
        f"suppressed; got "
        f"{[(m.local_id, m.fields.get(pyt_reserved_key)) for m in _creates(mutations)]}"
    )


# ===========================================================================
# Bug 7b9a finding 3: the run_differs outbound seam threads
# projects_store.load_mapping(repo_root); a MALFORMED projects.json must fail
# CLOSED (ValueError aborts the pass), not silently degrade. This exercises the
# real seam, not load_mapping in isolation.
# ===========================================================================


def test_run_differs_outbound_seam_fails_closed_on_malformed_projects_json(tmp_path):
    import types

    from rebar_reconciler import local_label_intent, outbound_differ, run_differs

    bridge = tmp_path / ".tickets-tracker" / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "projects.json").write_text("{ this is not valid json", encoding="utf-8")

    backend = types.SimpleNamespace(transport=object())
    ctx = types.SimpleNamespace(
        # A scoped pass so pending-binding recovery is skipped (no transport calls).
        filter_local_ids=["scope"],
        selection_ids=None,
        binding_store=types.SimpleNamespace(get_jira_key=lambda _id: None),
        local_tickets=[],
        local_label_intent_mod=local_label_intent,
        tracker_dir=tmp_path / ".tickets-tracker",
        repo_root=tmp_path,
        outbound_differ_mod=outbound_differ,
        pass_id="p-malformed",
        prev_snapshot={},
        curr_snapshot={},
        sync_logger=None,
        recovery_failures=0,
    )
    with pytest.raises(ValueError, match=r"projects\.json"):
        run_differs._run_differs_outbound(ctx, [], backend)
