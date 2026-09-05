"""Browser-tier non-execution guard — bug 337e-b558-17a2-49bd.

The e2e browser tier drives the real editor bundle in headless Chromium, and it self-skips
whenever Node, Playwright, Chromium or the built bundle is missing. That is the right runtime
behaviour — the Python unit tiers are the always-on floor — but a bare ``pytest.skip`` makes
the tier's ABSENCE indistinguishable from its SUCCESS: a build reports green while a whole
class of assertions never executed, and nothing in the summary says so.

This module makes the difference between the two states explicit and enforced. A deliberate
non-execution must be LICENSED by a record in the tree
(``tests/e2e/browser-tier-optout.toml``); every browser fixture routes its non-execution
through :func:`tier_unavailable`, which:

* raises a **loud skip** when the record is present and complete — the reason names the
  record, the ticket that decided it, and the concrete detail — and emits a
  :class:`BrowserTierNotRunWarning` so the run's own warnings summary tells a reader the tier
  did not run; or
* raises a **hard failure** when the record is absent, unparseable, or has a blank reason.

So deleting the record turns the tier red rather than quietly green. A marker nothing enforces
would simply relocate the original defect one level up.

**Portability.** Everything here reads one repository file. There is no environment variable,
no CI detection and no provider API, so a checkout with no CI provider at all behaves exactly
like one with any: loud skip with the record, hard failure without it.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest
import tomllib

#: The recorded opt-out. Module-level so a test can redirect it at a tree that has none.
RECORD_PATH = Path(__file__).with_name("browser-tier-optout.toml")

#: Top-level table the record must carry.
_TABLE = "browser_tier"

#: Prefix every licensed non-execution reason starts with. Tests and readers key on it, and
#: it is deliberately unlike anything pytest prints for a passing test.
NOT_RUN_BANNER = "BROWSER TIER DELIBERATELY NOT RUN"

_MISSING_RECORD = (
    "BROWSER TIER SILENTLY SKIPPED — {detail}.\n"
    "This tier did not execute and nothing in the tree licenses that. A browser test may only\n"
    "be skipped when {record} records the decision (a `[{table}]` table with a `scope`, a\n"
    "`decided_by` ticket id and a non-blank `reason`); {problem}.\n"
    "Either make the tier runnable here (install Node + Playwright + Chromium), or restore the\n"
    "record so the non-execution is a deliberate, readable decision. Do NOT silence this by\n"
    "turning the call back into a bare `pytest.skip` — a skip that nothing licenses reads as\n"
    "coverage while providing none (bug 337e-b558-17a2-49bd)."
)


class BrowserTierNotRunWarning(UserWarning):
    """Raised whenever the browser tier is licensed not to run.

    A warning rather than a print: pytest surfaces warnings in its own summary even under
    ``-q`` (and groups identical ones, so a whole skipped tier costs the reader one entry),
    and the channel is xdist-safe, so the fact travels from a worker to the report a human
    actually reads.
    """


@dataclass(frozen=True)
class OptOutRecord:
    """A complete, parsed opt-out declaration."""

    scope: str
    decided_by: str
    reason: str
    path: Path


def load_record(path: Path | None = None) -> OptOutRecord | None:
    """Return the opt-out record, or ``None`` when the tree does not license a skip.

    ``None`` covers every incomplete state on purpose — absent file, unreadable file, invalid
    TOML, missing table, blank field. A half-written record is not a decision, and treating it
    as one would restore exactly the silent-skip surface this guard removes.
    """
    record_path = RECORD_PATH if path is None else path
    try:
        raw = tomllib.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None
    table = raw.get(_TABLE)
    if not isinstance(table, dict):
        return None
    fields = {}
    for key in ("scope", "decided_by", "reason"):
        value = table.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        fields[key] = value.strip()
    return OptOutRecord(path=record_path, **fields)


def _describe(record: OptOutRecord, detail: str) -> str:
    """The per-test reason. Short on purpose: it is repeated once per skipped test."""
    return (
        f"{NOT_RUN_BANNER} ({record.scope}) — {detail}. "
        f"These browser assertions did NOT execute; this is a recorded decision, not a pass. "
        f"Why: {record.path.name} (ticket {record.decided_by})."
    )


def _announce(record: OptOutRecord, detail: str) -> str:
    """The warning text. Carries the recorded reasoning in full, so a reader of the run's
    summary gets the whole decision without going to look for the file."""
    return (
        f"{NOT_RUN_BANNER} ({record.scope}) — {detail}.\n"
        f"These browser assertions did NOT execute; this is a recorded decision, not a pass.\n"
        f"Recorded in {record.path.name} (decided on ticket {record.decided_by}):\n"
        f"{record.reason.strip()}"
    )


def tier_unavailable(detail: str, *, path: Path | None = None) -> NoReturn:
    """End the calling test: a licensed loud skip, or a failure when nothing licenses it.

    ``detail`` names the concrete reason the tier cannot run here (no Node, provisioning
    failed, Chromium absent, bundle missing) and is carried into both outcomes, so the reader
    is never left with "skipped" and no cause. Never returns.
    """
    record = load_record(path)
    if record is None:
        record_path = RECORD_PATH if path is None else path
        problem = (
            "that file is absent"
            if not record_path.exists()
            else "that file is present but incomplete or unparseable"
        )
        pytest.fail(
            _MISSING_RECORD.format(
                detail=detail, record=record_path, table=_TABLE, problem=problem
            ),
            pytrace=False,
        )
    warnings.warn(_announce(record, detail), BrowserTierNotRunWarning, stacklevel=2)
    pytest.skip(_describe(record, detail))
