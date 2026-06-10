"""`ready` JSON-shape contract across the CLI, library, and MCP interfaces.

Regression cover for the bug where the library `ready()` (and therefore the MCP
`ready_tickets` tool) invoked `ready` with no structured-output flag, but the engine `ready` had no
`--output json` form — so every call raised RebarError / ToolError.

The engine now emits a single JSON ARRAY of compiled ticket-state dicts for
`ready --output json` (same element shape as `list`/`search`). These tests assert that
contract through all three interfaces over one shared store (REBAR_ROOT set by
the `rebar_repo` fixture in conftest.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import rebar

from adapters import McpAdapter


def _cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True, text=True, cwd=cwd,
    )


def test_cli_ready_json_emits_array(rebar_repo: Path) -> None:
    """`rebar ready --output json` must exit 0 and print a single JSON array whose
    elements are compiled ticket-state dicts (keyed by `ticket_id`)."""
    ready_id = rebar.create_ticket("task", "Ready one", repo_root=str(rebar_repo))

    cp = _cli("ready", "--output", "json", cwd=str(rebar_repo))
    assert cp.returncode == 0, cp.stderr

    data = json.loads(cp.stdout)
    assert isinstance(data, list)
    ids = {t["ticket_id"] for t in data}
    assert ready_id in ids


def test_cli_ready_default_unchanged(rebar_repo: Path) -> None:
    """The default (no-flag) output is still a newline-separated id list, not
    JSON — `--output json` must not regress the bare `ready` contract."""
    ready_id = rebar.create_ticket("task", "Ready one", repo_root=str(rebar_repo))

    cp = _cli("ready", cwd=str(rebar_repo))
    assert cp.returncode == 0, cp.stderr
    lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
    assert ready_id in lines
    # Bare ids, not JSON.
    assert not cp.stdout.lstrip().startswith("[")


def test_library_ready_returns_list(rebar_repo: Path) -> None:
    """`rebar.ready()` parses the engine `--output json` array into a Python list
    (previously raised RebarError because `--json` was unrecognized (now `--output json`))."""
    ready_id = rebar.create_ticket("task", "Ready one", repo_root=str(rebar_repo))

    result = rebar.ready(repo_root=str(rebar_repo))
    assert isinstance(result, list)
    assert ready_id in {t["ticket_id"] for t in result}


def test_mcp_ready_tickets_returns_data(rebar_repo: Path) -> None:
    """The MCP `ready_tickets` tool returns the ready set without raising a
    ToolError (the user-visible symptom of the missing structured-output flag).

    Two ready tickets are created so the McpAdapter's `_unwrap` keeps the result
    as a list (it collapses a single-element content list to one dict)."""
    id1 = rebar.create_ticket("task", "Ready one", repo_root=str(rebar_repo))
    id2 = rebar.create_ticket("task", "Ready two", repo_root=str(rebar_repo))

    result = McpAdapter().ready()
    assert isinstance(result, list)
    ids = {t["ticket_id"] for t in result}
    assert {id1, id2} <= ids


def test_ready_output_excludes_blocked(rebar_repo: Path) -> None:
    """A ticket whose blocker is still open must NOT appear in the `--output json`
    array — the ready filter is preserved through the new output path."""
    blocker = rebar.create_ticket("task", "Blocker", repo_root=str(rebar_repo))
    blocked = rebar.create_ticket("task", "Blocked", repo_root=str(rebar_repo))
    rebar.link(blocked, blocker, "depends_on", repo_root=str(rebar_repo))

    result = rebar.ready(repo_root=str(rebar_repo))
    ids = {t["ticket_id"] for t in result}
    assert blocker in ids  # no blockers itself → ready
    assert blocked not in ids  # blocker still open → not ready
