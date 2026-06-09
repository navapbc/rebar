"""WS5d: quality gates + file-impact exposed on the library (and MCP).

The MCP surface/gating is asserted in test_surface.py and test_mcp.py; here we
exercise the library functions end-to-end (round-trips + check shapes).
"""

from __future__ import annotations

from pathlib import Path

import rebar


def test_file_impact_round_trip(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "impacts files")
    assert rebar.get_file_impact(tid) == []
    impact = [{"path": "src/foo.py", "reason": "modified"}]
    rebar.set_file_impact(tid, impact)
    got = rebar.get_file_impact(tid)
    paths = {e.get("path") for e in got}
    assert "src/foo.py" in paths


def test_verify_commands_round_trip(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "has verify cmds")
    assert rebar.get_verify_commands(tid) == []
    cmds = [{"dd_id": "DD1", "dd_text": "builds", "command": "make build"}]
    rebar.set_verify_commands(tid, cmds)
    got = rebar.get_verify_commands(tid)
    assert any(e.get("dd_id") == "DD1" for e in got)


def test_quality_gates_return_passed_shape(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Quality probe", description="Some description")
    for fn in (rebar.clarity_check, rebar.check_ac, rebar.quality_check):
        result = fn(tid)
        assert isinstance(result, dict)
        assert "passed" in result and isinstance(result["passed"], bool)
    v = rebar.validate(tid)
    assert isinstance(v, dict)
