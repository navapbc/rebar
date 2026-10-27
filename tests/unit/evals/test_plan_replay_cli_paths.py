"""The plan-replay CLI's path wiring (epic e47b slice 1).

``__main__`` used to hard-code its cache/out/sample defaults as CWD-relative
hard-coded literals. They are now ``None`` at the flag layer and resolve at
dispatch against the configured eval corpus root, and ``--ledger`` lets a caller point
the spend ledger somewhere else entirely. These tests cover that wiring: the parser
defaults, the two resolution helpers, and the threading of ``ledger_path`` into each
tier run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from rebar.llm.evals.plan_replay import __main__ as cli

pytestmark = pytest.mark.unit


@pytest.fixture
def corpus_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the corpus root somewhere writable and assertable, so these tests never
    depend on (or write into) the repo's own committed corpus.

    Requested explicitly rather than ``autouse``: the parser-only tests do not touch path
    resolution, and an autouse fixture would hide which tests actually depend on it."""
    monkeypatch.setenv("REBAR_LLM_EVALS_CORPUS_ROOT", str(tmp_path / "corpus"))
    return tmp_path / "corpus" / "plan-review-gate" / "replay"


#: Each subcommand with the flags argparse marks ``required=True`` for it.
_MINIMAL = {
    "tier0": ["tier0", "--candidate", "c"],
    "tier1": ["tier1", "--n", "1"],
    "tier2": ["tier2"],
}


@pytest.mark.parametrize("sub", ["tier0", "tier1", "tier2"])
def test_the_path_flags_default_to_none_not_a_hard_coded_literal(sub: str) -> None:
    """A literal default would re-pin the CWD-relative path the story removed."""
    args = cli.build_parser().parse_args(_MINIMAL[sub])
    assert args.cache_dir is None
    assert args.out_dir is None


@pytest.mark.parametrize("sub", ["tier1", "tier2"])
def test_the_ledger_flag_exists_and_defaults_to_none(sub: str) -> None:
    args = cli.build_parser().parse_args(_MINIMAL[sub])
    assert args.ledger is None


def test_unset_path_flags_resolve_under_the_configured_corpus_root(corpus_root: Path) -> None:
    args = cli.build_parser().parse_args(["tier2"])
    assert cli._cache_dir(args) == str(corpus_root / "cache")
    assert cli._sample_path(args) == str(corpus_root / "tier2-sample.json")


def test_explicit_path_flags_win_over_the_corpus_root() -> None:
    args = cli.build_parser().parse_args(
        ["tier2", "--cache-dir", "/tmp/c", "--sample-path", "/tmp/s.json"]
    )
    assert cli._cache_dir(args) == "/tmp/c"
    assert cli._sample_path(args) == "/tmp/s.json"


def _stub_result() -> dict:
    return {
        "run_id": "r1",
        "sample_n": 1,
        "requested_n": 1,
        "ledger_entry": {"usd": 0.0},
    }


def test_tier1_threads_the_ledger_flag_into_the_run(
    monkeypatch: pytest.MonkeyPatch, corpus_root: Path
) -> None:
    seen: dict = {}

    def fake_run_tier1(store_roots, **kwargs):
        seen.update(kwargs)
        return _stub_result()

    monkeypatch.setattr(cli.tier1, "run_tier1", fake_run_tier1)
    monkeypatch.setattr(cli.tier1, "render_tier1_report", lambda result: "report")
    monkeypatch.setattr(cli, "_default_store_roots", lambda: {"s": "/tmp"})
    monkeypatch.setattr(cli, "load_verifier_candidate", lambda name: object())

    args = cli.build_parser().parse_args(["tier1", "--n", "1", "--ledger", "/tmp/led.jsonl"])
    assert cli._run_tier1(args) == 0
    assert seen["ledger_path"] == "/tmp/led.jsonl"
    # the cache dir still resolves under the corpus root, not a CWD-relative literal
    assert seen["cache_dir"] == str(corpus_root / "cache")


def test_tier1_passes_none_when_the_ledger_flag_is_absent(
    monkeypatch: pytest.MonkeyPatch, corpus_root: Path
) -> None:
    """``None`` is what lets ``ledger`` resolve the configured default itself; a literal
    here would bypass that single resolution rule."""
    seen: dict = {}

    monkeypatch.setattr(
        cli.tier1, "run_tier1", lambda store_roots, **kw: (seen.update(kw), _stub_result())[1]
    )
    monkeypatch.setattr(cli.tier1, "render_tier1_report", lambda result: "report")
    monkeypatch.setattr(cli, "_default_store_roots", lambda: {"s": "/tmp"})
    monkeypatch.setattr(cli, "load_verifier_candidate", lambda name: object())

    assert cli._run_tier1(cli.build_parser().parse_args(["tier1", "--n", "1"])) == 0
    assert seen["ledger_path"] is None


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["tier2"], "run_tier2_full"),
        (["tier2", "--criteria", "G1G2"], "run_tier2_single_criterion"),
    ],
)
def test_both_tier2_arms_thread_the_ledger_flag(
    argv: list[str], target: str, monkeypatch: pytest.MonkeyPatch, corpus_root: Path
) -> None:
    seen: dict = {}

    monkeypatch.setattr(
        cli.tier2, target, lambda store_roots, **kw: (seen.update(kw), _stub_result())[1]
    )
    monkeypatch.setattr(cli.tier2, "render_tier2_report", lambda result: "report")
    monkeypatch.setattr(cli, "_default_store_roots", lambda: {"s": "/tmp"})

    args = cli.build_parser().parse_args([*argv, "--ledger", "/tmp/led.jsonl"])
    assert cli._run_tier2(args) == 0
    assert seen["ledger_path"] == "/tmp/led.jsonl"


def test_the_out_dir_is_created_under_the_corpus_root(
    monkeypatch: pytest.MonkeyPatch, corpus_root: Path
) -> None:
    """The report lands under the configured root, which is the whole point of the story:
    a client project's run must not write into rebar's own corpus."""
    monkeypatch.setattr(cli.tier1, "run_tier1", lambda store_roots, **kw: _stub_result())
    monkeypatch.setattr(cli.tier1, "render_tier1_report", lambda result: "report")
    monkeypatch.setattr(cli, "_default_store_roots", lambda: {"s": "/tmp"})
    monkeypatch.setattr(cli, "load_verifier_candidate", lambda name: object())

    assert cli._run_tier1(cli.build_parser().parse_args(["tier1", "--n", "1"])) == 0
    assert (corpus_root / "r1.md").read_text(encoding="utf-8") == "report"


def test_an_absolute_out_dir_flag_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corpus_root: Path
) -> None:
    monkeypatch.setattr(cli.tier1, "run_tier1", lambda store_roots, **kw: _stub_result())
    monkeypatch.setattr(cli.tier1, "render_tier1_report", lambda result: "report")
    monkeypatch.setattr(cli, "_default_store_roots", lambda: {"s": "/tmp"})
    monkeypatch.setattr(cli, "load_verifier_candidate", lambda name: object())

    out = tmp_path / "elsewhere"
    args = cli.build_parser().parse_args(["tier1", "--n", "1", "--out-dir", str(out)])
    assert cli._run_tier1(args) == 0
    assert (out / "r1.md").exists()


def test_argparse_namespace_is_not_mutated_by_resolution(corpus_root: Path) -> None:
    """The helpers resolve a value; they must not write the resolved path back onto the
    namespace, or a later read would see a root-pinned literal instead of ``None``."""
    args = cli.build_parser().parse_args(["tier2"])
    cli._cache_dir(args)
    cli._sample_path(args)
    assert args.cache_dir is None
    assert args.sample_path is None


def test_namespace_shape_matches_what_the_dispatchers_read() -> None:
    """Guards the flag/dispatcher contract: a renamed flag would leave the dispatcher
    reading a missing attribute, which argparse would not catch."""
    args = cli.build_parser().parse_args(["tier1", "--n", "1"])
    for attr in ("cache_dir", "out_dir", "ledger"):
        assert isinstance(args, argparse.Namespace)
        assert hasattr(args, attr)
