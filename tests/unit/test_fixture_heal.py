"""Happy-path oracle + shared scaffolding for the fixture-mining heal loop (ticket 1cef).

VISIBLE to the implementation subagent: this file specifies the loop's core happy path (a
gap criterion is attempted and admitted; a spec-covered criterion is out of scope). The edge
and end-to-end oracle (quarantine breaker, first-encounter un-minable, idempotent filing, the
budget stop, the cleared-env subprocess, the due-stamp interval) is HELD OUT in
``test_fixture_heal_heldout.py`` and validated by the orchestrator.

Every assertion targets OBSERVABLE behaviour: the returned ``HealReport``, tickets in a
temporary store, and the ledger total — never private structure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.eval import eval_spec_path
from rebar.llm.evals.fixture_mining import (
    UNRELIABLE_TITLE_PREFIX,
    AttemptOutcome,
    AttemptResult,
    heal_fixtures,
)
from rebar.llm.evals.fixture_selection import _default_criteria

pytestmark = pytest.mark.unit

DAY_SECONDS = 86400


# ── shared scaffolding (imported by the held-out oracle) ─────────────────────────────────
@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch rebar store: local git, no remote, sync off, forced HMAC key."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-1cef")
    rebar.init_repo(repo_root=str(repo))
    return repo


def usage_row(model: str = "bedrock:m", input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    """A priceable usage row (mirrors ``test_fixture_admission.usage_row``)."""
    return {
        "model": model,
        "provider": "bedrock",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "timestamp": "2026-07-30T00:00:00+00:00",
    }


def stub_prices(monkeypatch: pytest.MonkeyPatch, usd_per_row: float) -> None:
    """Install a fake ``genai_prices`` so ``ledger.finalize`` prices deterministically."""
    import types
    from typing import Any

    stub = types.ModuleType("genai_prices")

    class Usage:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _Price:
        def __init__(self, total_price: float) -> None:
            self.total_price = total_price

    def calc_price(usage: Any, model_ref: Any, provider_id: Any = None, **_: Any) -> _Price:
        return _Price(usd_per_row)

    stub.Usage = Usage  # type: ignore[attr-defined]
    stub.calc_price = calc_price  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "genai_prices", stub)


class FakeAttempter:
    """A scripted, model-free attempter. ``results`` maps a criterion id to the
    ``AttemptResult`` its ``run`` returns; ``plan`` returns ``(tier, sample_n)``. Records the
    ids it planned and ran for call assertions."""

    def __init__(
        self,
        results: dict[str, AttemptResult],
        *,
        tier: str = "tier1",
        sample_n: int = 1,
    ) -> None:
        self._results = results
        self._tier = tier
        self._sample_n = sample_n
        self.planned: list[str] = []
        self.ran: list[str] = []

    def plan(self, criterion_id: str) -> tuple[str, int]:
        self.planned.append(criterion_id)
        return (self._tier, self._sample_n)

    def run(self, criterion_id: str) -> AttemptResult:
        self.ran.append(criterion_id)
        return self._results[criterion_id]


def paths(store: Path) -> dict[str, str]:
    """State + ledger paths under the store's scratch dir."""
    scratch = store / ".rebar"
    scratch.mkdir(exist_ok=True)
    return {
        "state_path": str(scratch / "fixture_heal_state.json"),
        "ledger_path": str(scratch / "fixture_heal_ledger.jsonl"),
    }


def ledger_total(ledger_path: str) -> float:
    """Sum the ``usd`` recorded across the ledger (empty file → 0.0)."""
    p = Path(ledger_path)
    if not p.is_file():
        return 0.0
    return sum(json.loads(line)["usd"] for line in p.read_text().splitlines() if line.strip())


def unreliable_titles(store: Path) -> list[str]:
    """Titles of OPEN unreliable-criterion tickets in the store."""
    return [
        t["title"]
        for t in rebar.list_tickets(status="open", repo_root=str(store))
        if t["title"].startswith(UNRELIABLE_TITLE_PREFIX)
    ]


def admitted(**extra) -> AttemptResult:
    return AttemptResult(outcome=AttemptOutcome.ADMITTED, usage_rows=(usage_row(),), **extra)


# ── AC1: a spec-covered criterion is out of a run's attempt list (gap-only scope) ────────
def test_gap_only_scope_excludes_spec_covered_criterion(store: Path) -> None:
    gate = "plan_review"
    gap_before = set(_default_criteria(str(store), gate))
    assert len(gap_before) >= 2, "need at least two gap criteria to prove the exclusion"
    covered = sorted(gap_before)[0]

    # Materialize an eval spec for `covered` so it leaves the gap.
    spec = eval_spec_path(criterion_prompt_id(covered), str(store))
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("cases: []\n", encoding="utf-8")

    attempter = FakeAttempter({c: admitted() for c in gap_before})
    report = heal_fixtures(
        store,
        attempter=attempter,
        now=0.0,
        interval_days=30,
        **paths(store),
    )

    assert covered not in report.attempted
    assert set(report.attempted) == gap_before - {covered}


# ── Happy path: a gap criterion is attempted, admitted, and files no ticket ──────────────
def test_admitted_gap_criterion_is_reported_and_files_no_ticket(store: Path, monkeypatch) -> None:
    stub_prices(monkeypatch, usd_per_row=0.01)
    crit = "project.alpha"
    attempter = FakeAttempter({crit: admitted()})
    report = heal_fixtures(
        store,
        attempter=attempter,
        gap_source=lambda: [crit],
        now=0.0,
        interval_days=30,
        **paths(store),
    )

    assert report.ran is True
    assert report.attempted == (crit,)
    assert report.admitted == (crit,)
    assert report.quarantined == ()
    assert unreliable_titles(store) == []
