"""`rebar store compact --threshold` works as its help states (chorded-crystal-dungbeetle).

The help lists `--threshold N` beside `--all`, but the store-wide sweep rejected it
("unknown option '--threshold'"), and the single-ticket form accepted only `--threshold=N`.
These tests pin the help text and both arms' parsing together. The sweep's store scan is
replaced so no tracker contents are needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config
from rebar._cli._parsers.core.compact import build_compact
from rebar._commands import compact

pytestmark = pytest.mark.unit


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """Run the sweep against an empty tracker dir and record the threshold it selects with."""
    seen: list[int] = []
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setattr(config, "tracker_dir", lambda _root=None: tracker)

    def scan(_tracker, threshold, _horizon, **_kw):
        seen.append(threshold)
        return [], 0

    monkeypatch.setattr(compact, "_scan_snapshot_state", scan)
    monkeypatch.setattr(compact, "_best_effort_push", lambda *_a, **_k: None)
    return seen


def test_help_offers_threshold_with_all() -> None:
    text = build_compact(prog="rebar store compact").format_help()
    assert "--all" in text and "--threshold" in text


@pytest.mark.parametrize("spelling", [["--threshold", "8"], ["--threshold=8"]])
def test_sweep_accepts_both_threshold_spellings(sweep: list[int], spelling: list[str]) -> None:
    rc = compact.compact_dispatch_cli(["--all", "--dry-run", *spelling])

    assert rc == 0
    assert sweep == [8]


def test_sweep_without_threshold_uses_the_configured_one(sweep: list[int]) -> None:
    rc = compact.compact_dispatch_cli(["--all", "--dry-run"])

    assert rc == 0
    assert sweep == [config.compose_config(None).compact.threshold]


def test_single_ticket_form_accepts_the_space_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(compact, "compact_cli", lambda argv, repo_root=None: seen.append(argv) or 0)

    rc = compact.compact_dispatch_cli(["abcd-1234", "--threshold", "8", "--horizon", "5"])

    assert rc == 0
    assert seen == [["abcd-1234", "--threshold=8", "--horizon=5"]]


def test_a_trailing_threshold_without_a_value_is_still_rejected(sweep: list[int]) -> None:
    rc = compact.compact_dispatch_cli(["--all", "--dry-run", "--threshold"])

    assert rc != 0
    assert sweep == []
