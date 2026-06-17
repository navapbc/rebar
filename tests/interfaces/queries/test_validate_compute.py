"""validate characterization — docs/bash-migration.md §5.

The python characterization of ``validate``: the score→exit tiers, the JSON
report shape, the human text/terse goldens, and the library/schema.

Driven IN-PROCESS by injecting the raw ticket list (``validate._raw_tickets``) and
calling ``validate.run`` directly. EV-2b removed the ``TICKET_CMD`` subprocess
injection seam; the synthetic-graph fixtures are kept (they exercise the health
checks' scoring of arbitrary graphs — including the child→parent-dep critical,
which a real-store materialization could NOT reproduce because the write API's
hierarchy promotion neutralizes exactly that pathology).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar._engine_support import validate as _validate


def _run(capsys, monkeypatch, tmp_path: Path, tickets: list[dict], *args: str):
    """Inject ``tickets`` as the raw ticket list and run validate in-process.
    Returns (returncode, stdout, stderr). The tracker is an empty tmp dir —
    signature_findings no-ops without a signing key, so only the injected graph
    drives the score."""
    monkeypatch.setattr(_validate, "_raw_tickets", lambda tracker: list(tickets))
    capsys.readouterr()  # clear
    rc = _validate.run(list(args), str(tmp_path))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


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
_ONE_CRIT = [
    _ticket("ce", "open", "epic"),
    _ticket("cc", "open", "task", "ce", deps=[_dep("ce")]),
]  # 3
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
    [
        ("healthy", _HEALTHY, 5),
        ("dup_minor", _DUP, 4),
        ("one_crit", _ONE_CRIT, 3),
        ("orphan_crit", _ORPHAN_PLUS_CRIT, 2),
        ("four_crit", _FOUR_CRIT, 1),
    ],
)
def test_score_tier_and_exit(capsys, monkeypatch, tmp_path: Path, name, tickets, expect_score):
    """exit == 5 - score (the docs/exit-codes.md contract), and the JSON report
    carries the matching score + the canonical key set."""
    import json

    rc, out, _ = _run(capsys, monkeypatch, tmp_path, tickets, "--output", "json")
    d = json.loads(out)
    assert d["score"] == expect_score
    assert rc == 5 - expect_score
    assert set(d) == {
        "score",
        "critical_issues",
        "major_issues",
        "minor_issues",
        "warnings",
        "suggestions",
    }


def test_critical_message_golden(capsys, monkeypatch, tmp_path: Path):
    import json

    _, out, _ = _run(capsys, monkeypatch, tmp_path, _ONE_CRIT, "--output", "json")
    d = json.loads(out)
    assert d["critical_issues"] == [
        "Child->parent dependency: cc depends on its parent ce - Ticket cc"
    ]


# ───────────────────────────── human output goldens ──────────────────────────
def test_healthy_terse_is_single_line(capsys, monkeypatch, tmp_path: Path):
    rc, out, err = _run(capsys, monkeypatch, tmp_path, _HEALTHY, "--terse")
    assert rc == 0
    assert out == ""  # text/terse go to stderr; stdout is empty
    assert err == "Issues health: 5/5 (0 critical, 0 major, 0 minor, 0 warnings)\n"


def test_text_findings_colored_on_stderr(capsys, monkeypatch, tmp_path: Path):
    _, out, err = _run(capsys, monkeypatch, tmp_path, _ONE_CRIT)
    # The CRITICAL finding prints in red to stderr; stdout stays empty in text mode.
    assert out == ""
    assert "\033[0;31m[CRITICAL]\033[0m Child->parent dependency: cc" in err
    assert "Health Score: \033[1;33m3/5\033[0m - Fair (needs attention)" in err


def test_usage_errors(capsys, monkeypatch, tmp_path: Path):
    assert _run(capsys, monkeypatch, tmp_path, _HEALTHY, "--bogus")[0] == 1  # unknown → 1
    assert _run(capsys, monkeypatch, tmp_path, _HEALTHY, "--help")[0] == 0


# ───────────────────────────── library / MCP ─────────────────────────────────
def test_library_and_mcp_shape(monkeypatch):
    import rebar
    from rebar.mcp_server import ValidateReportOut

    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    d = rebar.validate()  # real store, in-process
    assert set(d) == {
        "score",
        "critical_issues",
        "major_issues",
        "minor_issues",
        "warnings",
        "suggestions",
    }
    assert 1 <= d["score"] <= 5
    ValidateReportOut.model_validate(d)
