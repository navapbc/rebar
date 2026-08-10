"""Held-out end-to-end oracle for library/MCP bridge parity."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from adapters import _unwrap

import rebar

pytestmark = pytest.mark.unit

LAST_PASS_REF = "refs/reconciler/last-pass"
GATE_REF = "refs/reconciler/gate"


def _tracker_bytes(repo: Path) -> dict[str, bytes]:
    tracker = repo / ".tickets-tracker"
    return {
        str(path.relative_to(tracker)): path.read_bytes()
        for path in tracker.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _refs(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show-ref"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


@pytest.fixture
def empty_acli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    bindir = tmp_path / "empty-bin"
    bindir.mkdir()
    executable = bindir / "acli"
    executable.write_text("#!/bin/sh\necho '[]'\nexit 0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("JIRA_PROJECT", "DIG")
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "reconciler-tests@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-api-token")
    return bindir


def test_preview_library_and_mcp_share_real_no_write_semantics(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_acli: Path,
) -> None:
    from rebar.mcp_server import build_server

    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    monkeypatch.delenv("REBAR_MCP_ALLOW_JIRA_SYNC", raising=False)
    before_refs = _refs(rebar_repo)
    before_tracker = _tracker_bytes(rebar_repo)

    library = rebar.bridge_preview(repo_root=rebar_repo)
    mcp = _unwrap(asyncio.run(build_server().call_tool("bridge_preview", {})))

    for result in (library, mcp):
        assert result["route"] == "preview"
        assert result["state"] == "converged"
        assert result["returncode"] == 0
        assert result["details"]["no_write"] is True
        assert result["details"]["mutations_applied"] == 0
    assert library["details"]["mutation_count"] == mcp["details"]["mutation_count"]
    assert _refs(rebar_repo) == before_refs
    assert _tracker_bytes(rebar_repo) == before_tracker


def _plant_blob(repo: Path, ref: str, payload: dict) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    oid = (
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", ref, oid],
        capture_output=True,
        check=True,
    )


def test_status_library_and_mcp_are_semantically_identical(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar.mcp_server import build_server

    _plant_blob(
        rebar_repo,
        LAST_PASS_REF,
        {
            "schema_version": 1,
            "pass_id": "status-e2e",
            "environment_id": "worker-a",
            "outcome": "success",
            "completed_at": "2026-08-09T12:00:00Z",
            "lock_fence": 8,
        },
    )
    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))

    library = rebar.bridge_status(target_environment_id="worker-a", repo_root=rebar_repo)
    mcp = _unwrap(
        asyncio.run(
            build_server().call_tool("bridge_status", {"target_environment_id": "worker-a"})
        )
    )

    assert mcp == library
    assert mcp["verdict"] == "HEALTHY"
    assert mcp["pass_id"] == "status-e2e"


def _configure_origin(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "HEAD"],
        check=True,
        capture_output=True,
    )
    return remote


def test_pause_and_resume_share_the_durable_control_ref_across_surfaces(
    rebar_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar.mcp_server import build_server

    _configure_origin(rebar_repo, tmp_path)
    subprocess.run(
        ["git", "-C", str(rebar_repo), "config", "user.email", "ops@example.com"],
        check=True,
    )
    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    monkeypatch.delenv("REBAR_MCP_READONLY", raising=False)
    monkeypatch.setenv("REBAR_MCP_ALLOW_JIRA_SYNC", "1")

    paused = rebar.bridge_pause("maintenance", repo_root=rebar_repo)
    assert paused["state"] == "paused"
    assert paused["reason"] == "maintenance"
    assert paused["who"] == "ops@example.com"
    status = _unwrap(
        asyncio.run(
            build_server().call_tool("bridge_status", {"target_environment_id": "reconciler"})
        )
    )
    assert status["verdict"] == "PAUSED"
    assert status["pause"]["reason"] == "maintenance"

    resumed = _unwrap(asyncio.run(build_server().call_tool("bridge_resume", {})))
    assert resumed["state"] == "resumed"
    assert (
        subprocess.run(
            ["git", "-C", str(rebar_repo), "rev-parse", "--verify", "--quiet", GATE_REF],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_check_access_returns_a_typed_six_step_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar_reconciler.adapters.jira import acli

    class FakeClient:
        def __init__(self, **_kwargs):
            self.issue_key = "DIG-1"
            self.property_value: str | None = None
            self.deleted = False

        def create_issue(self, _fields):
            return {"key": self.issue_key}

        def _direct_rest_put_raw(self, _path, _body):
            return None

        def set_issue_property(self, _key, _name, value):
            self.property_value = value

        def search_issues(self, _jql):
            return [{"key": self.issue_key}]

        def get_issue_property(self, _key, _name):
            return self.property_value

        def delete_issue(self, _key):
            self.deleted = True

    monkeypatch.setattr(acli, "AcliClient", FakeClient)
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "operator@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    monkeypatch.setenv("JIRA_PROJECT", "DIG")

    result = rebar.bridge_check_access()

    assert result["verdict"] == "PASS"
    assert [step["step"] for step in result["steps"]] == [
        "STEP_CREATE",
        "STEP_LABEL",
        "STEP_PROPERTY_WRITE",
        "STEP_JQL_SEARCH",
        "STEP_PROPERTY_READ",
        "STEP_DELETE",
    ]
    assert all(step["passed"] is True for step in result["steps"])
