"""Held-out oracle for ticket f0ff — cases the implementer does NOT see while
filling in the per-command table against the happy-path conformance test.

Covers the newly-documented codes (AC2), the forward-compatibility contract
sentence (AC5), and the teeth of the conformance guard itself (AC4).
"""

from __future__ import annotations

import re
from pathlib import Path

from test_exit_codes_doc_conformance import (
    dispatcher_arms,
    documented_arms,
    undocumented_arms,
)

_DOC = Path(__file__).resolve().parents[3] / "docs" / "exit-codes.md"


def _text() -> str:
    return _DOC.read_text(encoding="utf-8")


# ── AC4 teeth: the conformance guard is not a tautology ───────────────────────
def test_conformance_guard_flags_an_undocumented_arm() -> None:
    # A synthetic new command with no row is reported; once documented it clears.
    assert undocumented_arms({"brand-new-cmd"}, set()) == {"brand-new-cmd"}
    assert undocumented_arms({"brand-new-cmd"}, {"brand-new-cmd"}) == set()


def test_guard_derives_all_seventy_five_arms() -> None:
    # The union of the two authoritative sources — a guard that under-counts the
    # arm set would silently pass while arms went undocumented.
    assert len(dispatcher_arms()) == 75


def test_no_arm_is_undocumented_after_the_fix() -> None:
    assert undocumented_arms(dispatcher_arms(), documented_arms()) == set()


# ── AC2: the previously-undocumented codes are documented in the codes table ──
def test_codes_3_4_12_75_78_are_documented() -> None:
    codes = set(re.findall(r"^\| `(\d+)`", _text(), re.MULTILINE))
    for code in ("3", "4", "12", "75", "78"):
        assert code in codes, f"exit code {code} is not documented in the codes table"


# ── AC5: forward-compatibility contract sentence ──────────────────────────────
def test_forward_compat_sentence_present() -> None:
    # The contract sentence: an unknown/unlisted non-zero code MUST be treated as
    # failure. Require the tokens co-located in a single line so a stray "must" /
    # "fail" elsewhere in the document cannot satisfy it.
    for line in _text().splitlines():
        low = line.lower()
        if (
            "must" in low
            and re.search(r"non-?zero", low)
            and ("fail" in low)
            and re.search(r"unknown|unlisted|unrecognized|not listed|any other", low)
        ):
            return
    raise AssertionError(
        "docs/exit-codes.md lacks the forward-compatibility sentence: an unknown "
        "non-zero exit code MUST be treated as failure"
    )
