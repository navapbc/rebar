"""Plan-time project DET-invariant checks (story 7f0d) — the dynamic second phase of the
deterministic floor.

The static floor (P1–P9, :mod:`.det_floor`) is a frozen, built-in, polyglot readiness floor.
This module adds the OPEN, project-supplied half: an activated ``exec: "DET"`` criterion from the
``.rebar/criteria_routing.json`` overlay (epic 3156 / ADR 0015) is a pattern-rule invariant — its
"rubric" is a grounding **detector** (Engine B), not an LLM prompt. At plan time we run that
detector against the CURRENT code and, because a plan has no diff, SCOPE any match to the files
the ticket declares it will touch (``file_impact``):

* a detector MATCH on a **declared** file → a project-invariant defect the plan will (re)introduce
  → a ``fail`` DetResult, blocking per the criterion's ``default_posture``;
* a MATCH on a file the ticket does NOT declare (or no ``file_impact`` at all) → we cannot tie it
  to this plan, so it is ADVISORY (``fail``, non-blocking) — a coaching nudge, never a block;
* a detector ABSTAIN (tool unavailable / unsupported stack) → an ``abstain`` DetResult, blocking
  only when the criterion is ``fail_mode: "closed"`` (mirrors the code-review consumer);
* no signal → ``pass``.

Every check is FAIL-OPEN: :func:`run_project_det_checks` wraps each criterion in a try/except so a
raising check becomes an ``abstain`` (logged), never an exception that aborts the floor. A repo
with no activated ``exec: "DET"`` project criterion contributes ZERO results (the static floor is
byte-identical to before).
"""

from __future__ import annotations

import logging
from typing import Any

from .det_floor import DetResult, PlanContext

logger = logging.getLogger(__name__)


def _file_impact_paths(ctx: PlanContext) -> set[str]:
    """The set of paths the ticket declares it will touch (``state.file_impact`` — a list of
    ``{path, reason}`` dicts, or bare strings). Empty when none declared."""
    out: set[str] = set()
    for fi in ctx.state.get("file_impact") or []:
        p = fi.get("path") if isinstance(fi, dict) else fi
        if isinstance(p, str) and p:
            out.add(p)
    return out


def _det_project_criteria(repo_root: str | None) -> list[tuple[str, dict[str, Any]]]:
    """The ACTIVATED ``exec: "DET"`` project criteria for a repo as ``(cid, routing_entry)`` — the
    intersection of :func:`registry.effective_criteria` (activation) and the ``exec=="DET"`` routing
    entries. Empty (and fail-soft) when there is no overlay / no activated DET criterion."""
    from . import registry

    try:
        routing = registry.effective_routing(repo_root)
        active = registry.effective_criteria(repo_root)
    except Exception:  # noqa: BLE001 — a malformed overlay must not abort the floor; log + skip
        logger.warning("could not resolve project DET criteria; skipping", exc_info=True)
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for cid in active:
        entry = routing.get(cid) or {}
        if str(entry.get("exec", "")).upper() == "DET":
            out.append((cid, entry))
    return out


def _matching_detectors(selector: dict[str, Any] | None, repo_root: str | None):
    """The detector-registry slice a routing ``detector`` selector resolves to (exact ``id`` or
    ``id_prefix`` class). Returns a ``Registry`` (possibly empty) or ``None`` on load failure."""
    from rebar.grounding.detectors import Registry, load_registry

    from .registry import _detector_matches

    reg = load_registry(repo_root)
    selected = tuple(d for d in reg if _detector_matches(d.id, selector))
    return Registry(detectors=selected)


def _run_one(cid: str, entry: dict[str, Any], ctx: PlanContext) -> DetResult:
    """Run ONE project DET criterion: scan its detector against the code, scope matches to the
    ticket's ``file_impact``, and reduce to a :class:`DetResult` per the match/advisory/abstain
    rules documented at module top. Never raises to the caller in the expected paths (a genuinely
    unexpected error is caught + re-raised into an abstain by :func:`run_project_det_checks`)."""
    from rebar.grounding import engine_b

    name = str(entry.get("name", cid))
    posture = str(entry.get("default_posture", "advisory")).lower()
    fail_mode = str(entry.get("fail_mode", "open")).lower()
    selector = entry.get("detector")
    base_cov = {"ran": True, "criterion": cid, "detector": selector, "fail_mode": fail_mode}

    if not selector:
        # A DET criterion with no detector selector can establish no coverage → abstain.
        return DetResult(
            cid,
            name,
            "abstain",
            blocking=(fail_mode == "closed"),
            coverage={**base_cov, "ran": False, "reason": "no_detector_selector"},
        )

    reg_slice = _matching_detectors(selector, ctx.repo_root)
    if reg_slice is None or not reg_slice.detectors:
        return DetResult(
            cid,
            name,
            "abstain",
            blocking=(fail_mode == "closed"),
            coverage={**base_cov, "ran": False, "reason": "no_matching_detector"},
        )

    repo_root = ctx.repo_root or "."
    result = engine_b.scan(repo_root, registry=reg_slice)
    declared = _file_impact_paths(ctx)
    scoped: list[dict[str, Any]] = []
    unscoped = 0
    abstains: list[dict[str, Any]] = []
    for rec in result.records:
        outcome = rec.get("outcome")
        if outcome == "abstain":
            abstains.append(rec)
        elif outcome == "match":
            loc = (rec.get("location") or {}).get("file")
            if loc and loc in declared:
                scoped.append(rec)
            else:
                unscoped += 1
    cov = {
        **base_cov,
        "scoped_matches": len(scoped),
        "unscoped_matches": unscoped,
        "abstains": len(abstains),
        "declared_files": sorted(declared),
    }

    if scoped:
        # A detector match on a file the ticket declares it will touch — the plan will
        # (re)introduce the invariant violation. Blocking per the criterion's posture.
        return DetResult(
            cid,
            name,
            "fail",
            blocking=(posture == "blocking"),
            finding={
                "finding": _detector_message(reg_slice) or f"Project invariant {name!r} violated.",
                "evidence": [f"{r['location']['file']}" for r in scoped[:10] if r.get("location")],
                "impact": (
                    "A declared file already violates this project invariant; the plan will carry "
                    "the violation forward unless it is remediated."
                ),
                "suggested_fix": (
                    "Fix the flagged file(s) as part of this work, or narrow the file_impact / "
                    "invariant so the two are consistent."
                ),
                "criteria": [cid],
                "criterion_name": name,
                "tier": "DET",
            },
            coverage=cov,
        )
    if unscoped:
        # A match that we cannot tie to THIS plan (undeclared file / no file_impact) — advisory.
        return DetResult(
            cid,
            name,
            "fail",
            blocking=False,
            finding={
                "finding": (
                    f"Project invariant {name!r} is violated elsewhere in the repo "
                    "(not in a declared file)."
                ),
                "evidence": [f"{unscoped} match(es) outside the ticket's file_impact"],
                "impact": (
                    "The violation is pre-existing/undeclared, so it cannot be attributed to this "
                    "plan — surfaced as coaching, not a block."
                ),
                "suggested_fix": (
                    "If this ticket should fix it, declare the file in file_impact; otherwise "
                    "track it as separate work."
                ),
                "criteria": [cid],
                "criterion_name": name,
                "tier": "DET",
            },
            coverage=cov,
        )
    if abstains:
        return DetResult(
            cid,
            name,
            "abstain",
            blocking=(fail_mode == "closed"),
            coverage={**cov, "reason": "detector_abstained"},
        )
    return DetResult(cid, name, "pass", coverage=cov)


def _detector_message(reg_slice: Any) -> str | None:
    """The first non-empty rule ``message`` across a detector-registry slice (the human-readable
    finding text), or ``None``."""
    for det in reg_slice:
        msg = (getattr(det, "rule", None) or {}).get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return None


def run_project_det_checks(ctx: PlanContext) -> list[DetResult]:
    """Run every ACTIVATED ``exec: "DET"`` project criterion for the ticket's repo, fail-open per
    criterion (a raising check → an ``abstain`` DetResult, logged). Returns ``[]`` when there is no
    overlay / no activated DET project criterion — so a repo without one adds zero results and the
    static floor stays byte-identical."""
    results: list[DetResult] = []
    for cid, entry in _det_project_criteria(ctx.repo_root):
        try:
            results.append(_run_one(cid, entry, ctx))
        except Exception as exc:  # noqa: BLE001 — fail-open: a broken project check abstains, logged with traceback
            logger.warning("project DET check %s raised; abstaining", cid, exc_info=True)
            results.append(
                DetResult(
                    cid,
                    str(entry.get("name", cid)),
                    "abstain",
                    coverage={"ran": False, "reason": f"error:{exc}"},
                )
            )
    return results
