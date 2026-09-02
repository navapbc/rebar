"""No live blocking-severity guidance after its severity-retirement epic
(ticket 22ec-e638-c896-4b54).

Closed epic `pink-complex-xenurine` / child `byzantine-spinelike-penguin` removed
`ReceiverConfig.blocking_severities` in commit 8db373933654c7c21fa214565426f562ca55cd80.
`docs/gerrit-aws-setup.md` and `src/rebar/review_bot/config.py` kept describing it
as a live, configurable knob. These tests pin the corrected state: the current
PASS/BLOCK decision is the four-pass gate's deterministic Pass-3 blocker
(criteria_routing.json thresholds), not a severity threshold, while historical ADRs
(0009, 0108) keep their accurate record of the removed knob untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

_MAINTAINED_DOCS = (
    "docs/gerrit-aws-setup.md",
    "src/rebar/review_bot/config.py",
)

_HISTORICAL_DOCS = (
    "docs/adr/0009-review-bot-pipe.md",
    "docs/adr/0108-retire-severity-label.md",
)


def _text(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


# ─────────────────────────── HAPPY PATH ──────────────────────────────────────


@pytest.mark.parametrize("relpath", _MAINTAINED_DOCS)
def test_maintained_surface_has_no_live_blocking_severity_claim(relpath: str):
    """No maintained doc/config module claims a live, configurable blocking-severity
    threshold or set -- the knob was removed."""
    text = _text(relpath)
    assert "blocking-severity set" not in text
    assert "configured\n   blocking-severity threshold" not in text
    assert "the blocking-severity threshold is the\none knob" not in text


def test_receiver_config_has_no_blocking_severities_field():
    """ReceiverConfig no longer defines a blocking_severities field."""
    config_src = (REPO_ROOT / "src" / "rebar" / "review_bot" / "config.py").read_text(
        encoding="utf-8"
    )
    assert "blocking_severities" not in config_src


def test_gerrit_aws_setup_names_pass3_as_the_decision_source():
    """The maintained deployment doc names the four-pass Pass-3 blocker as the
    current, sole decision source for PASS/BLOCK."""
    text = _text("docs/gerrit-aws-setup.md")
    assert "four-pass\n   gate's deterministic Pass-3 blocker" in text


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


@pytest.mark.parametrize("relpath", _HISTORICAL_DOCS)
def test_historical_adrs_retain_blocking_severities_record(relpath: str):
    """The removal is recorded, not erased: the historical ADRs still name
    BLOCKING_SEVERITIES as the retired configuration -- they are NOT rewritten."""
    text = _text(relpath)
    assert "BLOCKING_SEVERITIES" in text


def test_adapter_module_already_documents_removal_unchanged():
    """adapter.py already correctly documented the removal before this ticket;
    confirm it still does (this ticket does not touch it)."""
    text = _text("src/rebar/review_bot/adapter.py")
    assert "blocking_severities`` has since been removed entirely" in text
