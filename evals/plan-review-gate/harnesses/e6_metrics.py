"""E6 pure metrics + permutation helpers (ticket a880 — judge reliability).

This module is the CI-testable, **LLM-free** core of experiment E6. It imports ONLY the
Python standard library so ``tests/unit/test_e6_agreement.py`` can exercise it without the
``[agents]`` extra, an ``anthropic`` client, or a network. The LLM-driving harness
(``e6_judge_reliability.py``) imports these helpers; the reverse never happens.

It implements, exactly as ticket a880's plan defines them:

* **Agreement math** — :func:`fleiss_kappa` (raters = the N interchangeable votes;
  subjects = findings/plans; categories = the decision/verdict space) and
  :func:`raw_agreement` (fraction of subjects whose votes are all identical), plus a
  :func:`compute_agreement` convenience that returns both alongside the pass/fail
  against the pre-registered floors (κ ≥ 0.6 AND raw ≥ 0.8).
* **Order-shuffle permutations** — :func:`permute_sections` reorders a plan's top-level
  ``##`` blocks into exactly ``n`` DISTINCT orderings (permutation 0 = identity; the rest
  drawn from a per-plan-seeded PRNG), preserving every section's content verbatim.
* **Infra-INDETERMINATE exclusion + retry-cap policy** — :func:`is_infra_indeterminate_vote`
  / :func:`is_infra_indeterminate_verdict` classify an execution failure (drop-and-re-run)
  vs. a stable judge outcome (keep), and :func:`finalize_votes` applies the bounded
  retry-cap, recording an explicit *excluded* row when a subject cannot reach the vote
  target instead of silently padding.
"""

from __future__ import annotations

import random
import re
from typing import Any

# ── Pre-registered constants (mirrored in runs/e6_prereg.json) ────────────────────────
KAPPA_FLOOR = 0.6  # Landis–Koch "substantial"
RAW_FLOOR = 0.8  # prevalence-robust co-floor (κ deflates under skewed base rates)
VOTE_TARGET = 3  # sota:44 — 3-vote self-consistency saturates the available gain
MAX_INFRA_RETRIES = 3  # extra attempts allowed to REPLACE infra-INDETERMINATE draws
ATTEMPT_BUDGET = VOTE_TARGET + MAX_INFRA_RETRIES  # hard cap on judge calls per subject

# The Pass-3 self-consistency agreement space and the gate-verdict order-shuffle space.
DECISION_CATEGORIES = ("block", "advisory", "dropped", "indeterminate")
VERDICT_CATEGORIES = ("PASS", "BLOCK", "INDETERMINATE")

# A top-level section heading: a line that opens with exactly two ``#`` (not ``###``).
_TOP_SECTION_RE = re.compile(r"^##(?!#)")


# ── Section splitting + order-shuffle permutations ────────────────────────────────────
def split_plan_sections(plan: str) -> tuple[str, list[str]]:
    """Split ``plan`` into ``(head, blocks)`` where ``head`` is everything before the first
    top-level ``##`` heading and ``blocks`` is the list of ``##`` section blocks (each block
    spans its heading line up to — but not including — the next top-level heading).

    Line endings are preserved, so ``head + "".join(blocks)`` reproduces ``plan`` byte for
    byte. A plan with no ``##`` heading yields ``(plan, [])``.
    """
    lines = plan.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if _TOP_SECTION_RE.match(ln)]
    if not starts:
        return plan, []
    head = "".join(lines[: starts[0]])
    blocks: list[str] = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        blocks.append("".join(lines[start:end]))
    return head, blocks


def count_top_sections(plan: str) -> int:
    """Number of top-level ``##`` sections in ``plan`` (the order-shuffle inclusion count)."""
    return len(split_plan_sections(plan)[1])


def plan_seed(plan_id: str) -> int:
    """The pinned per-plan PRNG seed: ``int(plan_id.split("-")[0], 16)`` (a880 plan)."""
    return int(plan_id.split("-")[0], 16)


def permute_sections(plan: str, plan_id: str, n_perms: int = 3) -> list[dict[str, Any]]:
    """Return exactly ``n_perms`` DISTINCT section-order permutations of ``plan``.

    Each element is ``{"permutation_index", "section_order", "text"}`` where ``section_order``
    is a permutation of ``range(n_sections)`` and ``text`` is the plan rebuilt with the ``##``
    blocks in that order (the head and every block's content are preserved verbatim — only the
    block order changes).

    Permutation 0 is the IDENTITY (the plan's original section order). Permutations 1..n-1 are
    drawn by repeatedly shuffling the index list with a single PRNG seeded by
    :func:`plan_seed` and taking the next ordering not already selected, so the whole set is
    reproducible under the pinned seed and mutually distinct. Requires enough sections to admit
    ``n_perms`` distinct orderings (``n_sections! >= n_perms``); raises ``ValueError`` otherwise.
    """
    head, blocks = split_plan_sections(plan)
    n = len(blocks)
    import math

    if math.factorial(n) < n_perms:
        raise ValueError(
            f"plan {plan_id!r} has {n} sections — cannot form {n_perms} distinct orderings"
        )
    identity = list(range(n))
    orders: list[list[int]] = [identity]
    rng = random.Random(plan_seed(plan_id))
    guard = 0
    while len(orders) < n_perms:
        guard += 1
        if guard > 100_000:  # unreachable for n>=3, n_perms<=6 — defensive only
            raise RuntimeError(f"could not find {n_perms} distinct orderings for {plan_id!r}")
        candidate = list(range(n))
        rng.shuffle(candidate)
        if candidate not in orders:
            orders.append(candidate)
    out: list[dict[str, Any]] = []
    for idx, order in enumerate(orders):
        text = head + "".join(blocks[i] for i in order)
        out.append({"permutation_index": idx, "section_order": order, "text": text})
    return out


# ── Agreement math ────────────────────────────────────────────────────────────────────
def _validate_ratings(ratings: list[list[Any]]) -> int:
    """Validate a subjects×raters ratings table and return the (uniform) rater count ``n``.

    Guards: at least one subject, every subject rated by the SAME number of raters, and
    ``n >= 2`` (agreement is undefined for a single rater). Raises ``ValueError`` otherwise.
    """
    if not ratings:
        raise ValueError("no subjects to score")
    n = len(ratings[0])
    if any(len(r) != n for r in ratings):
        raise ValueError("every subject must be rated by the same number of raters")
    if n < 2:
        raise ValueError("Fleiss' kappa is undefined with fewer than 2 raters per subject")
    return n


def raw_agreement(ratings: list[list[Any]]) -> float:
    """Fraction of subjects on which ALL raters assigned the identical category."""
    _validate_ratings(ratings)
    return sum(1 for r in ratings if len(set(r)) == 1) / len(ratings)


def fleiss_kappa(ratings: list[list[Any]]) -> float:
    """Fleiss' κ over a subjects×raters ratings table (categorical labels).

    ``ratings[i]`` is the list of category labels the ``n`` interchangeable raters assigned to
    subject ``i`` (all subjects share the same ``n``). The category set is the union of labels
    observed across the table. Returns κ = (P̄ − Pₑ) / (1 − Pₑ). When every rating falls in a
    single category (Pₑ = 1, agreement trivially perfect) the chance-corrected form is 0/0;
    by convention this returns ``1.0``.
    """
    n = _validate_ratings(ratings)
    subjects = len(ratings)
    categories = sorted({label for row in ratings for label in row}, key=str)
    col_totals = dict.fromkeys(categories, 0)
    p_bar_sum = 0.0
    for row in ratings:
        counts = dict.fromkeys(categories, 0)
        for label in row:
            counts[label] += 1
        p_bar_sum += (sum(v * v for v in counts.values()) - n) / (n * (n - 1))
        for cat in categories:
            col_totals[cat] += counts[cat]
    p_bar = p_bar_sum / subjects
    total = subjects * n
    p_e = sum((col_totals[cat] / total) ** 2 for cat in categories)
    denom = 1.0 - p_e
    if denom <= 1e-12:  # all ratings in one category ⇒ perfect agreement, no chance variance
        return 1.0
    return (p_bar - p_e) / denom


def passes_floor(
    kappa: float, raw: float, *, kappa_floor: float = KAPPA_FLOOR, raw_floor: float = RAW_FLOOR
) -> bool:
    """The pre-registered gate rule: PASS iff κ ≥ floor AND raw agreement ≥ floor."""
    return kappa >= kappa_floor and raw >= raw_floor


def compute_agreement(ratings: list[list[Any]]) -> dict[str, Any]:
    """Bundle the agreement figures for a subjects×raters table into a summary dict:
    ``fleiss_kappa``, ``raw_agreement``, ``pass`` (against the floors), the floors, the
    subject/rater counts, and the per-category assignment counts."""
    # Round FIRST, then decide `pass` from the rounded values that are published in the dict, so a
    # downstream re-check of ``pass == (fleiss_kappa >= 0.6 and raw_agreement >= 0.8)`` against the
    # persisted figures can never disagree at a rounding boundary (the AC proving commands do this).
    kappa = round(fleiss_kappa(ratings), 4)
    raw = round(raw_agreement(ratings), 4)
    category_counts: dict[str, int] = {}
    for row in ratings:
        for label in row:
            category_counts[label] = category_counts.get(label, 0) + 1
    return {
        "fleiss_kappa": kappa,
        "raw_agreement": raw,
        "pass": passes_floor(kappa, raw),
        "kappa_floor": KAPPA_FLOOR,
        "raw_floor": RAW_FLOOR,
        "n_subjects": len(ratings),
        "n_raters": len(ratings[0]),
        "category_counts": dict(sorted(category_counts.items())),
    }


def jaccard(a: set[Any], b: set[Any]) -> float:
    """Jaccard similarity |a∩b| / |a∪b| (two empty sets are defined as fully similar → 1.0)."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def modal(labels: list[Any]) -> Any:
    """The most frequent label (ties broken by first appearance)."""
    counts: dict[Any, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return max(labels, key=lambda x: (counts[x], -labels.index(x)))


# ── Infra-INDETERMINATE exclusion + retry-cap policy ──────────────────────────────────
def is_infra_indeterminate_vote(decision: str | None) -> bool:
    """Exp A per-vote infra test. ``three_pass.pass3_decide`` returns ``decision ==
    "indeterminate"`` ONLY when Pass-2 returned no verdict (``verify is None`` — an error or an
    agentic-no-verdict result). That is an execution failure, not a stable judge decision, so
    the vote is dropped-and-re-run. Any substantive decision (block/advisory/dropped) is kept.
    """
    return decision == "indeterminate"


def is_infra_indeterminate_verdict(verdict: str | None, coverage: dict[str, Any] | None) -> bool:
    """Exp B per-permutation infra test. A gate ``verdict == "INDETERMINATE"`` is an EXECUTION
    failure (drop-and-re-run) only when its ``coverage`` carries ``llm_unavailable`` (systemic
    LLM-tier failure) or ``verify_failed`` (Pass-2 could not run — bug 59bc). A genuine
    judge-INDETERMINATE (INDETERMINATE with neither flag) is KEPT as its own agreement category.
    """
    if verdict != "INDETERMINATE":
        return False
    cov = coverage or {}
    return bool(cov.get("llm_unavailable") or cov.get("verify_failed"))


def finalize_votes(
    raw_attempts: list[Any], *, is_infra, target: int = VOTE_TARGET
) -> dict[str, Any]:
    """Apply the retry-cap/exclusion policy to a subject's ordered ``raw_attempts``.

    ``is_infra`` classifies an attempt as an infra failure (dropped) vs. a substantive vote
    (kept). The first ``target`` substantive votes are the subject's ratings. When fewer than
    ``target`` substantive votes were obtained the subject is EXCLUDED (never padded), and the
    caller records it as an explicit excluded row. Returns
    ``{"votes", "excluded", "n_attempts", "n_infra", "n_substantive"}``.
    """
    substantive = [a for a in raw_attempts if not is_infra(a)]
    votes = substantive[:target]
    return {
        "votes": votes,
        "excluded": len(substantive) < target,
        "n_attempts": len(raw_attempts),
        "n_infra": len(raw_attempts) - len(substantive),
        "n_substantive": len(substantive),
    }
