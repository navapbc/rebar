"""Full reconcile passes round-trip rebar releases <-> Jira fixVersions (ADR 0124).

Reuses the faithful stateful Jira fake and git-backed store fixtures from
``test_reconcile_idempotency.py`` (imported, not copied). Each scenario runs real
``reconcile_once`` passes with the Cloud and the Data Center backend wired over the same
fake transport, and asserts the exact Jira write calls and rebar RELEASE_DELTA events.
"""

# ruff: noqa: F811 — pytest fixtures are imported by name.
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_echo_loop_experiment import _bindings, _ensure_tracker_git, _make_local_ticket
from test_reconcile_idempotency import (  # noqa: F401
    _FakeClient,
    _FakeJiraState,
    _ok_snapshot_head,
    git_repo,
    reconciler_modules,
)

pytestmark = pytest.mark.integration

_LOCAL = "jira-reb-1"


class _RebClient(_FakeClient):
    """The shared fake, but new issues are keyed in the configured ``REB`` project
    (the shared fake mints ``DIG-*`` keys, which the cross-project guard refuses), and
    a created issue carries the empty ``fixVersions`` list live Jira search returns."""

    def create_issue(self, fields: dict) -> dict:
        key = super().create_issue(fields)["key"]
        reb_key = key.replace("DIG-", "REB-", 1)
        issue = self._s.issues.pop(key)
        issue["fixVersions"] = []
        self._s.issues[reb_key] = issue
        self._s.write_calls[-1] = f"create_issue->{reb_key}"
        return {"key": reb_key}


def _backend(kind: str, client: Any) -> Any:
    if kind == "cloud":
        from rebar.reconciler.adapters.jira.backend import JiraBackend

        return JiraBackend(client)
    from rebar.reconciler.adapters.jira_datacenter.backend import JiraDataCenterBackend

    return JiraDataCenterBackend(client)


@pytest.fixture(params=["cloud", "datacenter"])
def run_pass(request, reconciler_modules, git_repo, monkeypatch):
    """One reconcile pass over the fake, with the parametrized backend's mappers."""
    fetcher, applier, reconcile = reconciler_modules
    run_differs_mod = importlib.import_module("rebar.reconciler.run_differs")
    counter = {"n": 0}

    def _run(state: _FakeJiraState, target_mode: Any = None) -> dict:
        client = _RebClient(state)
        monkeypatch.setattr(fetcher, "_load_acli", lambda: client)
        monkeypatch.setattr(applier, "_load_acli", lambda: client)
        monkeypatch.setattr(applier, "snapshot_head", _ok_snapshot_head)
        monkeypatch.setattr(
            run_differs_mod, "_load_reconcile_backend", lambda: _backend(request.param, client)
        )
        counter["n"] += 1
        return reconcile.reconcile_once(
            f"rel-{counter['n']}", repo_root=git_repo, target_mode=target_mode
        )

    return _run


def _seed(state: _FakeJiraState, versions: tuple[str, ...] = ("2.4", "3.0", "jira-only")) -> None:
    # Versions are people-created in the Jira project (ADR 0124); the bridge only names them.
    state.project_versions = {"REB": [{"name": v, "archived": False} for v in versions]}
    state.seed(
        "REB-1",
        summary="Ship the release",
        status={"name": "To Do"},
        issuetype={"name": "Task"},
        priority={"name": "Medium"},
        fixVersions=[],
    )


def _tracker(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker"


def _local_release_delta(repo_root: Path, data: dict, local_id: str = _LOCAL) -> None:
    from rebar._commands import _seam

    # The applier writes inbound events straight to disk; the real store write
    # path needs the tracker to be a git work tree (as in the echo-loop harness).
    _ensure_tracker_git(_tracker(repo_root))
    _seam.append_event(
        local_id, "RELEASE_DELTA", data, _tracker(repo_root), repo_root=str(repo_root)
    )


def _release_deltas(repo_root: Path) -> list[dict]:
    files = sorted((_tracker(repo_root) / _LOCAL).glob("*-RELEASE_DELTA.json"))
    return [json.loads(p.read_text())["data"] for p in files]


def _releases(repo_root: Path) -> list[str]:
    import rebar

    return rebar.show_ticket(_LOCAL, repo_root=str(repo_root))["releases"]


def _jira_versions(state: _FakeJiraState) -> list[str]:
    return [v["name"] for v in state.issues["REB-1"].get("fixVersions", [])]


def _fix_version_writes(state: _FakeJiraState) -> list[str]:
    """The fix-version writes only. A bound update also issues the long-standing
    (empty) scalar ``update_issue`` call for any non-parent update, as label-only
    updates do (``dispatch_one._update_one_scalar_update``)."""
    return [c for c in state.write_calls if "fix_version" in c]


def _bound(run_pass, state: _FakeJiraState) -> None:
    """Import REB-1 and settle, so the scenarios start from a quiet bound pair."""
    _seed(state)
    run_pass(state)
    run_pass(state)
    state.write_calls.clear()


def test_local_add_then_remove_each_push_one_per_item_write(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _bound(run_pass, state)

    _local_release_delta(git_repo, {"added": ["2.4"], "removed": []})
    run_pass(state)
    assert _fix_version_writes(state) == ["add_fix_version(REB-1,2.4)"]
    assert _jira_versions(state) == ["2.4"]

    state.write_calls.clear()
    deltas_before = _release_deltas(git_repo)
    run_pass(state)
    assert state.write_calls == [], "a no-change pass must write nothing to Jira"
    assert _release_deltas(git_repo) == deltas_before, "...and nothing to rebar"

    _local_release_delta(git_repo, {"added": [], "removed": ["2.4"]})
    run_pass(state)
    assert _fix_version_writes(state) == ["remove_fix_version(REB-1,2.4)"]
    assert _jira_versions(state) == []


def test_jira_add_then_jira_remove_arrive_inbound_without_jira_writes(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _bound(run_pass, state)

    state.issues["REB-1"]["fixVersions"].append({"name": "3.0"})
    run_pass(state)
    assert _release_deltas(git_repo)[-1] == {"added": ["3.0"], "removed": [], "source": "inbound"}
    assert _releases(git_repo) == ["3.0"]
    assert state.write_calls == []

    run_pass(state)
    assert len(_release_deltas(git_repo)) == 1, "a repeat pass writes no rebar event"
    assert state.write_calls == []

    # Jira-side removal of an inbound-origin release is not undone by an outbound re-add.
    state.issues["REB-1"]["fixVersions"].clear()
    run_pass(state)
    assert _release_deltas(git_repo)[-1] == {"added": [], "removed": ["3.0"], "source": "inbound"}
    assert _releases(git_repo) == []
    assert state.write_calls == []
    run_pass(state)
    assert _jira_versions(state) == []
    assert state.write_calls == []


def test_jira_version_rebar_never_held_is_never_removed(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _seed(state)
    state.issues["REB-1"]["fixVersions"] = [{"name": "jira-only"}]
    run_pass(state)
    run_pass(state)
    run_pass(state)
    assert _jira_versions(state) == ["jira-only"]
    assert not any("remove_fix_version" in c for c in state.write_calls)


def test_new_issue_gets_its_release_on_the_pass_after_create(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _bound(run_pass, state)
    local_id = _make_local_ticket(git_repo, "Locally authored release work")
    _local_release_delta(git_repo, {"added": ["2.4"], "removed": []}, local_id)

    run_pass(state)  # creates the Jira issue; the create payload carries no releases
    key = _bindings(git_repo)[local_id]["jira_key"]
    assert not [c for c in _fix_version_writes(state) if key in c]

    state.write_calls.clear()
    run_pass(state)  # the bound-update diff now adds the release
    assert _fix_version_writes(state) == [f"add_fix_version({key},2.4)"]


# ── Missing / archived versions (ADR 0124 decisions 8, 9) ────────────────────


def _open_release_alerts(repo_root: Path) -> list[str]:
    import rebar

    alerts = rebar.show_ticket(_LOCAL, repo_root=str(repo_root))["bridge_alerts"]
    return sorted(
        a["reason"]
        for a in alerts
        if not a.get("resolved") and a["reason"].startswith("release-version-")
    )


def _alert_events(repo_root: Path) -> int:
    return len(list((_tracker(repo_root) / _LOCAL).glob("*-BRIDGE_ALERT.json")))


def test_missing_version_is_withheld_alerted_and_written_once_it_exists(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _seed(state, versions=())
    run_pass(state)
    run_pass(state)
    state.write_calls.clear()
    _local_release_delta(git_repo, {"added": ["2.4"], "removed": []})

    run_pass(state)  # pass 1: version missing -> withheld, kept, one alert
    assert _fix_version_writes(state) == []
    assert _releases(git_repo) == ["2.4"]
    assert _open_release_alerts(git_repo) == ["release-version-missing:2.4"]

    events = _alert_events(git_repo)
    state.write_calls.clear()
    run_pass(state)  # pass 2: steady state writes nothing
    assert state.write_calls == []
    assert _alert_events(git_repo) == events
    assert _releases(git_repo) == ["2.4"]

    state.project_versions["REB"].append({"name": "2.4", "archived": False})
    run_pass(state)  # pass 3: version exists -> add goes out, alert resolved
    assert _fix_version_writes(state) == ["add_fix_version(REB-1,2.4)"]
    assert _open_release_alerts(git_repo) == []

    state.write_calls.clear()
    events = _alert_events(git_repo)
    run_pass(state)  # pass 4: nothing
    assert state.write_calls == []
    assert _alert_events(git_repo) == events


def test_archived_version_is_withheld_with_the_archived_alert(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _seed(state, versions=())
    state.project_versions["REB"] = [{"name": "1.0", "archived": True}]
    run_pass(state)
    run_pass(state)
    _local_release_delta(git_repo, {"added": ["1.0"], "removed": []})
    state.write_calls.clear()
    run_pass(state)
    assert _fix_version_writes(state) == []
    assert _open_release_alerts(git_repo) == ["release-version-archived:1.0"]


def test_failed_version_read_writes_and_resolves_no_alert(run_pass, git_repo) -> None:
    state = _FakeJiraState()
    _seed(state, versions=())
    run_pass(state)
    run_pass(state)
    _local_release_delta(git_repo, {"added": ["2.4"], "removed": []})
    run_pass(state)
    assert _open_release_alerts(git_repo) == ["release-version-missing:2.4"]

    del state.project_versions["REB"]  # the fake now raises: availability unknown
    events = _alert_events(git_repo)
    run_pass(state)
    assert _alert_events(git_repo) == events
    assert _open_release_alerts(git_repo) == ["release-version-missing:2.4"]


def test_preview_pass_writes_no_alert(run_pass, git_repo) -> None:
    from rebar.reconciler.mode import Mode

    state = _FakeJiraState()
    _seed(state, versions=())
    run_pass(state)
    run_pass(state)
    _local_release_delta(git_repo, {"added": ["2.4"], "removed": []})
    events = _alert_events(git_repo)
    run_pass(state, target_mode=Mode.DRY_RUN)
    assert _alert_events(git_repo) == events
    assert _open_release_alerts(git_repo) == []
