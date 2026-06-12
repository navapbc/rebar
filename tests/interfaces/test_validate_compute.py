"""Tier C (REBAR_COMPUTE) parity gate for ``validate`` — docs/bash-migration.md §5.

``validate`` shares the dispatcher's ``_compute_python`` switch helper (pinned to
``rebar._switch`` in ``test_next_batch_compute.py``), so this file pins the port's
*behavioral* parity with ``validate-issues.sh`` through the dispatcher, driven by
the same ``TICKET_CMD``-injection mechanism the bash suite uses:

- **Text / terse / verbose** stderr + exit code are byte-identical across both
  switch values (the §1.4 human-output contract, ANSI colors included).
- **``--output json``** is pinned by JSON **semantic + schema** equality, not raw
  bytes (jq vs ``json.dumps`` whitespace is explicitly outside the contract).
- The **score→exit** tiers (0..4) match across impls for crafted fixtures.
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
    """A fake ``ticket`` whose ``list`` prints the given JSON — the bash suite's
    injection mechanism, honored by both impls (subprocess on TICKET_CMD)."""
    payload = json.dumps(tickets)
    (tmp_path / "tickets.json").write_text(payload)
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


def _run(ticket_cmd: str, impl: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TICKET_CMD": ticket_cmd, "REBAR_COMPUTE": impl, "REBAR_NO_SYNC": "1"}
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
_HEALTHY = [_ticket("he", "open", "epic"), _ticket("ht", "open", "task", "he")]  # score 5
_ONE_CRIT = [_ticket("ce", "open", "epic"), _ticket("cc", "open", "task", "ce", deps=[_dep("ce")])]  # 1 CRIT → score 3
_FOUR_CRIT = [_ticket("e", "open", "epic")] + [
    _ticket(f"c{i}", "open", "task", "e", deps=[_dep("e")]) for i in range(4)
]  # 4 CRIT → score 1
_DUP = [
    _ticket("de", "open", "epic"),
    _ticket("d1", "open", "task", "de", title="Same Title"),
    _ticket("d2", "open", "task", "de", title="Same Title"),
]  # 1 MINOR → score 4
_ORPHAN_PLUS_CRIT = [
    _ticket("oe", "open", "epic"),
    _ticket("oc", "open", "task", "oe", deps=[_dep("oe")]),  # CRIT
    _ticket("orphan", "open", "task", None, title="Orphan Task"),  # WARNING
]  # score 2
_INPROGRESS = [_ticket("ie", "open", "epic"), _ticket("it", "in_progress", "task", "ie", notes="")]

_SCENARIOS = [
    ("healthy", _HEALTHY, []),
    ("healthy_terse", _HEALTHY, ["--terse"]),
    ("healthy_verbose", _HEALTHY, ["--verbose"]),
    ("one_crit", _ONE_CRIT, []),
    ("one_crit_terse", _ONE_CRIT, ["--terse"]),
    ("four_crit", _FOUR_CRIT, []),
    ("four_crit_verbose", _FOUR_CRIT, ["--verbose"]),
    ("dup_minor", _DUP, ["--terse"]),
    ("orphan_crit", _ORPHAN_PLUS_CRIT, []),
    ("orphan_crit_terse", _ORPHAN_PLUS_CRIT, ["--terse"]),
    ("inprogress", _INPROGRESS, ["--terse"]),
    ("quick", _FOUR_CRIT, ["--quick"]),
    ("help", _HEALTHY, ["--help"]),
    ("unknown_opt", _HEALTHY, ["--bogus"]),
]


@pytest.mark.parametrize("name,tickets,args", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_validate_text_byte_parity(tmp_path: Path, name, tickets, args):
    """stdout + stderr + exit identical across bash|python for text/terse/verbose."""
    cmd = _mock_ticket_cmd(tmp_path, tickets)
    b = _run(cmd, "bash", *args)
    p = _run(cmd, "python", *args)
    assert b.returncode == p.returncode, f"{name}: exit {b.returncode} vs {p.returncode}"
    assert b.stdout == p.stdout, f"{name}: stdout drift\nBASH:\n{b.stdout!r}\nPY:\n{p.stdout!r}"
    assert b.stderr == p.stderr, f"{name}: stderr drift\nBASH:\n{b.stderr!r}\nPY:\n{p.stderr!r}"


_JSON_SCENARIOS = [
    ("healthy", _HEALTHY, 0),
    ("one_crit", _ONE_CRIT, 2),
    ("four_crit", _FOUR_CRIT, 4),
    ("dup_minor", _DUP, 1),
    ("orphan_crit", _ORPHAN_PLUS_CRIT, 3),
]


@pytest.mark.parametrize("name,tickets,expect_exit", _JSON_SCENARIOS, ids=[s[0] for s in _JSON_SCENARIOS])
def test_validate_json_semantic_parity_and_exit_tier(tmp_path: Path, name, tickets, expect_exit):
    """--output json: parsed-equal across impls, schema key set intact, and the
    score→exit tier matches the documented exit-code contract (exit == 5 - score)."""
    cmd = _mock_ticket_cmd(tmp_path, tickets)
    b = _run(cmd, "bash", "--output", "json")
    p = _run(cmd, "python", "--output", "json")
    bj, pj = json.loads(b.stdout), json.loads(p.stdout)
    assert bj == pj, f"{name}: json drift\n{bj}\n{pj}"
    assert set(pj) == {"score", "critical_issues", "major_issues", "minor_issues", "warnings", "suggestions"}
    assert b.returncode == p.returncode == expect_exit
    assert pj["score"] == 5 - expect_exit


def test_validate_library_and_mcp_parity(tmp_path, monkeypatch):
    """Library rebar.validate() agrees across impls and conforms to the MCP schema."""
    import rebar
    from rebar.mcp_server import ValidateReportOut

    # Real-store in-process path (no TICKET_CMD): both impls read via list_states.
    monkeypatch.setenv("REBAR_NO_SYNC", "1")
    monkeypatch.delenv("TICKET_CMD", raising=False)
    monkeypatch.setenv("REBAR_COMPUTE", "bash")
    b = rebar.validate()
    monkeypatch.setenv("REBAR_COMPUTE", "python")
    p = rebar.validate()
    assert b == p
    ValidateReportOut.model_validate(p)
