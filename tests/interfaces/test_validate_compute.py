"""validate characterization (Tier C, post-retirement) — docs/bash-migration.md §5.

Tier C was retired on 2026-06-12: the bash ``validate-issues.sh`` and the
``REBAR_COMPUTE`` switch are gone, so this no longer dual-runs bash-vs-python. It
is now the durable python characterization (the a93885ed pattern): the score→exit
tiers, the JSON report shape, the human text/terse goldens, and library/schema —
driven through the dispatcher with the same ``TICKET_CMD`` injection the port
honors.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_DISPATCHER = _PLUGIN_ROOT / "src" / "rebar" / "_engine" / "ticket"


def _mock_ticket_cmd(tmp_path: Path, tickets: list[dict]) -> str:
    (tmp_path / "tickets.json").write_text(json.dumps(tickets))
    script = tmp_path / "ticket"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f'  list) cat {tmp_path / "tickets.json"} ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _run(ticket_cmd: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TICKET_CMD": ticket_cmd, "REBAR_NO_SYNC": "1"}
    return subprocess.run([str(_DISPATCHER), "validate", *args], env=env, capture_output=True, text=True)


def _ticket(tid, status, ttype, parent=None, title=None, desc="yes", notes="", deps=None):
    return {
        "ticket_id": tid,
        "status": status,
        "ticket_type": ttype,
        "title": title or f"Ticket {tid}",
        "parent_id": parent,
        "description": desc,
        "notes": notes,
        "deps": deps or [],
        "created_at": "2026-01-01T00:00:00Z",
    }


def _dep(target):
    return {"target_id": target, "relation": "blocks"}


# Fixtures spanning the score tiers (exit == 5 - score).
_HEALTHY = [_ticket("he", "open", "epic"), _ticket("ht", "open", "task", "he")]  # 5
_ONE_CRIT = [_ticket("ce", "open", "epic"), _ticket("cc", "open", "task", "ce", deps=[_dep("ce")])]  # 3
_FOUR_CRIT = [_ticket("e", "open", "epic")] + [
    _ticket(f"c{i}", "open", "task", "e", deps=[_dep("e")]) for i in range(4)
]  # 1
_DUP = [
    _ticket("de", "open", "epic"),
    _ticket("d1", "open", "task", "de", title="Same Title"),
    _ticket("d2", "open", "task", "de", title="Same Title"),
]  # 4 (1 MINOR)
_ORPHAN_PLUS_CRIT = [
    _ticket("oe", "open", "epic"),
    _ticket("oc", "open", "task", "oe", deps=[_dep("oe")]),  # CRIT
    _ticket("orphan", "open", "task", None, title="Orphan Task"),  # WARNING
]  # 2


# ───────────────────────────── score → exit tiers ────────────────────────────
@pytest.mark.parametrize(
    "name,tickets,expect_score",
    [("healthy", _HEALTHY, 5), ("dup_minor", _DUP, 4), ("one_crit", _ONE_CRIT, 3),
     ("orphan_crit", _ORPHAN_PLUS_CRIT, 2), ("four_crit", _FOUR_CRIT, 1)],
)
def test_score_tier_and_exit(tmp_path: Path, name, tickets, expect_score):
    """exit == 5 - score (the docs/exit-codes.md contract), and the JSON report
    carries the matching score + the canonical key set."""
    cmd = _mock_ticket_cmd(tmp_path, tickets)
    r = _run(cmd, "--output", "json")
    d = json.loads(r.stdout)
    assert d["score"] == expect_score
    assert r.returncode == 5 - expect_score
    assert set(d) == {"score", "critical_issues", "major_issues", "minor_issues", "warnings", "suggestions"}


def test_critical_message_golden(tmp_path: Path):
    cmd = _mock_ticket_cmd(tmp_path, _ONE_CRIT)
    d = json.loads(_run(cmd, "--output", "json").stdout)
    assert d["critical_issues"] == ["Child->parent dependency: cc depends on its parent ce - Ticket cc"]


# ───────────────────────────── human output goldens ──────────────────────────
def test_healthy_terse_is_single_line(tmp_path: Path):
    cmd = _mock_ticket_cmd(tmp_path, _HEALTHY)
    r = _run(cmd, "--terse")
    assert r.returncode == 0
    assert r.stdout == ""  # text/terse go to stderr; stdout is empty
    assert r.stderr == "Issues health: 5/5 (0 critical, 0 major, 0 minor, 0 warnings)\n"


def test_text_findings_colored_on_stderr(tmp_path: Path):
    cmd = _mock_ticket_cmd(tmp_path, _ONE_CRIT)
    r = _run(cmd)
    # The CRITICAL finding prints in red to stderr; stdout stays empty in text mode.
    assert r.stdout == ""
    assert "\033[0;31m[CRITICAL]\033[0m Child->parent dependency: cc" in r.stderr
    assert "Health Score: \033[1;33m3/5\033[0m - Fair (needs attention)" in r.stderr


def test_usage_errors(tmp_path: Path):
    cmd = _mock_ticket_cmd(tmp_path, _HEALTHY)
    assert _run(cmd, "--bogus").returncode == 1  # unknown option → exit 1
    assert _run(cmd, "--help").returncode == 0


# ───────────────────────────── library / MCP ─────────────────────────────────
def test_library_and_mcp_shape(monkeypatch):
    import rebar
    from rebar.mcp_server import ValidateReportOut

    monkeypatch.setenv("REBAR_NO_SYNC", "1")
    monkeypatch.delenv("TICKET_CMD", raising=False)
    d = rebar.validate()  # real store, in-process
    assert set(d) == {"score", "critical_issues", "major_issues", "minor_issues", "warnings", "suggestions"}
    assert 1 <= d["score"] <= 5
    ValidateReportOut.model_validate(d)
