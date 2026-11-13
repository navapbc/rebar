"""Fix-version (``releases``) sync against live Jira Cloud, through real reconcile passes.

ADR 0124 sync was otherwise proven only against an in-memory Jira fake. These tests run real
``bootstrap-strict`` passes against the shared REB project, scoped with ``--filter-local-ids``
to one throwaway issue, so the Cloud version read and the per-item ``fixVersions`` writes in
``adapters/jira/acli_graph.py`` meet the real service.

Every pass fetches all of REB (about 2 minutes each, measured 2026-11-12), so the 4 scenarios
share 2 issues: one test walks outbound add, outbound remove and inbound on one issue; the
other covers a missing version, withheld with a BRIDGE_ALERT and written once it exists.

Each issue carries the run label and is deleted by key in ``finally``; each version has a
run-scoped name and is deleted in ``finally``. The conftest backstop and the workflow's
failure/cancel step sweep both by the run label and the run prefix.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from _cloud_mutation_support import (
    build_cloud_client,
    cloud_project,
    create_version,
    delete_version,
    engine_on_path,
    run_label,
    run_prefix,
    version_name,
)

engine_on_path()

import rebar  # noqa: E402

_live_jira_ready = True

_WRITING_MODE = "bootstrap-strict"
_ALERT_PREFIX = "release-version-"
_UPDATE_LINE = re.compile(r"RECON: outbound_update key=(\S+) changed=\[([^\]]*)\] .*releases=(\d+)")


def _pass(store: Path, local_id: str, what: str) -> str:
    """Run one writing pass scoped to ``local_id`` (and its bound key); return its stderr."""
    from rebar._engine import engine_env

    argv = [
        sys.executable,
        "-m",
        "rebar.reconciler",
        "--mode",
        _WRITING_MODE,
        "--repo-root",
        str(store),
        "--filter-local-ids",
        local_id,
    ]
    cp = subprocess.run(
        argv, env=engine_env(str(store)), text=True, capture_output=True, check=False
    )
    assert cp.returncode == 0, (
        f"{what} failed (rc={cp.returncode})\nstdout:\n{cp.stdout[-2000:]}\n"
        f"stderr:\n{cp.stderr[-3000:]}"
    )
    return cp.stderr or ""


@contextmanager
def _bound_issue(store: Path, title: str) -> Iterator[tuple[str, str, Any]]:
    """Create a local ticket, bind it with one scoped pass, label it, and delete it after.

    The pass's outbound create files exactly one REB issue labelled ``rebar-id:<local_id>``,
    with the run prefix in its summary. The run label is added straight after; until then the
    backstops find the issue by that summary, and ``finally`` also looks it up by its
    ``rebar-id`` label in case the pass created it but did not record the binding.
    """
    local_id = rebar.create_ticket("task", f"{title} {run_prefix()}", repo_root=str(store))
    client = build_cloud_client()
    key: str | None = None
    try:
        _pass(store, local_id, "binding pass")
        from rebar.reconciler.binding_store import load_binding_store

        key = load_binding_store(store).get_jira_key(local_id)
        assert key, f"the binding pass did not bind {local_id} to a REB issue"
        client.add_label(key, run_label())
        yield local_id, key, client
    finally:
        keys = {key} if key else set()
        if not keys:
            client.invalidate_search_cache()
            keys = {h.get("key") for h in client.search_issues(f'labels = "rebar-id:{local_id}"')}
        for found in filter(None, keys):
            client.delete_issue(found)


@contextmanager
def _versions(client: Any, *names: str) -> Iterator[None]:
    """Create run-scoped versions and delete every one that was created, even on failure."""
    created: list[str] = []
    try:
        for name in names:
            created.append(create_version(client, cloud_project(), name))
        yield
    finally:
        for version_id in created:
            delete_version(client, version_id)


def _jira_versions(key: str) -> list[str]:
    """``fixVersions`` read back from Jira by REST, not from any local projection."""
    fields = build_cloud_client().get_issue_by_rest(key).get("fields") or {}
    return sorted(v["name"] for v in fields.get("fixVersions") or [])


def _wait_until_search_shows(
    key: str, predicate: Callable[[list[str]], bool], what: str, timeout: float = 180.0
) -> None:
    """Wait until JQL shows ``fixVersions`` accepted by ``predicate``.

    A pass diffs against search results, and Cloud's index lags writes; a pass run before the
    index catches up would re-plan a write that already landed.
    """
    deadline = time.monotonic() + timeout
    last: list[str] | None = None
    while time.monotonic() < deadline:
        client = build_cloud_client()
        client.invalidate_search_cache()
        for hit in client.search_issues(f'key = "{key}"'):
            if hit.get("key") == key:
                fields = hit.get("fields") or {}
                last = sorted(v["name"] for v in fields.get("fixVersions") or [])
                if predicate(last):
                    return
        time.sleep(5.0)
    raise AssertionError(
        f"search never showed {what} on {key} within {timeout:.0f}s; last: {last!r}. "
        "This is index lag, not a bridge defect."
    )


def _releases(store: Path, local_id: str) -> list[str]:
    return sorted(rebar.show_ticket(local_id, repo_root=str(store)).get("releases") or [])


def _release_alerts(store: Path, local_id: str) -> list[dict[str, Any]]:
    alerts = rebar.show_ticket(local_id, repo_root=str(store)).get("bridge_alerts") or []
    return [a for a in alerts if str(a.get("reason", "")).startswith(_ALERT_PREFIX)]


def _assert_one_release_item(stderr: str, key: str) -> None:
    """The pass wrote exactly one per-item fixVersions change for ``key``.

    Jira's end state cannot tell a per-item ``update`` from a whole-list ``fields`` replace,
    because rebar holds the full intended set. The pass's own outbound line can.
    """
    lines = [m for m in _UPDATE_LINE.finditer(stderr) if m.group(1) == key]
    assert len(lines) == 1, f"expected one outbound_update for {key}; stderr:\n{stderr[-2000:]}"
    changed, releases = lines[0].group(2).split(","), int(lines[0].group(3))
    assert "fixVersions" not in changed, f"fixVersions was written as a whole field: {changed}"
    assert releases == 1, f"expected 1 per-item release write, got {releases}"


def test_releases_round_trip_on_one_issue(cloud_store: Path) -> None:
    """Scenarios 2, 3 and 1 on one issue: outbound add per item, outbound remove, inbound."""
    first, second, inbound = (version_name(x) for x in ("add-a", "add-b", "inbound"))
    with (
        _bound_issue(cloud_store, "[rebar cloud-mutation probe] fix versions") as (
            local_id,
            key,
            client,
        ),
        _versions(client, first, second, inbound),
    ):
        # Scenario 2: each release added in rebar is written, and the first one stays.
        rebar.edit_ticket(local_id, add_releases=[first], repo_root=str(cloud_store))
        _pass(cloud_store, local_id, "outbound add pass 1")
        assert _jira_versions(key) == [first]
        _wait_until_search_shows(key, lambda v: v == [first], f"fixVersion {first}")

        rebar.edit_ticket(local_id, add_releases=[second], repo_root=str(cloud_store))
        stderr = _pass(cloud_store, local_id, "outbound add pass 2")
        _assert_one_release_item(stderr, key)
        both = sorted([first, second])
        assert _jira_versions(key) == both
        _wait_until_search_shows(key, lambda v: v == both, "both fixVersions")

        # Scenario 3: a release removed in rebar leaves fixVersions; the other one stays.
        rebar.edit_ticket(local_id, remove_releases=[first], repo_root=str(cloud_store))
        stderr = _pass(cloud_store, local_id, "outbound remove pass")
        _assert_one_release_item(stderr, key)
        assert _jira_versions(key) == [second]
        assert _releases(cloud_store, local_id) == [second]

        # Scenario 1: a version set on the issue in Jira arrives in the ticket's releases.
        client.add_fix_version(key, inbound)
        _wait_until_search_shows(
            key, lambda v: v == sorted([second, inbound]), f"fixVersion {inbound}"
        )
        _pass(cloud_store, local_id, "inbound pass")
        assert _releases(cloud_store, local_id) == sorted([second, inbound])
        assert _jira_versions(key) == sorted([second, inbound]), "the inbound release was echoed"


def test_missing_version_is_withheld_then_written(cloud_store: Path) -> None:
    """Scenario 4: a release for a version REB lacks is withheld with a BRIDGE_ALERT; once the
    version exists, the next pass writes it and resolves the alert."""
    name = version_name("missing")
    with _bound_issue(cloud_store, "[rebar cloud-mutation probe] missing version") as (
        local_id,
        key,
        client,
    ):
        rebar.edit_ticket(local_id, add_releases=[name], repo_root=str(cloud_store))
        _pass(cloud_store, local_id, "pass with the version missing")
        assert _jira_versions(key) == []
        assert _releases(cloud_store, local_id) == [name]
        alerts = _release_alerts(cloud_store, local_id)
        assert [a["reason"] for a in alerts if not a["resolved"]] == [
            f"release-version-missing:{name}"
        ]

        with _versions(client, name):
            _pass(cloud_store, local_id, "pass after the version exists")
            assert _jira_versions(key) == [name]
            alerts = _release_alerts(cloud_store, local_id)
            assert [a for a in alerts if not a["resolved"]] == [], "the alert did not clear"
            assert [a for a in alerts if a["resolved"]], "the alert was never resolved"
            client.remove_fix_version(key, name)
