"""Scheduled per-criterion fixture-mining heal loop (ticket 1cef).

The heal loop mines regression fixtures for plan-review criteria that still lack an eval
spec. It composes the existing selection → emit → admission plumbing behind an injectable
``attempter`` seam, quarantines criteria it cannot reliably mine behind an
``unreliable-criterion: <id>`` ticket, and bounds its own spend against the replay ledger.
"""

from rebar.llm.evals.fixture_mining.heal import (
    UNRELIABLE_TITLE_PREFIX,
    AttemptOutcome,
    AttemptResult,
    HealReport,
    heal_fixtures,
)

__all__ = [
    "UNRELIABLE_TITLE_PREFIX",
    "AttemptOutcome",
    "AttemptResult",
    "HealReport",
    "heal_fixtures",
]
