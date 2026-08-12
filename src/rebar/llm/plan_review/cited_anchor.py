"""Warn-only cited-anchor pre-check before a re-review (task ccba-3b6a-4241-4f59).

The gap this closes sits BETWEEN the two shipped reuse short-circuits in :mod:`.reuse`
and a full billable re-review. ``verdict_reuse`` already answers "nothing the review
reads has changed" with zero LLM calls, but it demands the WHOLE material fingerprint
and the review code SHA both hold still. Touch anything else — add a ``file_impact``
row, land an unrelated commit — and the reuse path correctly declines, so the ticket
pays a full multi-pass review (~$3-7) even though the *plan prose the blocking findings
actually quoted* was never edited. The historical audit on b902-18af-14ed-4da1 measured
that shape at 13-14% of 894 BLOCK -> re-review transitions: re-blocked on the SAME
persisting criteria, having learned nothing.

This module is the deterministic (zero-LLM, local-read-only) heads-up for that case. It
compares the prior BLOCK's cited evidence quotes against the plan text now and, when the
description component is provably untouched AND at least one cited anchor is still
present verbatim, logs a WARNING naming the persisting findings and their criteria.

**WARN-ONLY is the whole contract.** It never changes a verdict, never declines or skips
the review, never adds an LLM call, and never moves an exit code. The review runs
immediately afterwards exactly as it would have. That posture is deliberate and is the
reason a hard block was rejected by the operator (2026-08-11): roughly 8% of BLOCK -> PASS
flips carry no material change at all (verdict flap), so a decline would freeze those
recoveries. It mirrors the escape-hatch posture of the shipped BLOCK verdict reuse.

Every plan review records :func:`record_metrics`' ``metrics.precheck.cited_anchor_warning``
on the sidecar so the warning's hit rate and predictive value are measurable offline
(ADR 0054) BEFORE anyone considers promoting it to something stronger.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["MIN_ANCHOR_CHARS", "precheck", "record_metrics"]

# The component of the material basis whose fingerprint gates the warning. The findings'
# quotes are drawn from the plan prose, so the description is the only component whose
# stillness makes "the author did not touch the cited text" a sound inference.
_PLAN_COMPONENT = "description"

# Shortest normalized anchor treated as evidence of a specific, untouched claim. A finder
# quote like "the plan" or "AC 1" would collide with almost any revision and turn the
# warning into noise; anything this short is dropped as unmatchable rather than matched.
MIN_ANCHOR_CHARS = 24


def _normalize(text: str) -> str:
    """Collapse runs of whitespace so a requoted anchor still matches.

    The description fingerprint is already required to be unchanged before any anchor is
    consulted, so the plan text itself is byte-identical; this only absorbs the finder's
    own reflowing of a quote (a wrapped line, a doubled space) which would otherwise make
    a genuinely untouched citation look unmatchable.
    """
    return " ".join(str(text or "").split())


def _anchor_texts(finding: dict[str, Any]) -> list[str]:
    """The citable quote strings on one finding's ``evidence`` grounding prose.

    ``evidence`` is persisted losslessly on the v2 sidecar (story 4e19). Its items are
    normally plain strings, but a mapping carrying a ``quote``/``text``/``evidence`` key is
    accepted too so a future finder shape degrades to "no anchors" instead of raising —
    this whole module is advisory and must never break a review.
    """
    out: list[str] = []
    for item in finding.get("evidence") or []:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, dict):
            raw = item.get("quote") or item.get("text") or item.get("evidence") or ""
        else:
            continue
        anchor = _normalize(raw)
        if len(anchor) >= MIN_ANCHOR_CHARS:
            out.append(anchor)
    return out


def _result(warning: bool, matched: int = 0, findings: list[dict[str, Any]] | None = None):
    return {
        "cited_anchor_warning": warning,
        "matched_anchors": matched,
        "findings": findings or [],
    }


def precheck(ticket_id: str, ctx, *, repo_root) -> dict[str, Any]:
    """Deterministically decide whether a re-review looks doomed, and WARN if so.

    Returns the metrics record (never ``None``, never raises): ``cited_anchor_warning``
    plus the matched-anchor count and the persisting findings' ids/criteria. The caller
    proceeds into the review unconditionally on either answer.

    The warning fires only when ALL of these hold:

    1. the latest usable sidecar verdict is ``BLOCK`` with at least one blocking finding;
    2. the ``description`` component fingerprint recorded at that review equals the
       description component fingerprint of the plan NOW (both sides read from the same
       ``material_parts`` rule) — so the plan prose is provably untouched; and
    3. at least one blocking finding cites an anchor still present verbatim in that text.

    Condition 3 is what keeps the check honest. Paraphrased evidence and structural
    findings that quote nothing yield no anchors, and per the approved scope an
    unmatchable anchor NEVER triggers the warning on its own — silence is the correct
    answer when the check cannot see what the finding was pointing at. Condition 2 is
    also what lets condition 3 reason about the OLD text using only the CURRENT text:
    the sidecar stores the description's fingerprint, not its bytes, so matching "the
    text the finding quoted" is sound precisely because that text has not moved.

    Fail-safe throughout: any read/parse failure returns a no-warning record.
    """
    from . import sidecar
    from .material_diff import material_components

    try:
        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        if not prior or prior.get("verdict") != "BLOCK":
            return _result(False)
        blocking = [f for f in prior.get("findings") or [] if f.get("decision") == "block"]
        if not blocking:
            return _result(False)

        recorded = (prior.get("material_parts") or {}).get(_PLAN_COMPONENT)
        if not recorded:
            return _result(False)  # pre-94a3 sidecar: components were never recorded
        current = material_components(ctx).get(_PLAN_COMPONENT)
        if not current or str(recorded[0]) != str(current[0]):
            return _result(False)  # the plan prose was revised -> the review may learn something

        haystack = _normalize(getattr(ctx, "description", ""))
        matched = 0
        persisting: list[dict[str, Any]] = []
        for finding in blocking:
            hits = [a for a in _anchor_texts(finding) if a in haystack]
            if hits:
                matched += len(hits)
                persisting.append(
                    {"id": finding.get("id"), "criteria": list(finding.get("criteria") or [])}
                )
        if not persisting:
            return _result(False)  # nothing verbatim-matchable -> never warn on its own
    except Exception:
        # Advisory only: a broken read must never fail a review.
        logger.debug("cited-anchor pre-check failed; continuing to the review", exc_info=True)
        return _result(False)

    logger.warning(
        "cited-anchor pre-check: %s outstanding blocking finding(s) from the previous BLOCK "
        "cite plan text this revision did not touch (%s) — the description is unchanged and "
        "%d cited anchor(s) are still present verbatim, so this review will likely re-block "
        "on the same criteria. Running the review anyway; address the findings above first "
        "to avoid paying for a re-review that learns nothing.",
        len(persisting),
        ", ".join(
            "{} [{}]".format(f["id"], ", ".join(f["criteria"]) or "no criteria recorded")
            for f in persisting
        ),
        matched,
    )
    return _result(True, matched, persisting)


def record_metrics(verdict: dict[str, Any], result: dict[str, Any] | None) -> None:
    """Stamp ``coverage.metrics.precheck.cited_anchor_warning`` on ``verdict`` in place.

    The sidecar lifts ``coverage.metrics`` verbatim into its own ``metrics`` block, so this
    is what makes the flag land at ``metrics.precheck.cited_anchor_warning`` for offline
    analysis. It is written on EVERY review — ``False`` is a measurement, not an absence,
    and only a recorded ``False`` makes the warning's precision computable. Best-effort:
    a verdict carrying non-dict coverage/metrics is left untouched rather than corrupted.
    """
    coverage = verdict.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
        verdict["coverage"] = coverage
    metrics = coverage.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
        coverage["metrics"] = metrics
    precheck_block = metrics.get("precheck")
    if not isinstance(precheck_block, dict):
        precheck_block = {}
        metrics["precheck"] = precheck_block
    precheck_block["cited_anchor_warning"] = bool((result or {}).get("cited_anchor_warning"))
