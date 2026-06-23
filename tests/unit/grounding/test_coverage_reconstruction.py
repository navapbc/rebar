"""S6 / AC5 — COVERAGE reconstruction over a mixed-language run.

The evidence model's "no silent no-op" guarantee: a skipped backend is a VISIBLE
coverage record (``status=skipped`` + ``reason``), not an absence. So from the records
of one scan/query over a mixed-language repo you can fully RECONSTRUCT what ran vs what
was skipped and WHY — every backend that participated is accounted for.

We scan a Python+JS+Go repo and assert:

* every record carries a ``coverage`` block (``backend`` + ``status`` (+ ``reason`` when
  skipped)) and validates against the S1 contract;
* the per-backend account is reconstructable — each backend appears as ran OR skipped
  (with a closed reason), and the skipped set explains itself (e.g. scc/lizard absent →
  ``no_tool``);
* there are NO silent no-ops: the count of (ran ∪ skipped) records equals the count of
  emitted records (no record lacks coverage).
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from rebar.grounding import engine_b, oracle
from rebar.grounding import evidence as ev

pytestmark = pytest.mark.unit

_HAVE_SEMGREP = bool(shutil.which("opengrep") or shutil.which("semgrep"))
_HAVE_ASTGREP = bool(shutil.which("ast-grep") or shutil.which("sg"))


@pytest.fixture
def mixed_repo(tmp_path: Path) -> Path:
    """A mixed-language repo: JS (matches built-ins), Go, and Python."""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.js").write_text("function f(){ console.log('x'); debugger; }\n")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "main.go").write_text("package main\nfunc Serve() int { return 0 }\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("x = 1\n" * 50)
    return tmp_path


def _coverage_table(records) -> dict[str, dict]:
    """Reconstruct a per-backend coverage table from the emitted records.

    Returns ``{backend: {"ran": n, "skipped": n, "reasons": {reason: n}}}`` — the
    legible account of what each backend did, derived purely from the records.
    """
    table: dict[str, dict] = {}
    for rec in records:
        cov = rec["coverage"]
        b = table.setdefault(cov["backend"], {"ran": 0, "skipped": 0, "reasons": Counter()})
        b[cov["status"]] += 1
        if cov["status"] == ev.STATUS_SKIPPED:
            b["reasons"][cov.get("reason")] += 1
    return table


def test_every_record_carries_coverage_no_silent_noop(mixed_repo) -> None:
    result = engine_b.scan(mixed_repo)
    assert result.records, "a mixed-language scan must produce records (no silent no-op)"
    ran = skipped = 0
    for rec in result.records:
        ev.validate(rec)
        cov = rec["coverage"]
        assert cov["backend"], "every record must name its backend"
        assert cov["status"] in (ev.STATUS_RAN, ev.STATUS_SKIPPED)
        if cov["status"] == ev.STATUS_SKIPPED:
            # a skip MUST explain itself with a closed reason (the skip IS the coverage)
            assert cov.get("reason") in ev.ABSTAIN_REASONS
            skipped += 1
        else:
            assert "reason" not in cov  # a ran record carries no skip reason
            ran += 1
    # The set is complete: ran ∪ skipped accounts for every record (no record uncovered).
    assert ran + skipped == len(result.records)


def test_metric_backend_skip_is_reconstructable(mixed_repo) -> None:
    # scc/lizard are absent on this host -> the metric backend appears in the table as
    # skipped with no_tool. The skip is recoverable from the records alone.
    result = engine_b.scan(mixed_repo)
    table = _coverage_table(result.records)
    assert engine_b.BACKEND_METRIC in table, "the metric backend must be accounted for"
    metric = table[engine_b.BACKEND_METRIC]
    assert metric["skipped"] >= 1
    assert metric["reasons"].get("no_tool", 0) >= 1


def test_full_backend_account_is_present(mixed_repo, capsys) -> None:
    # Reconstruct the WHOLE per-backend account and assert every Engine B backend that
    # had an applicable detector is represented (ran or skipped) — never missing.
    result = engine_b.scan(mixed_repo)
    table = _coverage_table(result.records)
    with capsys.disabled():
        print("\n[AC5] reconstructed coverage account:")
        for backend, stats in sorted(table.items()):
            reasons = dict(stats["reasons"])
            print(
                f"  {backend:10s} ran={stats['ran']} skipped={stats['skipped']} reasons={reasons}"
            )
    # The metric backend is always present (its detectors are language-agnostic).
    assert engine_b.BACKEND_METRIC in table
    # At least one structural backend participated (ran or skipped) on the JS surface.
    structural = {engine_b.BACKEND_OPENGREP, engine_b.BACKEND_ASTGREP}
    assert structural & set(table), "a structural backend must be accounted for on a JS repo"


def test_oracle_scan_records_reconstruct_coverage(mixed_repo) -> None:
    # The facade surface carries the same coverage account through to the consumer.
    records = oracle.scan(str(mixed_repo))
    assert records
    for rec in records:
        ev.validate(rec)
        assert rec["coverage"]["status"] in (ev.STATUS_RAN, ev.STATUS_SKIPPED)
    table = _coverage_table(records)
    assert table, "the oracle scan must expose a reconstructable coverage account"


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_ran_and_skipped_coexist_in_one_run(mixed_repo) -> None:
    # The defining property: a SINGLE run yields both ran records (engines present) and
    # skipped records (scc/lizard absent) — the account distinguishes them, no silent gap.
    result = engine_b.scan(mixed_repo)
    statuses = {r["coverage"]["status"] for r in result.records}
    assert ev.STATUS_RAN in statuses, "an engine that ran must be recorded as ran"
    assert ev.STATUS_SKIPPED in statuses, "the absent metric tool must be recorded as skipped"
