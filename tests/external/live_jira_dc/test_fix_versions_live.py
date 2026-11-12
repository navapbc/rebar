"""Fix-version (``releases``) sync against the live Docker Jira Data Center server.

ADR 0124 sync was otherwise proven only against an in-memory Jira fake
(``tests/integration/reconciler/test_reconcile_releases_roundtrip.py``). These scenarios run
real scoped reconcile passes against DC, so the DC version read (``get_project_versions``) and
the per-item ``fixVersions`` writes in ``adapters/jira_datacenter/_issues.py`` meet a real server:

1. inbound: a version set on the issue through DC appears in the ticket's ``releases``;
2. outbound add: each release added in rebar is written, per item, and earlier ones stay;
3. outbound remove: a release removed in rebar leaves ``fixVersions``; the others stay;
4. a release naming a version the project lacks is withheld with a BRIDGE_ALERT, and once the
   version exists a later pass writes it and resolves the alert.

Each test gets its own scratch project (``jira_dc_project``) and bound issue (``bound_dc_issue``).
Every version a test creates is deleted in ``finally``; project teardown is the backstop.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from _child_diag import assert_child_ran_clean
from _dc_support import live_jira_ready
from _dc_support import read_local_ticket as _local
from _dc_support import run_reconcile as _run
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

import rebar

pytestmark = pytest.mark.external

# Re-export the exact sentinel name used to mark this module for the all-skipped live-test canary.
_live_jira_ready = live_jira_ready

_WRITING_MODE = "bootstrap-strict"
_ALERT_PREFIX = "release-version-"


def _version_name(label: str) -> str:
    """A version name no prior run can have created, so no assertion passes on a stale read."""
    return f"rbj-{label}-{uuid.uuid4().hex[:8]}"


@contextmanager
def _versions(dc_request: Any, project: str, *names: str) -> Iterator[dict[str, str]]:
    """Create ``names`` in ``project`` and delete every one that was created, even on failure."""
    created: dict[str, str] = {}
    try:
        for name in names:
            created[name] = _create_version(dc_request, project, name)
        yield created
    finally:
        for version_id in created.values():
            _delete_version(dc_request, version_id)


def _create_version(dc_request: Any, project: str, name: str) -> str:
    status, body = dc_request(
        "/rest/api/2/version", method="POST", payload={"name": name, "project": project}
    )
    assert status == 201, f"creating version {name!r} in {project} returned {status}: {body!r}"
    return str(body["id"])


def _delete_version(dc_request: Any, version_id: str) -> None:
    status, body = dc_request(f"/rest/api/2/version/{version_id}", method="DELETE")
    assert status in (204, 404), f"deleting version {version_id} returned {status}: {body!r}"


def _jira_versions(dc_transport: Any, key: str) -> list[str]:
    """``fixVersions`` read back from the DC instance, not from any local projection."""
    fields = dc_transport.get_issue_by_rest(key).get("fields") or {}
    return sorted(v["name"] for v in fields.get("fixVersions") or [])


def _releases(repo: Path, local_id: str) -> list[str]:
    return sorted(_local(repo, local_id).get("releases") or [])


def _open_release_alerts(repo: Path, local_id: str) -> list[str]:
    alerts = _local(repo, local_id).get("bridge_alerts") or []
    return sorted(
        a["reason"]
        for a in alerts
        if not a.get("resolved") and str(a.get("reason", "")).startswith(_ALERT_PREFIX)
    )


def _all_release_alerts(repo: Path, local_id: str) -> list[dict[str, Any]]:
    alerts = _local(repo, local_id).get("bridge_alerts") or []
    return [a for a in alerts if str(a.get("reason", "")).startswith(_ALERT_PREFIX)]


def _pass(repo: Path, local_id: str, key: str, what: str) -> str:
    """Run one scoped writing pass and return its stderr."""
    cp = _run(repo, _WRITING_MODE, only=f"{local_id},{key}")
    assert_child_ran_clean(cp, what=what)
    return cp.stderr or ""


_UPDATE_LINE = re.compile(r"RECON: outbound_update key=(\S+) changed=\[([^\]]*)\] .*releases=(\d+)")


def _assert_one_release_item(stderr: str, key: str) -> None:
    """The pass wrote exactly one per-item fixVersions change for ``key``.

    Jira's end state cannot tell a per-item ``update`` from a whole-list ``fields`` replace,
    because rebar holds the full intended set. The pass's own outbound line can: per-item
    writes count under ``releases=``, and a whole-list write would show ``fixVersions`` in
    ``changed``.
    """
    lines = [m for m in _UPDATE_LINE.finditer(stderr) if m.group(1) == key]
    assert len(lines) == 1, f"expected one outbound_update for {key}; stderr:\n{stderr[-2000:]}"
    changed, releases = lines[0].group(2).split(","), int(lines[0].group(3))
    assert "fixVersions" not in changed, f"fixVersions was written as a whole field: {changed}"
    assert releases == 1, f"expected 1 per-item release write, got {releases}"


def _wait_until_search_reflects(
    transport: Any,
    project: str,
    key: str,
    predicate: Callable[[list[str]], bool],
    what: str,
    timeout: float = 90.0,
) -> None:
    """Wait until JQL returns ``key`` with ``fixVersions`` accepted by ``predicate``.

    The inbound differ reads the search result, so waiting for the exact change keeps index lag
    from masquerading as a bridge defect.
    """
    deadline = time.monotonic() + timeout
    last: list[str] | None = None
    while time.monotonic() < deadline:
        for hit in transport.search_issues(f'project = "{project}" AND key = "{key}"'):
            if hit.get("key") == key:
                fields = hit.get("fields") or {}
                last = sorted(v["name"] for v in fields.get("fixVersions") or [])
                if predicate(last):
                    return
        time.sleep(2.0)
    raise AssertionError(
        f"the index never reflected {what} on {key} within {timeout:.0f}s; last indexed "
        f"fixVersions: {last!r}. This is search lag, not a bridge defect."
    )


@_skip
@_skip_no_extra
def test_inbound_fix_version_arrives_as_a_release(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    dc_request: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """Scenario 1: a version set on the DC issue appears in the bound ticket's releases."""
    local_id, key = bound_dc_issue
    name = _version_name("inbound")
    with _versions(dc_request, jira_dc_project, name):
        dc_transport.add_fix_version(key, name)
        _wait_until_search_reflects(
            dc_transport, jira_dc_project, key, lambda v: name in v, f"fixVersion {name}"
        )

        _pass(dc_store_copy_repo, local_id, key, "inbound pass")

        assert _releases(dc_store_copy_repo, local_id) == [name]
        # An inbound release is not echoed back: Jira still holds exactly the one version.
        assert _jira_versions(dc_transport, key) == [name]
        dc_transport.remove_fix_version(key, name)


@_skip
@_skip_no_extra
def test_outbound_add_writes_each_release_and_keeps_the_others(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    dc_request: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """Scenario 2: each release added in rebar is written per item; the first one stays."""
    local_id, key = bound_dc_issue
    first, second = _version_name("add-a"), _version_name("add-b")
    with _versions(dc_request, jira_dc_project, first, second):
        rebar.edit_ticket(local_id, add_releases=[first], repo_root=str(dc_store_copy_repo))
        _pass(dc_store_copy_repo, local_id, key, "outbound add pass 1")
        assert _jira_versions(dc_transport, key) == [first]
        # The next pass diffs against search, so let search catch up or it re-plans `first`.
        _wait_until_search_reflects(
            dc_transport, jira_dc_project, key, lambda v: v == [first], f"fixVersion {first}"
        )

        rebar.edit_ticket(local_id, add_releases=[second], repo_root=str(dc_store_copy_repo))
        stderr = _pass(dc_store_copy_repo, local_id, key, "outbound add pass 2")
        _assert_one_release_item(stderr, key)
        assert _jira_versions(dc_transport, key) == sorted([first, second])
        assert _releases(dc_store_copy_repo, local_id) == sorted([first, second])
        for name in (first, second):
            dc_transport.remove_fix_version(key, name)


@_skip
@_skip_no_extra
def test_outbound_remove_drops_only_that_release(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    dc_request: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """Scenario 3: a release removed in rebar leaves fixVersions; the other entry stays."""
    local_id, key = bound_dc_issue
    kept, dropped = _version_name("keep"), _version_name("drop")
    with _versions(dc_request, jira_dc_project, kept, dropped):
        rebar.edit_ticket(local_id, add_releases=[kept, dropped], repo_root=str(dc_store_copy_repo))
        _pass(dc_store_copy_repo, local_id, key, "outbound add pass")
        assert _jira_versions(dc_transport, key) == sorted([kept, dropped])
        # A remove is planned only for a version search shows on the issue.
        both = sorted([kept, dropped])
        _wait_until_search_reflects(
            dc_transport, jira_dc_project, key, lambda v: v == both, "both fixVersions"
        )

        rebar.edit_ticket(local_id, remove_releases=[dropped], repo_root=str(dc_store_copy_repo))
        stderr = _pass(dc_store_copy_repo, local_id, key, "outbound remove pass")
        _assert_one_release_item(stderr, key)

        assert _jira_versions(dc_transport, key) == [kept]
        assert _releases(dc_store_copy_repo, local_id) == [kept]
        dc_transport.remove_fix_version(key, kept)


@_skip
@_skip_no_extra
def test_missing_version_is_withheld_alerted_then_written_once_it_exists(
    dc_store_copy_repo: Path,
    dc_transport: Any,
    dc_request: Any,
    jira_dc_project: str,
    bound_dc_issue: Any,
) -> None:
    """Scenario 4: a release for a version DC lacks is withheld with a BRIDGE_ALERT; once the
    version is created, a later pass writes it and resolves the alert."""
    local_id, key = bound_dc_issue
    name = _version_name("missing")
    rebar.edit_ticket(local_id, add_releases=[name], repo_root=str(dc_store_copy_repo))

    _pass(dc_store_copy_repo, local_id, key, "pass with the version missing")
    assert _jira_versions(dc_transport, key) == []
    assert _releases(dc_store_copy_repo, local_id) == [name]
    assert _open_release_alerts(dc_store_copy_repo, local_id) == [f"release-version-missing:{name}"]

    _pass(dc_store_copy_repo, local_id, key, "repeat pass with the version still missing")
    assert len(_all_release_alerts(dc_store_copy_repo, local_id)) == 1, "the alert was re-raised"

    with _versions(dc_request, jira_dc_project, name):
        _pass(dc_store_copy_repo, local_id, key, "pass after the version exists")
        assert _jira_versions(dc_transport, key) == [name]
        assert _open_release_alerts(dc_store_copy_repo, local_id) == []
        resolved = [a for a in _all_release_alerts(dc_store_copy_repo, local_id) if a["resolved"]]
        assert resolved, "the missing-version alert was never resolved"
        dc_transport.remove_fix_version(key, name)
