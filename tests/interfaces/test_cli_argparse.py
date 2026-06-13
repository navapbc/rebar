"""Tier E E0: the argparse CLI surface, pinned byte-identical to the dispatcher.

These tests prove the in-process argparse CLI (:mod:`rebar._cli`) reproduces the
bash dispatcher's help/overview/error output byte-for-byte BEFORE the E1 cutover,
plus that its routing tables cover every known subcommand and the in-process arms
are wired correctly. The goldens in ``tests/golden/cli_help`` were captured from
the live bash dispatcher (every ``rebar <cmd> --help``, the overview, and the two
unknown-subcommand error paths).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._cli import _help
from rebar._cli import (
    _READS_INIT_ONLY,
    _READS_NO_INIT,
    _WRITES_FULL,
    main,
)

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "cli_help"

# (golden-name, argv) for every captured invocation form except ``reconcile``
# (passthrough to ``python -m rebar_reconciler`` — covered by the reconciler's own
# tests and unchanged by this CLI).
_OVERVIEW_FORMS = [
    ("__overview_noargs", []),
    ("__overview_help", ["help"]),
    ("__overview_dashhelp", ["--help"]),
    ("__overview_dashh", ["-h"]),
    ("__unknown__", ["frobnicate"]),
    ("__help_unknown__", ["help", "frobnicate"]),
]


def _all_subcommands() -> list[str]:
    return sorted(_help.known_subcommands())


def _golden(name: str) -> tuple[str, str, int]:
    out = (_GOLDEN / f"{name}.out").read_text(encoding="utf-8")
    err = (_GOLDEN / f"{name}.err").read_text(encoding="utf-8")
    code = int((_GOLDEN / f"{name}.exit").read_text(encoding="utf-8"))
    return out, err, code


def _golden_cases() -> list[tuple[str, list[str]]]:
    cases = list(_OVERVIEW_FORMS)
    for sub in _all_subcommands():
        cases.append((f"{sub}__dashhelp", [sub, "--help"]))
        cases.append((f"{sub}__help", ["help", sub]))
    return cases


@pytest.mark.parametrize("name,argv", _golden_cases(), ids=lambda v: v if isinstance(v, str) else None)
def test_help_byte_parity(name: str, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """Every help/overview/error invocation matches the dispatcher byte-for-byte."""
    want_out, want_err, want_code = _golden(name)
    code = main(argv)
    captured = capsys.readouterr()
    assert captured.out == want_out, f"{name}: stdout mismatch"
    assert captured.err == want_err, f"{name}: stderr mismatch"
    assert code == want_code, f"{name}: exit mismatch"


def test_sub_help_equals_help_sub() -> None:
    """``rebar <sub> --help`` and ``rebar help <sub>`` are the identical contract."""
    for sub in _all_subcommands():
        a, _ = _golden(f"{sub}__dashhelp")[0], None
        b = _golden(f"{sub}__help")[0]
        assert a == b, f"{sub}: the two help forms diverge"


def test_routing_tables_cover_every_known_subcommand() -> None:
    """No known subcommand falls through dispatch: each is in-process or passthrough.

    The in-process sets are disjoint; everything else is the transitional
    passthrough set. A command missing from BOTH would silently route to the bash
    dispatcher, which is fine transitionally — but the explicit in-process sets must
    never overlap (a command can't be both a read and a write arm).
    """
    inproc = _READS_INIT_ONLY | _READS_NO_INIT | _WRITES_FULL
    assert _READS_INIT_ONLY.isdisjoint(_WRITES_FULL)
    assert _READS_INIT_ONLY.isdisjoint(_READS_NO_INIT)
    assert _READS_NO_INIT.isdisjoint(_WRITES_FULL)
    # Every in-process arm is a real, known subcommand (no typo in the tables).
    known = _help.known_subcommands()
    assert inproc <= known, f"in-process arms not in known subcommands: {inproc - known}"


def test_inprocess_reads_are_wired(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """In-process read arms (list/show) dispatch through the CLI and return data."""
    tid = rebar.create_ticket("task", "E0 wiring smoke", repo_root=str(rebar_repo))

    code = main(["list"])
    out = capsys.readouterr().out
    assert code == 0
    listed = json.loads(out)
    assert any(t["ticket_id"] == tid for t in listed), "created ticket absent from list"

    code = main(["show", tid])
    out = capsys.readouterr().out
    assert code == 0
    shown = json.loads(out)
    assert shown["ticket_id"] == tid
    assert shown["title"] == "E0 wiring smoke"


def test_inprocess_writes_are_wired(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """In-process leaf-write arms (comment/tag) dispatch through the CLI and persist."""
    tid = rebar.create_ticket("task", "E0 write smoke", repo_root=str(rebar_repo))

    assert main(["comment", tid, "hello from E0"]) == 0
    capsys.readouterr()
    assert main(["tag", tid, "e0-smoke"]) == 0
    capsys.readouterr()

    shown = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert any(c["body"] == "hello from E0" for c in shown["comments"])
    assert "e0-smoke" in shown["tags"]


def test_unknown_subcommand_streams(capsys: pytest.CaptureFixture[str]) -> None:
    """Top-level unknown: error to stderr, overview to stdout, exit 1.

    Distinct from ``help <unknown>`` (all to stderr) — the dispatcher separates the
    two streams and so must the CLI.
    """
    code = main(["frobnicate"])
    cap = capsys.readouterr()
    assert code == 1
    assert cap.err == "Error: unknown subcommand 'frobnicate'\n"
    assert cap.out == _help.overview()
