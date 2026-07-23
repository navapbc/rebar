"""Tier E: the argparse CLI surface — routing coverage, wiring, and stream/exit contracts.

These prove the in-process argparse CLI (:mod:`rebar._cli`) routes every known subcommand,
that its in-process read/write arms are wired, and that the help forms and unknown-subcommand
paths honor their stream/exit contracts. Assertions are on OBSERVABLE BEHAVIOR (return data,
exit codes, which stream output lands on, help-form equivalence) — not byte-for-byte snapshots
of help text. (The former ``tests/golden/cli_help`` byte-parity goldens pinned the CLI to the
retired bash dispatcher during the E1 cutover; post-cutover they only re-asserted "the help
bytes are what they were last captured", so they were a change-detector — any help edit broke
them and was "fixed" by re-capturing — and were removed.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._cli import (
    _DESCENDANTS,
    _FIELD_READS,
    _GATES,
    _LOOKUPS,
    _READS_INIT_ONLY,
    _READS_NO_INIT,
    _WRITES_FULL,
    _help,
    main,
)


def _all_subcommands() -> list[str]:
    return sorted(_help.known_subcommands())


def test_sub_help_equals_help_sub(capsys: pytest.CaptureFixture[str]) -> None:
    """``rebar <sub> --help`` and ``rebar help <sub>`` are the identical contract (checked LIVE,
    so this asserts current behavior rather than a captured snapshot)."""
    for sub in _all_subcommands():
        main([sub, "--help"])
        a = capsys.readouterr().out
        main(["help", sub])
        b = capsys.readouterr().out
        assert a == b, f"{sub}: the two help forms diverge"
        assert a, f"{sub}: --help produced no output"


def test_routing_tables_cover_every_known_subcommand() -> None:
    """No known subcommand falls through dispatch: each is in-process or passthrough.

    The in-process sets are disjoint; everything else is the transitional
    passthrough set. A command missing from BOTH would silently route to the bash
    dispatcher, which is fine transitionally — but the explicit in-process sets must
    never overlap (a command can't be both a read and a write arm).
    """
    inproc = (
        _READS_INIT_ONLY
        | _READS_NO_INIT
        | _WRITES_FULL
        | _FIELD_READS
        | _LOOKUPS
        | _DESCENDANTS
        | _GATES
    )
    sets = [
        _READS_INIT_ONLY,
        _READS_NO_INIT,
        _WRITES_FULL,
        _FIELD_READS,
        _LOOKUPS,
        _DESCENDANTS,
        _GATES,
    ]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            assert sets[i].isdisjoint(sets[j]), "in-process routing sets overlap"
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


def test_help_unknown_streams_all_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """``rebar help <unknown>``: everything to stderr (error line AND the overview), nothing to
    stdout, exit 1 — the complement of a top-level unknown, which splits the two streams."""
    code = main(["help", "frobnicate"])
    cap = capsys.readouterr()
    assert code == 1
    assert cap.out == ""
    assert "unknown subcommand 'frobnicate'" in cap.err
    assert _help.overview() in cap.err
