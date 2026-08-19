"""Held-out surface-parity oracle for ``bridge projects`` (story c927).

The five-slot census the happy-path implementer does not see proven: one record read
identically through the library, the CLI, and MCP; the MCP read tool annotated
READ_ONLY while set/remove are registered as write tools; and the pinned group help +
argparse metavar both naming the new verb.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import rebar
from rebar._cli._bridge_commands import bridge_cli

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict = {}
        self.anns: dict = {}

    def tool(self, *_a, annotations=None, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.anns[fn.__name__] = annotations
            return fn

        return deco


def _read_tools() -> _FakeMcp:
    from rebar import _mcp_reads

    m = _FakeMcp()
    ctx = types.SimpleNamespace(
        readonly=False,
        allow_jira_sync=False,
        cap_workflow_payload=lambda *a, **k: None,
        MODE_CAPS={},
        Mode=None,
    )
    _mcp_reads.register_read_tools(m, ctx=ctx)
    return m


def _write_tools() -> _FakeMcp:
    from rebar import _mcp_writes

    m = _FakeMcp()
    ctx = types.SimpleNamespace(
        readonly=lambda: False,
        dump=lambda obj: obj,
        allow_llm=lambda: False,
        logger=logging.getLogger("test"),
    )
    _mcp_writes.register_write_tools(m, ctx=ctx)
    return m


def test_one_record_read_identically_through_lib_cli_and_mcp(repo: Path) -> None:
    """A write through the library is read back byte-equal through the CLI and MCP."""
    rebar.bridge_projects_set("REB", ["rebar"], repo_root=str(repo))
    expected = {"REB": {"repos": ["rebar"]}}

    # CLI read.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bridge_cli(["projects", "list"])
    assert rc == 0
    assert json.loads(buf.getvalue()) == expected

    # MCP read.
    mcp_list = _read_tools().tools["bridge_projects_list"]
    assert json.loads(json.dumps(mcp_list())) == expected


def test_mcp_list_is_read_only_and_set_remove_are_write_tools() -> None:
    """list rides the READ_ONLY read registrar; set/remove ride the write registrar."""
    reads = _read_tools()
    writes = _write_tools()

    assert "bridge_projects_list" in reads.tools
    assert reads.anns["bridge_projects_list"].readOnlyHint is True

    assert "bridge_projects_set" in writes.tools
    assert "bridge_projects_remove" in writes.tools
    assert writes.anns["bridge_projects_set"].readOnlyHint is False
    assert writes.anns["bridge_projects_remove"].readOnlyHint is False

    # A mutation must never be registered as a read tool.
    assert "bridge_projects_set" not in reads.tools
    assert "bridge_projects_remove" not in reads.tools


def test_pinned_group_help_and_metavar_name_projects() -> None:
    """The pinned bridge group help and the argparse subcommand metavar both list ``projects``."""
    from rebar._cli import _bridge_commands, _help

    help_text = _help.subcommand_help("bridge")
    assert help_text is not None
    assert "projects" in help_text

    parser = _bridge_commands._parser()
    subactions = [a for a in parser._actions if a.dest == "command"]
    assert subactions, "bridge parser has no command subparsers action"
    assert "projects" in subactions[0].metavar


def test_invalid_key_is_rebarerror_through_lib_and_mcp(repo: Path) -> None:
    """A syntactically-invalid key (ticket 209b) surfaces as one clean ``RebarError``
    (returncode 2) through BOTH the library and the MCP write tools — the same error
    contract the consumers already handle for ``remove``'s absent-key case — and writes
    nothing."""
    from rebar._errors import RebarError

    projects_file = repo / ".tickets-tracker" / ".bridge_state" / "projects.json"
    before = projects_file.read_bytes() if projects_file.exists() else None

    # Library surface.
    with pytest.raises(RebarError) as lib_set:
        rebar.bridge_projects_set("le-g", ["rebar"], repo_root=str(repo))
    assert lib_set.value.returncode == 2
    with pytest.raises(RebarError):
        rebar.bridge_projects_remove("le-g", repo_root=str(repo))

    # MCP write-tool surface (invalid key raises the same clean RebarError).
    writes = _write_tools()
    with pytest.raises(RebarError):
        writes.tools["bridge_projects_set"]("1x", ["rebar"])
    with pytest.raises(RebarError):
        writes.tools["bridge_projects_remove"]("1x")

    after = projects_file.read_bytes() if projects_file.exists() else None
    assert after == before


def test_bridge_projects_set_commits_and_leaves_a_clean_tree(repo: Path) -> None:
    """``bridge_projects_set`` commits the mapping itself and leaves NOTHING staged or
    dirty in the tracker (ticket b783).

    Regression for the live-DC rehearsal defect: since the write routes through
    ``commit_and_push_tickets_branch`` (commit under the write lock, independent of push
    policy — ticket fea4), a follow-up manual ``git add``/``git commit`` finds "nothing to
    commit" and fails. This asserts the invariant that made that manual commit stale: after
    the call the blob is in the committed tree AND the working tree is clean.
    """
    rebar.bridge_projects_set("REB", ["rebar"], repo_root=str(repo))
    tracker = repo / ".tickets-tracker"

    # The mapping blob is tracked (committed), not merely present in the worktree.
    tracked = subprocess.run(
        ["git", "ls-files", ".bridge_state/projects.json"],
        cwd=tracker,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked.strip() == ".bridge_state/projects.json", (
        "bridge_projects_set did not commit the projects.json blob"
    )

    # And the committed blob carries the mapping we just wrote.
    committed = subprocess.run(
        ["git", "show", "HEAD:.bridge_state/projects.json"],
        cwd=tracker,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert json.loads(committed)["projects"] == {"REB": {"repos": ["rebar"]}}

    # Nothing is left staged or dirty — a subsequent `git commit` would find nothing.
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tracker,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert porcelain == "", f"bridge_projects_set left an unclean tracker tree: {porcelain!r}"
