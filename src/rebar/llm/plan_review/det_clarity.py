"""Blocking clarity-floor DET checks P10 + P11 (ticket 49b8).

Backtests over the 7-day population (153 tickets; 104 passed-plan FP set, 55
blocked-plan hit set) and a 200-ticket extended set supported two deterministic
floor additions, operator-approved for BLOCKING. ``p6_ac_quality`` is monolithic
and stays advisory, so both rules are SEPARATE DET checks with their own coverage
entries rather than promotions inside P6:

* **P10 verification-presence** — a leaf plan passes if it has a ``## Testing``
  or ``## Verification`` H2 section, OR >=1 ``- [ ]``/``- [x]`` AC checklist item
  that contains an inline code span (backtick-fenced token) or matches the
  verification-vocabulary regex (:data:`_VERIFICATION_VOCAB_RE`). That vocabulary
  is EXHAUSTIVE for the check — nothing else qualifies. A container is a natural
  pass (its children carry the verification detail). **BLOCKS.**
* **P11 AC vagueness** — the boundary-FIXED vague lexicon
  (:data:`_VAGUE_LEXICON_FIXED`: both word boundaries; ``clean`` dropped —
  the old prefix matching fired on "cleanly"/"lint clean" and measured FPs,
  while the fixed rule measured 0 FPs across 304 passed plans; ``etc.`` kept)
  scanned over AC item lines only (:func:`.det_operator_attested.ac_item_lines`).
  **BLOCKS.**

Code-span handling (:func:`vague_hits_in_line`): inline code-span positions are
recorded on the ORIGINAL line before the spans are blanked for matching; a
lexicon hit inside a recorded span never fires, and an ``etc.`` occurrence whose
start lies within :data:`ETC_SPAN_PROXIMITY_CHARS` characters after a recorded
span's end is exempt — a non-exhaustive enumeration of already-concrete examples
("run ``git grep …``, etc.") is not vague.

Extracted alongside :mod:`.det_lint` / :mod:`.det_operator_attested` (the same
module-size seam) so the size-ceilinged :mod:`.det_floor` does not grow past the
800-LOC cap; ``det_floor`` imports the two checks into ``DET_CHECKS`` and shares
:func:`vague_hits_in_line` for P6's advisory lexicon so the two surfaces agree.
No runtime import back into ``det_floor`` at module level (``DetResult`` is
imported lazily, the :func:`.det_lint.p9_file_impact_coverage` precedent), so
the import is cycle-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .det_floor import DetResult, PlanContext

import re

# An inline Markdown code span: a backtick-fenced token on one line.
_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")

# ``## Testing`` / ``## Verification`` H2 heading (start of line).
_TESTING_H2_RE = re.compile(r"^##\s+(?:Testing|Verification)\b", re.MULTILINE | re.IGNORECASE)

# The EXHAUSTIVE verification vocabulary for P10 — an AC item matching this (or
# carrying an inline code span) counts as naming its verification. Nothing else
# qualifies.
_VERIFICATION_VOCAB_RE = re.compile(
    r"\b(?:pytest|test_[a-z0-9_]+|make\s+\w+|rebar\s+\w+|git\s+\w+|grep\b|assert\w*"
    r"|checked:|verif(?:y|ied|ies|ication)|exit\s+code)\b",
    re.IGNORECASE,
)

# The boundary-FIXED vague lexicon (P11 blocking + P6 advisory). Every term is
# matched with BOTH word boundaries — the old P6 rule matched word prefixes with
# no trailing boundary, so ``clean`` fired on "cleanly"/"lint clean". ``clean``
# is DROPPED (dominant false-positive source: "runs clean", "clean parses",
# "cleaned up"); ``etc.`` is KEPT with the code-span-proximity exemption; all
# other terms of the original lexicon are kept.
_VAGUE_LEXICON_FIXED = (
    "better",
    "improved",
    "improve",
    "sufficient",
    "robust",
    "robustly",
    "appropriate",
    "appropriately",
    "properly",
    "reasonable",
    "as needed",
    "etc.",
    "and so on",
    "good",
    "nice",
    "optimal",
    "efficient",
)
# (?<!\w)…(?!\w) rather than \b…\b so terms ending in a non-word char ("etc.")
# still get a real trailing boundary (\b after "." only matches before a word
# character, which would silently drop end-of-line "etc.").
_VAGUE_TERM_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (term, re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE))
    for term in _VAGUE_LEXICON_FIXED
)

# An ``etc.`` starting within this many characters AFTER a code span's end is a
# non-exhaustive enumeration of already-concrete examples — exempt.
ETC_SPAN_PROXIMITY_CHARS = 30


def code_span_ranges(line: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` positions of inline code spans on the ORIGINAL line."""
    return [(m.start(), m.end()) for m in _CODE_SPAN_RE.finditer(line)]


def vague_hits_in_line(line: str) -> list[str]:
    """The fixed-lexicon vague terms present on one line, code-span aware.

    Span positions are recorded on the ORIGINAL line, then the spans are blanked
    (length-preserving, so positions stay valid) before matching. A hit inside a
    recorded span never fires; an ``etc.`` whose start lies within
    :data:`ETC_SPAN_PROXIMITY_CHARS` characters after a recorded span's end is
    exempt. Returns each matched term at most once, in lexicon order."""
    spans = code_span_ranges(line)
    blanked = line
    for start, end in spans:
        blanked = blanked[:start] + " " * (end - start) + blanked[end:]
    hits: list[str] = []
    for term, rx in _VAGUE_TERM_RES:
        for m in rx.finditer(blanked):
            pos = m.start()
            if any(start <= pos < end for start, end in spans):
                continue  # inside a recorded span (defensive; blanking already prevents this)
            if term == "etc." and any(
                end <= pos <= end + ETC_SPAN_PROXIMITY_CHARS for _start, end in spans
            ):
                continue  # concrete-enumeration exemption
            hits.append(term)
            break
    return hits


def p10_verification_presence(ctx: PlanContext) -> DetResult:
    """BLOCKING. A leaf plan must say how its outcome is verified: a
    ``## Testing``/``## Verification`` H2 section, OR >=1 AC checklist item with
    an inline code span or a verification-vocabulary match. A container is a
    natural pass — its children carry the verification detail."""
    from .det_floor import DetResult  # lazy: det_floor imports this module at load
    from .det_operator_attested import ac_item_lines

    if ctx.has_children:
        return DetResult(
            "P10", "verification-presence", "pass", coverage={"ran": True, "container": True}
        )
    has_section = bool(_TESTING_H2_RE.search(ctx.description))
    items = ac_item_lines(ctx.plan_text)
    qualifying = [
        it for it in items if _CODE_SPAN_RE.search(it) or _VERIFICATION_VOCAB_RE.search(it)
    ]
    cov = {
        "ran": True,
        "testing_section": has_section,
        "ac_items": len(items),
        "qualifying_ac_items": len(qualifying),
    }
    if has_section or qualifying:
        return DetResult("P10", "verification-presence", "pass", coverage=cov)
    return DetResult(
        "P10",
        "verification-presence",
        "fail",
        blocking=True,
        finding={
            "finding": "The plan states no verification: no Testing/Verification section and "
            "no acceptance criterion names a proving command or verification step.",
            "evidence": [
                f"{len(items)} AC item(s), none with an inline code span or verification "
                "vocabulary; no `## Testing` / `## Verification` section."
            ],
            "impact": (
                "Without a stated verification the definition of done is unfalsifiable — "
                "the completion verifier has nothing objective to check."
            ),
            "suggested_fix": (
                "Add a `## Testing` section, or make >=1 acceptance criterion name its proof "
                "(a backticked command, a test, or 'checked: <how>')."
            ),
        },
        coverage=cov,
    )


def p11_ac_vagueness(ctx: PlanContext) -> DetResult:
    """BLOCKING. The fixed vague lexicon over AC item lines only. A vague term in
    an acceptance criterion makes 'done' subjective; prose elsewhere is P6's
    advisory business, not a block."""
    from .det_floor import DetResult  # lazy: det_floor imports this module at load
    from .det_operator_attested import ac_item_lines

    items = ac_item_lines(ctx.plan_text)
    flagged = [(it, hits) for it in items if (hits := vague_hits_in_line(it))]
    cov = {"ran": True, "ac_items": len(items), "vague_items": len(flagged)}
    if not flagged:
        return DetResult("P11", "ac-vagueness", "pass", coverage=cov)
    return DetResult(
        "P11",
        "ac-vagueness",
        "fail",
        blocking=True,
        finding={
            "finding": "Acceptance criteria contain vague/subjective terms.",
            "evidence": [
                f"{line.strip()} — vague term(s): {', '.join(hits)}" for line, hits in flagged
            ],
            "impact": "A vague acceptance criterion cannot be verified objectively.",
            "suggested_fix": (
                "Replace each vague term with an observable outcome (a concrete value, "
                "behavior, or proving command)."
            ),
        },
        coverage=cov,
    )
