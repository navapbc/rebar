"""HELD-OUT edge/E2E oracle for the fixture-mining heal loop (ticket 1cef).

Withheld from the implementation subagent and validated by the orchestrator. Covers every
disposition the happy path does not: the three-strike quarantine breaker, mixed-kind counter
accrual, first-encounter un-minable quarantine, idempotent filing, the budget stop, the
cleared-environment CLI dry-run, and the due-stamp interval boundary.

Shared scaffolding is imported from the visible happy-path module (``test_fixture_heal``), the
same cross-module pattern the admission oracle uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_fixture_heal import (
    DAY_SECONDS,
    AttemptOutcome,
    AttemptResult,
    FakeAttempter,
    admitted,
    ledger_total,
    paths,
    store,  # noqa: F401 — re-exported fixture
    stub_prices,
    unreliable_titles,
)

from rebar.llm.evals.fixture_mining import UNRELIABLE_TITLE_PREFIX, heal_fixtures
from rebar.llm.evals.plan_replay import ledger

pytestmark = pytest.mark.unit


def failed() -> AttemptResult:
    return AttemptResult(outcome=AttemptOutcome.FAILED_REPRODUCE)


def skipped_unbalanced() -> AttemptResult:
    return AttemptResult(outcome=AttemptOutcome.SKIPPED_UNBALANCED)


def unminable(reason: str) -> AttemptResult:
    return AttemptResult(outcome=AttemptOutcome.UNMINABLE, reason=reason)


def title_count(repo: Path, criterion_id: str) -> int:
    want = UNRELIABLE_TITLE_PREFIX + criterion_id
    return sum(1 for t in unreliable_titles(repo) if t == want)


def run_at(repo, result, *, day, crit, interval_days=30, **kw):
    """One heal run over a single-criterion gap at wall-day ``day`` (fresh attempter)."""
    attempter = FakeAttempter({crit: result})
    report = heal_fixtures(
        repo,
        attempter=attempter,
        gap_source=lambda: [crit],
        now=day * DAY_SECONDS,
        interval_days=interval_days,
        **paths(repo),
        **kw,
    )
    return report, attempter


# ── AC2: the third consecutive failed run files exactly one ticket; 1st/2nd do not ───────
def test_third_consecutive_failure_files_one_ticket(store) -> None:  # noqa: F811
    crit = "project.alpha"
    run_at(store, failed(), day=0, crit=crit)
    assert unreliable_titles(store) == []
    run_at(store, failed(), day=30, crit=crit)
    assert unreliable_titles(store) == []
    run_at(store, failed(), day=60, crit=crit)
    assert unreliable_titles(store) == [UNRELIABLE_TITLE_PREFIX + crit]
    assert title_count(store, crit) == 1


# ── AC3: a skipped-unbalanced run accrues the SAME counter as a reproduction failure ─────
def test_mixed_kind_failures_share_the_counter(store) -> None:  # noqa: F811
    crit = "project.beta"
    run_at(store, failed(), day=0, crit=crit)
    run_at(store, skipped_unbalanced(), day=30, crit=crit)
    assert unreliable_titles(store) == []
    run_at(store, failed(), day=60, crit=crit)
    assert unreliable_titles(store) == [UNRELIABLE_TITLE_PREFIX + crit]


# ── AC5: an un-minable criterion is quarantined on FIRST encounter (no three strikes) ────
@pytest.mark.parametrize(
    ("crit", "reason"),
    [
        ("project.container", "container-material-unrecoverable"),
        ("project.isf", "not-inline-admissible"),
    ],
)
def test_unminable_criterion_quarantined_on_first_encounter(store, crit, reason) -> None:  # noqa: F811
    report, _ = run_at(store, unminable(reason), day=0, crit=crit)
    assert unreliable_titles(store) == [UNRELIABLE_TITLE_PREFIX + crit]
    assert crit in report.quarantined
    assert title_count(store, crit) == 1


# ── AC6: filing is idempotent — a re-run while the ticket is open creates no duplicate ───
def test_ticket_filing_is_idempotent(store) -> None:  # noqa: F811
    crit = "project.gamma"
    run_at(store, unminable("not-inline-admissible"), day=0, crit=crit)
    assert title_count(store, crit) == 1
    # Second run: the open ticket makes the criterion skip; no duplicate is filed.
    report, attempter = run_at(store, unminable("not-inline-admissible"), day=30, crit=crit)
    assert title_count(store, crit) == 1
    assert crit in report.skipped_unreliable
    assert attempter.ran == []


# ── AC7: the run stops before a criterion whose estimate exceeds headroom; total ≤ cap ───
def test_budget_stop_admits_completed_and_stays_under_cap(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(ledger, "LEDGER_RESERVE_USD", 0.0)
    stub_prices(monkeypatch, usd_per_row=10.0)  # each attempt's ACTUAL spend is $10
    crits = ["project.c1", "project.c2", "project.c3"]
    attempter = FakeAttempter(
        {c: admitted() for c in crits},
        tier="tier1",  # PER_SAMPLE_ESTIMATE_USD["tier1"] == 0.5
        sample_n=20,  # estimate == 0.5 * 20 == $10 per attempt
    )
    report = heal_fixtures(
        store,
        attempter=attempter,
        gap_source=lambda: crits,
        now=0.0,
        interval_days=30,
        cap_usd=25.0,
        **paths(store),
    )

    # c1 ($10) and c2 ($20) fit under $25; c3's $10 estimate exceeds the $5 remaining → stop.
    assert report.admitted == ("project.c1", "project.c2")
    assert report.stopped_for_budget is True
    assert ledger_total(paths(store)["ledger_path"]) <= 25.0


# ── AC8: the CLI dry-run reports its attempt list with NO CI environment set ──────────────
def test_cli_dry_run_reports_attempt_list_with_cleared_env(store) -> None:  # noqa: F811
    from rebar.llm.evals.fixture_selection import _default_criteria

    expected = _default_criteria(str(store), "plan_review")
    assert expected, "fixture: the packaged gate must route at least one gap criterion"

    rebar_bin = str(Path(sys.executable).parent / "rebar")
    cleared_env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin",
        "HOME": os.environ.get("HOME", str(store)),
        "REBAR_ROOT": str(store),
        "REBAR_SYNC_PULL": "off",
        "REBAR_SYNC_PUSH": "off",
        "REBAR_SIGNING_KEY": "test-signing-key-1cef",
    }
    result = subprocess.run(
        [rebar_bin, "criteria", "heal", "--dry-run"],
        env=cleared_env,
        capture_output=True,
        text=True,
        cwd=str(store),
        check=False,
    )

    assert result.returncode == 0, f"stderr:\n{result.stderr}"
    printed = set(result.stdout.split())
    assert set(expected) & printed, f"no gap criterion in dry-run output:\n{result.stdout}"


# ── AC9: the due-stamp advances only after the configured interval ───────────────────────
def test_due_stamp_advances_only_after_interval(store) -> None:  # noqa: F811
    crit = "project.delta"
    state_path = paths(store)["state_path"]
    # Seed a last_run at day 0.
    Path(state_path).write_text(json.dumps({"last_run": 0.0, "counters": {}}), encoding="utf-8")

    early = FakeAttempter({crit: admitted()})
    early_report = heal_fixtures(
        store,
        attempter=early,
        gap_source=lambda: [crit],
        now=29 * DAY_SECONDS,  # interval minus one day
        interval_days=30,
        **paths(store),
    )
    assert early_report.ran is False
    assert early.planned == []

    due = FakeAttempter({crit: admitted()})
    due_report = heal_fixtures(
        store,
        attempter=due,
        gap_source=lambda: [crit],
        now=30 * DAY_SECONDS,  # exactly the interval
        interval_days=30,
        **paths(store),
    )
    assert due_report.ran is True
    assert crit in due.ran


# ── Gate-finding oracle (2615 PS2 LLM-Review BLOCK) — production-attempter correctness ────
# These cover behaviour the happy-path FakeAttempter cannot exercise: the live selection→
# emit→admission attempter's outcome classification and the loop's budget accounting at a
# non-default cap. Authored RED-first against the review findings, then fixed.


# Finding heal.py:255 — a non-default cap must not immediately trip BudgetExceeded. The old
# code reserved against `cap + LEDGER_RESERVE_USD` only when cap == 25.0, so any other cap
# left the outer pre-check at `cap - 30 - spent` (negative) and stopped before any work.
def test_non_default_cap_reserves_against_the_whole_cap(store, monkeypatch) -> None:  # noqa: F811
    # LEDGER_RESERVE_USD stays at its production default (30.0) — NOT zeroed.
    stub_prices(monkeypatch, usd_per_row=1.0)  # each admit finalizes $1 of real spend
    crits = ["project.c1", "project.c2", "project.c3"]
    attempter = FakeAttempter(
        {c: admitted() for c in crits},
        tier="criteria-eval-cheap",  # PER_SAMPLE_ESTIMATE_USD == 0.03
        sample_n=1,  # estimate == $0.03 per attempt
    )
    report = heal_fixtures(
        store,
        attempter=attempter,
        gap_source=lambda: crits,
        now=0.0,
        interval_days=30,
        cap_usd=10.0,  # a non-default cap; $3 of spend fits under it
        **paths(store),
    )
    assert report.admitted == ("project.c1", "project.c2", "project.c3")
    assert report.stopped_for_budget is False
    assert ledger_total(paths(store)["ledger_path"]) <= 10.0


def _stub_admission(monkeypatch, summary) -> None:
    from rebar.llm.evals.fixture_mining import heal as heal_mod

    monkeypatch.setattr(heal_mod, "_select_rows", lambda repo, cid: [])
    monkeypatch.setattr(heal_mod, "_emitter_skips_unbalanced", lambda cid, rows, repo: False)
    monkeypatch.setattr(heal_mod, "_run_admission", lambda *args: summary)


# Finding heal.py:327 — a transient outage (admission `incomplete`, retries exhausted) must
# NOT be classified as a reproduction failure, or a passing outage accrues toward a spurious
# three-strike quarantine.
def test_incomplete_admission_is_not_a_reproduction_failure(tmp_path, monkeypatch) -> None:
    from rebar.llm.evals.fixture_admission import AdmissionSummary
    from rebar.llm.evals.fixture_mining import heal as heal_mod

    _stub_admission(monkeypatch, AdmissionSummary(incomplete=["project.c1"]))
    attempter = heal_mod._ProductionAttempter(tmp_path, tmp_path / "l.jsonl", 25.0)

    result = attempter.run("project.c1")

    assert result.outcome != AttemptOutcome.FAILED_REPRODUCE
    assert result.outcome == "incomplete"


# Finding heal.py:444 — with several drift entries for one criterion, an un-minable reason is
# authoritative and must outrank a `non-reproducing` entry regardless of order, so the
# criterion is quarantined on first sight instead of retried three times.
def test_unminable_drift_outranks_reproduction_failure(tmp_path, monkeypatch) -> None:
    from rebar.llm.evals.fixture_admission import AdmissionSummary, DriftEntry
    from rebar.llm.evals.fixture_mining import heal as heal_mod

    def drift(reason: str) -> DriftEntry:
        return DriftEntry(
            criterion="project.c1",
            case_id="case",
            direction="fire",
            predicted="fire",
            observed="no_fire",
            reason=reason,
            ticket_id=None,
            review_event_uuid="uuid",
        )

    summary = AdmissionSummary(drift=[drift("non-reproducing"), drift("not-inline-admissible")])
    _stub_admission(monkeypatch, summary)
    attempter = heal_mod._ProductionAttempter(tmp_path, tmp_path / "l.jsonl", 25.0)

    result = attempter.run("project.c1")

    assert result.outcome == "unminable"
    assert result.reason == "not-inline-admissible"


# ── Gate-finding oracle (2615 PS3 LLM-Review BLOCK) ──────────────────────────────────────


# Finding heal.py:423 — the production solver reports NO usage rows (the live per-case runner
# does not surface a priceable row today), so nothing was recorded to the ledger and the cap
# `reserve` read `spent == 0` forever: the budget FAILED OPEN, running every criterion no
# matter how many. The loop must charge the pre-flight ESTIMATE per attempt so the cap holds
# even when an attempt records no priceable usage. This mirrors the production path exactly —
# an ADMITTED outcome carrying an EMPTY `usage_rows`.
def test_empty_usage_rows_still_fail_closed_on_budget(store, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(ledger, "LEDGER_RESERVE_USD", 0.0)
    crits = ["project.c1", "project.c2", "project.c3"]
    # No usage rows at all — the live solver's actual behaviour. tier1 × sample_n 20 ⇒ the
    # pre-flight estimate is $10 per attempt (0.5 × 20), so c1 ($10) and c2 ($20) fit under
    # the $25 cap and c3's $10 estimate exceeds the $5 remaining.
    attempter = FakeAttempter(
        {c: AttemptResult(outcome=AttemptOutcome.ADMITTED, usage_rows=()) for c in crits},
        tier="tier1",
        sample_n=20,
    )
    report = heal_fixtures(
        store,
        attempter=attempter,
        gap_source=lambda: crits,
        now=0.0,
        interval_days=30,
        cap_usd=25.0,
        **paths(store),
    )

    assert report.stopped_for_budget is True
    assert report.admitted == ("project.c1", "project.c2")
    assert "project.c3" not in report.admitted
    assert ledger_total(paths(store)["ledger_path"]) <= 25.0


def incomplete() -> AttemptResult:
    return AttemptResult(outcome=AttemptOutcome.INCOMPLETE)


# Finding heal.py:292 — the loop-level contract for a transient outage: repeated INCOMPLETE
# sweeps must never accrue toward the three-strike quarantine. Even beyond `threshold` sweeps
# the criterion is neither quarantined nor skipped — it is retried on the next sweep, because
# an outage is not evidence the fixture is unreliable.
def test_repeated_incomplete_sweeps_never_quarantine(store) -> None:  # noqa: F811
    crit = "project.outage"
    for day in (0, 30, 60, 90):  # four sweeps — past the default threshold of 3
        report, attempter = run_at(store, incomplete(), day=day, crit=crit)
        assert attempter.ran == [crit]  # still attempted, never skipped
        assert crit not in report.quarantined
    assert unreliable_titles(store) == []  # no quarantine ticket ever filed
