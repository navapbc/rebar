"""Story 1734: inbound fetch fans out over the multi-project mapping.

The fetcher must ingest EVERY project in the store's ``projects.json`` mapping,
not just the single configured ``jira.project``:

  * **Base queries** — one active + done JQL pair PER mapped project (the query
    loop already dedups cross-query via ``seen_keys``).
  * **Enrichment** — one parent/comment/issuelink pass PER mapped project, each
    failing open independently.
  * **Per-project ceiling** — the ``_ACLI_CEILING`` is a PER-QUERY (hence
    per-project) bound, never a shared cumulative budget across projects.
  * **Single-project fallback** — an unseeded store reproduces the exact
    single-project queries + enrichment (regression parity).

Inbound-created tickets are stamped with ``bridge_project`` (derived from the
Jira key prefix) and ``repos`` (from the mapping), so cef7's tri-state project
routing fields are populated at creation.

Held-out RED oracle for 1734 — kept out of the fix subagent's tree.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fetcher():
    return _load("fetcher", "fetcher.py")


def _seed_projects(repo_root: Path, mapping: dict[str, list[str]]) -> None:
    """Seed ``projects.json`` under ``repo_root`` with ``{key: repos}``."""
    from rebar_reconciler import projects_store

    for key, repos in mapping.items():
        projects_store.set_project(repo_root, key, repos)


class _FakeClient:
    """Offset-aware fake: serves active-query issues per project, records every
    search JQL and every enrichment call so the fan-out contract is observable."""

    def __init__(self, active_by_project: dict[str, list[dict]]) -> None:
        self.active_by_project = active_by_project
        self.search_calls: list[str] = []
        self.parent_calls: list[str] = []
        self.comment_calls: list[str] = []
        self.issuelink_calls: list[str] = []

    @staticmethod
    def _project_of(jql: str) -> str | None:
        m = re.search(r"project = (\w+)", jql)
        return m.group(1) if m else None

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50) -> list[dict]:
        self.search_calls.append(jql)
        # Done query carries nothing — keeps issue accounting to the active set.
        if 'status = "Done"' in jql:
            return []
        issues = self.active_by_project.get(self._project_of(jql) or "", [])
        return issues[start_at : start_at + max_results]

    def get_parent_map(self, project_key: str) -> dict:
        self.parent_calls.append(project_key)
        return {}

    def get_comment_map(self, project_key: str) -> dict:
        self.comment_calls.append(project_key)
        return {}

    def get_issuelinks_map(self, project_key: str) -> dict:
        self.issuelink_calls.append(project_key)
        return {}


def _issues(project: str, n: int) -> list[dict]:
    return [
        {"key": f"{project}-{i}", "fields": {"summary": f"{project} issue {i}"}}
        for i in range(1, n + 1)
    ]


# --- fan-out: base queries + enrichment per project ------------------------


def test_fetch_fans_out_base_queries_and_enrichment_per_project(fetcher, tmp_path, monkeypatch):
    """A two-project mapping issues both projects' JQL pairs AND one enrichment
    pass per project; the merged snapshot carries both projects' issues."""
    monkeypatch.setenv("JIRA_PROJECT", "AAA")
    _seed_projects(tmp_path, {"AAA": ["ra"], "BBB": ["rb"]})
    client = _FakeClient({"AAA": _issues("AAA", 2), "BBB": _issues("BBB", 3)})

    with patch.object(fetcher, "_load_acli", return_value=client):
        snapshot = fetcher.compute_snapshot("m2m-fanout", repo_root=tmp_path)

    issued = set(client.search_calls)
    assert fetcher.jql_active("AAA") in issued
    assert fetcher.jql_done_recent("AAA") in issued
    assert fetcher.jql_active("BBB") in issued
    assert fetcher.jql_done_recent("BBB") in issued

    # Exactly one enrichment pass per project (the fan-out contract — a bug that
    # only enriches the first project fails here).
    assert sorted(client.parent_calls) == ["AAA", "BBB"]
    assert sorted(client.comment_calls) == ["AAA", "BBB"]
    assert sorted(client.issuelink_calls) == ["AAA", "BBB"]

    # Both projects' issues merged into one snapshot.
    assert {"AAA-1", "AAA-2", "BBB-1", "BBB-2", "BBB-3"} <= set(snapshot)


def test_empty_result_project_does_not_fail_the_fetch(fetcher, tmp_path, monkeypatch):
    """A mapped project with zero issues is ingested cleanly; the other project's
    issues + enrichment still land."""
    monkeypatch.setenv("JIRA_PROJECT", "AAA")
    _seed_projects(tmp_path, {"AAA": ["ra"], "EMPTY": ["re"]})
    client = _FakeClient({"AAA": _issues("AAA", 2), "EMPTY": []})

    with patch.object(fetcher, "_load_acli", return_value=client):
        snapshot = fetcher.compute_snapshot("m2m-empty", repo_root=tmp_path)

    assert sorted(client.parent_calls) == ["AAA", "EMPTY"]
    assert {"AAA-1", "AAA-2"} <= set(snapshot)


def test_ceiling_is_per_project_not_a_shared_budget(fetcher, tmp_path, monkeypatch):
    """With the ceiling lowered to 10, two projects of 7 issues (sum 14 > 10, each
    under 10) BOTH ingest — proving the ceiling is a per-query bound, not a shared
    cumulative budget that would raise once the combined total crossed 10."""
    from rebar_reconciler import fetch_paging

    monkeypatch.setattr(fetch_paging, "_ACLI_CEILING", 10)
    monkeypatch.setenv("JIRA_PROJECT", "AAA")
    _seed_projects(tmp_path, {"AAA": ["ra"], "BBB": ["rb"]})
    client = _FakeClient({"AAA": _issues("AAA", 7), "BBB": _issues("BBB", 7)})

    with patch.object(fetcher, "_load_acli", return_value=client):
        snapshot = fetcher.compute_snapshot("m2m-ceiling", repo_root=tmp_path)

    # 14 issues total across the two projects, no SilentTruncationError.
    assert len({k for k in snapshot if k.startswith(("AAA-", "BBB-"))}) == 14


def test_single_project_fallback_is_regression_identical(fetcher, tmp_path, monkeypatch):
    """An UNSEEDED store (no projects.json) reproduces the exact single-project
    query pair + a single enrichment pass — byte-for-byte the pre-1734 behaviour."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    client = _FakeClient({"REB": _issues("REB", 2)})

    with patch.object(fetcher, "_load_acli", return_value=client):
        snapshot = fetcher.compute_snapshot("m2m-single", repo_root=tmp_path)

    issued = set(client.search_calls)
    assert issued == {fetcher.jql_active("REB"), fetcher.jql_done_recent("REB")}
    assert client.parent_calls == ["REB"]
    assert client.comment_calls == ["REB"]
    assert client.issuelink_calls == ["REB"]
    assert {"REB-1", "REB-2"} <= set(snapshot)


# --- stamping: bridge_project + repos on inbound create --------------------


def test_resolve_inbound_bridge_fields_derives_from_prefix(tmp_path):
    """The helper derives ``bridge_project`` from the Jira key prefix and ``repos``
    from the mapping entry."""
    from rebar_reconciler import projects_store

    _seed_projects(tmp_path, {"DIG": ["rebar", "infra"]})
    fields = projects_store.resolve_inbound_bridge_fields("DIG-123", tmp_path)
    assert fields == {"bridge_project": "DIG", "repos": ["rebar", "infra"]}


def test_resolve_inbound_bridge_fields_unmapped_project_empty_repos(tmp_path):
    """A project absent from the mapping still resolves its prefix; repos default
    to empty."""
    from rebar_reconciler import projects_store

    _seed_projects(tmp_path, {"DIG": ["rebar"]})
    fields = projects_store.resolve_inbound_bridge_fields("REB-9", tmp_path)
    assert fields == {"bridge_project": "REB", "repos": []}


def test_inbound_create_stamps_bridge_project_and_repos(tmp_path):
    """An inbound-created ticket carries ``bridge_project`` (from the key prefix)
    and ``repos`` (from the mapping) on its CREATE event."""
    from rebar.reducer import reduce_ticket

    _apply_inbound = _load("_apply_inbound_1734_ut", "apply_inbound.py")
    _mutation = _load("_mutation_1734_ut", "mutation.py")
    _bs = _load("_binding_store_1734_ut", "binding_store.py")

    _seed_projects(tmp_path, {"DIG": ["rebar", "infra"]})
    bs = _bs.BindingStore(tmp_path / ".tickets-tracker")
    mutation = _mutation.Mutation(
        direction=_mutation.MutationDirection.inbound,
        action=_mutation.MutationAction.create,
        target="DIG-777",
        payload={"fields": {"summary": "native"}, "jira_fields": {"summary": "native"}},
        provenance={"source": "binding_walk", "drift_class": "B", "jira_key": "DIG-777"},
    )
    _apply_inbound._apply_inbound_create(
        mutation, client=None, repo_root=tmp_path, binding_store=bs
    )

    local_id = _apply_inbound._jira_key_to_local_id("DIG-777")
    state = reduce_ticket(str(tmp_path / ".tickets-tracker" / local_id))
    assert state is not None
    assert state["bridge_project"] == "DIG"
    assert state["repos"] == ["rebar", "infra"]
