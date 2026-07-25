"""One criteria vocabulary in the reviewer prompts and routing index (ticket 2aa6).

The plan-review prompts and `criteria_routing.json` pooled "acceptance/success
criterion" as one concept while spelling it two ways. This pins the collapsed
vocabulary as a property of the shipped prompt corpus.

Deliberately NOT collapsed, and asserted here so a broad find-and-replace cannot
quietly swallow them:

* `plan_review_T2.md` — "a pilot with metrics, or stated success criteria for a
  trial" is generic English about whether an EXPERIMENT succeeded, not the ticket
  heading. Renaming it would change T2's meaning.
* `plan_review_verifier.md` / `plan_review_verifier_agentic.md` — the
  `dod_uncertifiable` "definition-of-done / success criterion" wording is a
  separate concept, out of scope for this story and its epic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "rebar"
_REVIEWERS = _SRC / "llm" / "reviewers"
_ROUTING = _SRC / "llm" / "plan_review" / "criteria_routing.json"

_SC_RE = re.compile(r"success[ _-]criteri(a|on)", re.IGNORECASE)

# The three sites the story's Scope deliberately excludes.
_EXCLUDED = {
    "plan_review_T2.md",
    "plan_review_verifier.md",
    "plan_review_verifier_agentic.md",
}


def _sc_hits(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if _SC_RE.search(ln)]


def _in_scope_files() -> list[Path]:
    return sorted(p for p in _REVIEWERS.glob("*.md") if p.name not in _EXCLUDED)


# ── the collapse ──────────────────────────────────────────────────────────────
def test_no_success_criteria_vocabulary_in_the_reviewer_prompts() -> None:
    offenders = {p.name: _sc_hits(p) for p in _in_scope_files() if _sc_hits(p)}
    assert offenders == {}, f"SC vocabulary still in reviewer prompts: {offenders}"


def test_no_success_criteria_vocabulary_in_the_routing_index() -> None:
    assert _sc_hits(_ROUTING) == []


# ── held-out: the anti-overreach guards ───────────────────────────────────────
def test_the_t2_pilot_metrics_phrase_is_left_intact() -> None:
    """T2's phrase is generic English, not the heading — it must survive."""
    hits = _sc_hits(_REVIEWERS / "plan_review_T2.md")
    assert len(hits) == 1, f"expected T2's single pilot-metrics phrase, got {len(hits)}"
    assert "trial" in hits[0], "T2's surviving hit is not the pilot-metrics phrase"


@pytest.mark.parametrize("name", ["plan_review_verifier.md", "plan_review_verifier_agentic.md"])
def test_the_dod_uncertifiable_wording_is_left_intact(name: str) -> None:
    """`definition-of-done / success criterion` is a separate concept, out of scope."""
    hits = _sc_hits(_REVIEWERS / name)
    assert len(hits) == 1
    assert "dod_uncertifiable" in hits[0]


# ── held-out: the bare-SC abbreviation ────────────────────────────────────────
def test_no_bare_sc_abbreviation_in_g3() -> None:
    """G3 carried the repo's only two bare `SC` tokens; both must be renamed."""
    text = (_REVIEWERS / "plan_review_G3.md").read_text()
    assert re.findall(r"\bSCs?\b", text) == []


def test_g3_still_teaches_the_contradiction_patterns() -> None:
    """Renaming the token must not delete the guidance it belonged to."""
    text = (_REVIEWERS / "plan_review_G3.md").read_text()
    assert "AC-CONTRADICTION PATTERNS" in text
    assert "annotates exceptions" in text


# ── held-out: the generated guide ─────────────────────────────────────────────
def test_generated_criteria_guide_is_regenerated_and_still_generated() -> None:
    """The guide is a generated snapshot of the registry, so it embeds each criterion's
    prompt verbatim — including T2's deliberately-excluded pilot-metrics phrase. The
    contract is therefore "no SC vocabulary EXCEPT that one echo", not "none at all".
    """
    guide = Path(__file__).resolve().parents[2] / "docs" / "plan-review-criteria-guide.md"
    text = guide.read_text()

    hits = [ln for ln in text.splitlines() if _SC_RE.search(ln)]
    unexpected = [ln for ln in hits if "for a trial" not in ln]
    assert unexpected == [], f"SC vocabulary survives in the generated guide: {unexpected}"
    # And the excluded phrase really is still carried through, not silently dropped.
    assert len(hits) == 1

    # The guide is generated; hand-editing it instead of regenerating would drop the banner.
    assert re.search(r"(?i)generated", text), "the GENERATED banner is missing"
