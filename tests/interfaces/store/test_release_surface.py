"""Release surfaces over RELEASE_DELTA (ADR 0124, story waiting-nonspinal-ermine).

``rebar edit --add-release/--remove-release/--set-releases``, the library and MCP
``edit_ticket`` release keywords, ``rebar list --release`` / ``list_tickets(release=)``
(exact, OR across names, no parent inheritance), and the ``rl`` LLM-view key.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas
from rebar._commands import composer


def _seed(repo: Path, title: str = "Release surface", parent: str | None = None) -> str:
    return rebar.create_ticket(
        "epic" if title.startswith("Epic") else "task",
        title,
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        parent=parent,
        repo_root=str(repo),
    )


def _releases(repo: Path, tid: str) -> list[str]:
    return rebar.show_ticket(tid, repo_root=str(repo))["releases"]


def _release_deltas(repo: Path, tid: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(str(repo), ".tickets-tracker", tid, "*.json"))):
        with open(f) as fh:
            event = json.load(fh)
        if event.get("event_type") == "RELEASE_DELTA":
            out.append(event["data"])
    return out


def _edit(repo: Path, tid: str, *flags: str) -> int:
    return composer.edit_cli([tid, *flags], repo_root=str(repo))


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        check=False,
    )


# ── CLI edit ─────────────────────────────────────────────────────────────────


def test_add_release_writes_one_delta_and_show_displays_it(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--add-release", "2.4") == 0
    assert _release_deltas(rebar_repo, tid) == [{"added": ["2.4"], "removed": []}]
    shown = _cli(rebar_repo, "show", tid)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["releases"] == ["2.4"]

    assert _edit(rebar_repo, tid, "--add-release=2.4") == 0  # repeat: no-op
    assert len(_release_deltas(rebar_repo, tid)) == 1


def test_remove_release(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _edit(rebar_repo, tid, "--add-release", "2.4")
    assert _edit(rebar_repo, tid, "--remove-release", "2.4") == 0
    assert _release_deltas(rebar_repo, tid)[-1] == {"added": [], "removed": ["2.4"]}
    assert _releases(rebar_repo, tid) == []


def test_set_releases_compiles_an_add_wins_delta_and_empty_clears(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _edit(rebar_repo, tid, "--add-release", "2.4")
    assert _edit(rebar_repo, tid, "--set-releases", "2.5,2.6") == 0
    assert _release_deltas(rebar_repo, tid)[-1] == {"added": ["2.5", "2.6"], "removed": ["2.4"]}
    assert _edit(rebar_repo, tid, "--set-releases=") == 0
    assert _releases(rebar_repo, tid) == []


def test_set_with_add_is_an_error(rebar_repo: Path, capsys) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--set-releases", "2.5", "--add-release", "2.4") != 0
    assert "--set-releases cannot be combined" in capsys.readouterr().err
    assert _release_deltas(rebar_repo, tid) == []


def test_same_release_added_and_removed_is_an_error(rebar_repo: Path, capsys) -> None:
    tid = _seed(rebar_repo)
    assert _edit(rebar_repo, tid, "--add-release", "2.4", "--remove-release", "2.4") != 0
    assert "given to both --add-release and --remove-release" in capsys.readouterr().err


def test_releases_never_touch_tags(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _edit(rebar_repo, tid, "--add-tag", "keep", "--add-release", "2.4")
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["tags"] == ["keep"]
    assert state["releases"] == ["2.4"]


# ── CLI list ─────────────────────────────────────────────────────────────────


@pytest.fixture
def released(rebar_repo: Path) -> dict[str, str]:
    a = _seed(rebar_repo, "A")
    b = _seed(rebar_repo, "B")
    c = _seed(rebar_repo, "C")
    e = _seed(rebar_repo, "Epic E")
    d = _seed(rebar_repo, "D", parent=e)
    _edit(rebar_repo, a, "--add-release", "2.4")
    _edit(rebar_repo, b, "--add-release", "2.5")
    _edit(rebar_repo, e, "--add-release", "2.4")
    return {"A": a, "B": b, "C": c, "D": d, "E": e}


def _listed(repo: Path, *flags: str) -> set[str]:
    out = _cli(repo, "list", *flags)
    assert out.returncode == 0, out.stderr
    return {row["ticket_id"] for row in json.loads(out.stdout)}


def test_list_release_is_exact_with_no_inheritance(rebar_repo: Path, released) -> None:
    want = {released["A"], released["E"]}
    assert _listed(rebar_repo, "--release", "2.4") == want
    assert _listed(rebar_repo, "--release=2.4") == want


def test_list_release_or_and_unknown(rebar_repo: Path, released) -> None:
    assert _listed(rebar_repo, "--release", "2.4,2.5") == {
        released["A"],
        released["B"],
        released["E"],
    }
    assert _listed(rebar_repo, "--release", "9.9") == set()


# ── Library ──────────────────────────────────────────────────────────────────


def test_library_edit_and_list(rebar_repo: Path, released) -> None:
    c = released["C"]
    rebar.edit_ticket(c, add_releases=["3.0"], repo_root=str(rebar_repo))
    assert _releases(rebar_repo, c) == ["3.0"]
    rows = rebar.list_tickets(release="2.4", repo_root=str(rebar_repo))
    assert {r["ticket_id"] for r in rows} == {released["A"], released["E"]}


# ── MCP ──────────────────────────────────────────────────────────────────────


def _mcp(tool: str, **args: object) -> object:
    from rebar.mcp_server import build_server

    result = asyncio.run(build_server().call_tool(tool, args))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def _output_schema(tool: str) -> dict:
    from rebar.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    return next(t for t in tools if t.name == tool).outputSchema or {}


def test_mcp_edit_list_and_show_within_output_schemas(rebar_repo: Path, released) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    c = released["C"]
    _mcp("edit_ticket", ticket_id=c, add_releases=["3.0"])
    shown = _mcp("show_ticket", ticket_id=c)
    assert shown["releases"] == ["3.0"]
    jsonschema.validate(shown, _output_schema("show_ticket"))

    listed = _mcp("list_tickets", release="2.4")
    jsonschema.validate(listed, _output_schema("list_tickets"))
    rows = listed["result"] if isinstance(listed, dict) else listed
    assert {r["ticket_id"] for r in rows} == {released["A"], released["E"]}


# ── LLM view ─────────────────────────────────────────────────────────────────


def test_llm_view_carries_rl(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _edit(rebar_repo, tid, "--add-release", "2.4")
    out = _cli(rebar_repo, "show", tid, "--output", "llm")
    assert out.returncode == 0, out.stderr
    view = json.loads(out.stdout)
    assert view["rl"] == ["2.4"]
    schemas.validator(schemas.TICKET_STATE_LLM).validate(view)
