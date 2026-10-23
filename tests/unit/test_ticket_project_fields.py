"""Held-out oracle for tri-state bridge_project and repos fields (cef7).

The implementer sees only happy-path create. Withheld tests cover absent,
empty, and keyed state, CLI/library/MCP parity, promote-only edit refusal,
and legacy replay. Assert reduced state, exit codes, and event replay rather
than private names."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar

pytestmark = pytest.mark.unit


# ── fixtures / helpers ────────────────────────────────────────────────────────
@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real initialized rebar store rooted at a throwaway git repo."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=subprocess_env(),
        check=False,
    )


def _created_id(proc: subprocess.CompletedProcess) -> str:
    """The canonical id inside ``create``'s one-line confirmation."""
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"\b[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}\b", proc.stdout)
    assert match, proc.stdout
    return match.group(0)


def _state(repo: Path, tid: str) -> dict:
    """Reduced ticket state as JSON (the observable ``rebar show`` contract)."""
    proc = _cli("show", tid, cwd=repo)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write_binding(repo: Path, local_id: str, jira_key: str) -> None:
    """Make ``local_id`` hold a tracker binding by writing the authoritative
    ``bindings.json`` the promote-only guard consults (``reverse`` maps the
    jira key back to the local id)."""
    bstate = _tracker(repo) / ".bridge_state"
    bstate.mkdir(parents=True, exist_ok=True)
    (bstate / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {local_id: {"jira_key": jira_key, "state": "confirmed"}},
                "reverse": {jira_key: local_id},
            }
        ),
        encoding="utf-8",
    )


# ── HAPPY PATH (handed to the implementer) ────────────────────────────────────
def test_create_cli_sets_bridge_project_and_repos(repo: Path) -> None:
    """A CLI create carrying both flags projects both fields after a full replay."""
    tid = _created_id(
        _cli("create", "task", "t", "--bridge-project", "REB", "--repos", "rebar,api", cwd=repo)
    )
    state = _state(repo, tid)
    assert state["bridge_project"] == "REB"
    assert state["repos"] == ["rebar", "api"]


# ── HELD OUT: orchestrator-only checks ───────────────────────────────────────


def test_three_states_are_distinguishable_after_replay(repo: Path) -> None:
    """absent (legacy) vs explicit-empty (never-sync) vs a key (sync) must be
    three DISTINGUISHABLE states after replay — not merely three falsy values."""
    absent = _state(repo, _created_id(_cli("create", "task", "no-flag", cwd=repo)))
    empty = _state(
        repo, _created_id(_cli("create", "task", "never", "--bridge-project", "", cwd=repo))
    )
    keyed = _state(
        repo, _created_id(_cli("create", "task", "sync", "--bridge-project", "REB", cwd=repo))
    )

    # Distinct states.
    assert absent.get("bridge_project") is None  # legacy / unset — the absent sentinel
    assert empty["bridge_project"] == ""  # explicit never-sync
    assert keyed["bridge_project"] == "REB"  # bound-project sync target

    # Both falsy values differ.
    assert absent.get("bridge_project") != empty["bridge_project"]

    # Unset repos defaults to [].
    assert absent.get("repos") == []


def test_bridge_project_and_repos_are_parity_across_surfaces(repo: Path) -> None:
    """CLI, library and MCP creates produce the same bridge_project / repos state."""
    # CLI
    cli_id = _created_id(
        _cli("create", "task", "cli", "--bridge-project", "REB", "--repos", "rebar,api", cwd=repo)
    )
    # Library
    lib_id = rebar.create_ticket(
        "task",
        "lib",
        bridge_project="REB",
        repos=["rebar", "api"],
        repo_root=str(repo),
    )
    # MCP write-tool registration.
    mcp_tools: dict = {}

    class _FakeMcp:
        def tool(self, *_a, annotations=None, **_k):
            def deco(fn):
                mcp_tools[fn.__name__] = fn
                return fn

            return deco

    ctx = types.SimpleNamespace(
        readonly=lambda: False,
        allow_llm=lambda: False,
        logger=logging.getLogger("test"),
    )
    from rebar import _mcp_writes

    _mcp_writes.register_write_tools(_FakeMcp(), ctx=ctx)
    mcp_res = mcp_tools["create_ticket"](
        "task", "mcp", bridge_project="REB", repos=["rebar", "api"]
    )
    mcp_id = mcp_res["id"]

    shapes = [
        (_state(repo, cli_id)["bridge_project"], _state(repo, cli_id)["repos"]),
        (_state(repo, lib_id)["bridge_project"], _state(repo, lib_id)["repos"]),
        (_state(repo, mcp_id)["bridge_project"], _state(repo, mcp_id)["repos"]),
    ]
    assert shapes == [("REB", ["rebar", "api"])] * 3


def test_mcp_edit_sets_bridge_project_and_repos_at_parity(repo: Path) -> None:
    """MCP edit_ticket forwards bridge_project and repos explicitly, matching
    CLI and library edits. Its enumerated signature has no **fields passthrough."""
    tid = rebar.create_ticket("task", "mcp-edit", repo_root=str(repo))
    assert _state(repo, tid).get("bridge_project") is None
    assert _state(repo, tid).get("repos") == []

    mcp_tools: dict = {}

    class _FakeMcp:
        def tool(self, *_a, annotations=None, **_k):
            def deco(fn):
                mcp_tools[fn.__name__] = fn
                return fn

            return deco

    ctx = types.SimpleNamespace(
        readonly=lambda: False,
        allow_llm=lambda: False,
        logger=logging.getLogger("test"),
    )
    from rebar import _mcp_writes

    _mcp_writes.register_write_tools(_FakeMcp(), ctx=ctx)
    mcp_tools["edit_ticket"](tid, bridge_project="REB", repos=["rebar", "api"])

    state = _state(repo, tid)
    assert state["bridge_project"] == "REB"
    assert state["repos"] == ["rebar", "api"]


def test_promote_only_refuses_rebind_but_allows_first_set(repo: Path) -> None:
    """Setting bridge_project on an UNBOUND ticket succeeds; the same edit on a
    ticket that already holds a binding is refused (non-zero) with no state change."""
    # Unbound → allowed.
    unbound = _created_id(_cli("create", "task", "unbound", cwd=repo))
    ok = _cli("edit", unbound, "--bridge-project", "REB", cwd=repo)
    assert ok.returncode == 0, ok.stderr
    assert _state(repo, unbound)["bridge_project"] == "REB"

    # Rebind refused.
    bound = _created_id(_cli("create", "task", "bound", "--bridge-project", "REB", cwd=repo))
    _write_binding(repo, bound, "REB-1")
    before = _state(repo, bound)["bridge_project"]
    refused = _cli("edit", bound, "--bridge-project", "DIG", cwd=repo)
    assert refused.returncode != 0
    assert _state(repo, bound)["bridge_project"] == before == "REB"


def test_legacy_create_event_replays_with_new_keys_defaulted(repo: Path) -> None:
    """Legacy CREATE events lacking bridge_project and repos replay with their
    defaults while preserving every other projected field."""
    from rebar.reducer import reduce_ticket

    tid = "abcd-1234-5678-9abc"
    tdir = _tracker(repo) / tid
    tdir.mkdir(parents=True)
    uuid = "00000000-0000-0000-0000-000000000000"
    event = {
        "event_type": "CREATE",
        "uuid": uuid,
        "timestamp": 1700000000000000000,
        "env_id": "test-env",
        "author": "legacy",
        "data": {
            "ticket_type": "task",
            "title": "legacy ticket",
            "priority": 2,
            "tags": ["x"],
            "description": "legacy body",
            "id": tid,
        },
    }
    (tdir / f"1700000000000000000-{uuid}-CREATE.json").write_text(
        json.dumps(event), encoding="utf-8"
    )
    state = reduce_ticket(str(tdir))
    assert state is not None
    # Preserve projection.
    assert state["ticket_type"] == "task"
    assert state["title"] == "legacy ticket"
    assert state["priority"] == 2
    assert state["tags"] == ["x"]
    assert state["status"] == "open"
    # Default new keys.
    assert state.get("bridge_project") is None
    assert state.get("repos") == []
