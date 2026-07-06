"""WS7 (epic cite-stone-sea) — prompt-hardening pass over rebar's own plan-review prompts.

The plan-review gate critiques prompt hygiene in the plans it reviews (T8: instruction-locality,
pink-elephant) but its OWN 41 reviewer prompts accumulate DO-NOT constraints. This module is the
deterministic, re-runnable guard for that one-time affirmative-framing sweep (R-6):

* the shared reviewing-stance preamble (G-12 injection directive + FP-3c forward-looking rule)
  is prepended to every plan-review PASS system prompt via ``_resolve_system``; and
* no plan-review reviewer prompt contains a *bare DO-NOT-only block* — a bullet or paragraph
  whose only content is a prohibition, with no adjacent affirmative "do this instead".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import passes

pytestmark = pytest.mark.unit

REVIEWERS_DIR = Path(passes.__file__).parent.parent / "reviewers"
PLAN_REVIEW_PROMPTS = sorted(REVIEWERS_DIR.glob("plan_review_*.md"))

# A prohibition opens a block when its FIRST sentence is a bare "don't" imperative.
_PROHIBITION_START = re.compile(r"^(do not\b|do NOT\b|don't\b|never\b|must not\b)", re.IGNORECASE)
# An adjacent affirmative "do this instead" REDIRECT — any of these in the same block clears it.
# These are genuine redirect cues (do X rather than Y / X is done elsewhere); a mere em-dash or
# colon does NOT count (an em-dash clause is often failure-narration, exactly what R-6 says to cut),
# nor do generic verbs, so the detector bites on real bare prohibitions.
_AFFIRMATIVE_CUE = re.compile(
    r"(instead\b|rather\b|prefer\b|→|\buse\b|leave it\b|reserve\b|"
    r"compute[sd]?\b|separate pass|is done|handled elsewhere)",
    re.IGNORECASE,
)


def _strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[text.find("\n", end + 1) + 1 :]
    return text


def _blocks(body: str) -> list[str]:
    """Split a prompt body into bullets (one per '- '/'* ' item, joined with continuations)
    and blank-line-separated paragraphs — the unit the R-6 definition scopes."""
    blocks: list[str] = []
    current: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        is_bullet = bool(re.match(r"^\s*[-*]\s+", line))
        if is_bullet or not line.strip():
            if current:
                blocks.append(" ".join(current).strip())
                current = []
            if is_bullet:
                current = [re.sub(r"^\s*[-*]\s+", "", line)]
        else:
            current.append(line.strip())
    if current:
        blocks.append(" ".join(current).strip())
    return [b for b in blocks if b]


def bare_do_not_only_blocks(text: str) -> list[str]:
    """Return the bare-DO-NOT-only blocks in a prompt body: a block whose FIRST sentence is a
    prohibition imperative AND that carries no adjacent affirmative "do this instead" cue."""
    out = []
    for block in _blocks(_strip_front_matter(text)):
        if _PROHIBITION_START.match(block) and not _AFFIRMATIVE_CUE.search(block):
            out.append(block)
    return out


def test_resolve_system_preamble() -> None:
    # AC1: the shared reviewing-stance preamble (injection directive + forward-looking rule) is
    # prepended to EVERY plan-review pass reviewer resolved through _resolve_system.
    cfg = LLMConfig()
    for pid in (
        passes.PASS_FINDER,
        passes.PASS_VERIFIER,
        passes.PASS_CONTAINER,
        passes.PASS_ISF,
        passes.PASS_COMPLETION,
        passes.PASS_COACH,
        passes.PASS_NOVELTY,
    ):
        system = passes._resolve_system(pid, "PLAN-BODY-MARKER", cfg)
        assert "MATERIAL UNDER REVIEW" in system, f"{pid}: injection directive missing"
        assert "Evaluate the spec AS WRITTEN" in system, f"{pid}: forward-looking rule missing"
        # prepended (stance leads), plan body still present
        assert system.index("MATERIAL UNDER REVIEW") < system.index("PLAN-BODY-MARKER")


def test_bare_do_not_detector_is_not_vacuous() -> None:
    # The detector must actually bite: a bullet that is only a prohibition IS flagged; the same
    # prohibition with an adjacent "do this instead" redirect is NOT. (Guards the 0-count below
    # from being a vacuously-passing check.)
    assert bare_do_not_only_blocks("- Do NOT emit a severity score for the finding.")
    assert bare_do_not_only_blocks("- Never restate the finding text verbatim.")
    assert not bare_do_not_only_blocks(
        "- Do NOT emit a severity score — a separate pass computes it."
    )
    assert not bare_do_not_only_blocks("- Reference findings by id rather than restating them.")


def test_no_bare_do_not_only_blocks() -> None:
    # AC2: the affirmative-framing sweep leaves zero bare-DO-NOT-only blocks across all 41 files.
    assert PLAN_REVIEW_PROMPTS, "no plan_review_*.md reviewer prompts found"
    offenders: dict[str, list[str]] = {}
    for path in PLAN_REVIEW_PROMPTS:
        bare = bare_do_not_only_blocks(path.read_text())
        if bare:
            offenders[path.name] = bare
    assert not offenders, "bare DO-NOT-only blocks remain:\n" + "\n".join(
        f"  {name}: {blocks}" for name, blocks in offenders.items()
    )
