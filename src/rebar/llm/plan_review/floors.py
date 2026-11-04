"""Pass-3 advisory-finding floors + blocking fix-unit grouping for the plan-review gate
(ticket 02b7 moved these five functions out of ``rebar.llm.plan_review`` to clear the
800-line module-size cap; see that module's docstring for the gate this is part of).

They are re-imported into ``rebar.llm.plan_review`` (see that module's re-export block) so
every existing caller and every ``monkeypatch.setattr(plan_review, "...")`` target keeps
working; this module is not meant to be imported directly by consumers. The blocking
fix-unit grouping has since moved from a lexical key to one set-level grouping sub-call
(bug longish-ripe-snowyowl).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

logger = logging.getLogger(__name__)


def _apply_floor_to_verdict(
    verdict: dict[str, Any], novelty_map: dict[int, float], *, t_novel: float, floor: float
) -> None:
    """Apply the Pass-3 rising floor (child cc5b) IN PLACE on the verdict's surfaced advisory
    findings: a finding at position ``i`` is DROPPED iff ``decide.rising_floor_drop`` (novel +
    low-priority). Dropped findings move from ``advisory`` into the verdict's ``dropped`` bucket
    (the sidecar persists it with ``norm_id``), and the coverage records ``narrowed``/
    ``floored_criteria``/``floored_finding_ids`` AND its ``counts`` are corrected (advisory_surfaced
    down, dropped up) so the post-floor counts stay consistent with the buckets. Pure (no LLM); the
    novelty per index is injected. A no-drop run leaves the verdict byte-identical.

    Unlike ``drift_floor``/``xcheck``'s drop appliers, this never calls
    ``decide._recompute_verdict_after_drop``: it only ever moves findings between
    ``advisory`` and ``dropped``, never touches ``verdict["blocking"]``, so the overall
    PASS/BLOCK decision cannot change here."""
    from rebar.llm.review_kernel import decide

    advisory = verdict.get("advisory") or []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(advisory):
        nov = novelty_map.get(i, 0.0)
        prio = f.get("priority") or 0.0
        if decide.rising_floor_drop(prio, nov, t_novel=t_novel, floor=floor):
            dropped.append({**f, "_floored": True, "novelty": nov, "drop_reason": "novelty"})
        else:
            kept.append(f)
    if not dropped:
        return
    verdict["advisory"] = kept
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["floored_criteria"] = sorted({c for f in dropped for c in (f.get("criteria") or [])})
    cov["floored_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):  # keep the baked counts consistent with the post-floor buckets
        counts["advisory_surfaced"] = len(kept)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)


def _score_floor_novelty(
    advisory: list[dict[str, Any]],
    prior_findings: list[dict[str, Any]],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> dict[int, float]:
    """Run the 150b novelty sub-call over the surfaced advisory findings (the droppable surface)
    against the prior findings, returning ``{advisory_index: novelty}``. Fail-safe: any error →
    ``{}`` (no drops). The droppable surface is bounded by the advisory cap, so a generous single
    window + a coarse char/4 estimator keep it to one sub-call."""
    from rebar.llm.review_kernel.verify import score_novelty
    from rebar.llm.runner import RunRequest, get_runner

    try:
        from dataclasses import replace

        from rebar.llm.plan_review import _verifier_cfg

        runner_sel = runner or get_runner(cfg)
        # Greedy sampling for this SEPARATE Pass-2 novelty sub-call (hand-built RunRequest, so it
        # bypasses the workflow `with: temperature` seam): a re-run must not resample the
        # carryover-vs-novel judgement (upstream review-code report §2).
        vcfg = replace(_verifier_cfg(cfg), temperature=0.0)
        from . import passes

        system = passes._resolve_system(passes.PASS_NOVELTY, ctx.plan_text, vcfg)

        def run_chunk(instructions: str, context: str) -> list[dict[str, Any]]:
            req = RunRequest.for_structured(
                system_prompt=system,
                instructions=f"{instructions}\n\n## Prior-review findings (context)\n{context}",
                config=vcfg,
                reviewers=["plan-novelty"],
                output_schema="plan_review_novelty",
                bounds=RunRequest.INHERIT_POLICY,
            )
            return runner_sel.run(req).get("novelties", []) or []

        return score_novelty(
            advisory,
            prior_findings=prior_findings,
            run_chunk=run_chunk,
            window_tokens=100_000,
            est_tokens=lambda s: len(s) // 4 + 1,
        )
    except Exception:
        logger.warning("rising-floor novelty scoring failed; running un-floored", exc_info=True)
        return {}


def _apply_completion_floor_to_verdict(
    verdict: dict[str, Any],
    completion_map: dict[int, dict[str, Any]],
    *,
    floor: float,
    preserve: frozenset[str],
    delivered_ids: frozenset[str],
) -> None:
    """Apply the Pass-3 COMPLETION floor (story 6533) IN PLACE on the surfaced advisory findings:
    a finding at position ``i`` is DROPPED iff
    :func:`completion_subcall.completion_floor_drop` (attribution in
    ``delivered_ids`` + limited-to-closed + plan-semantics + priority < floor + not-preserved).
    Dropped findings move from ``advisory`` into the verdict's ``dropped`` bucket carrying
    ``drop_reason="completion"`` (+ the finding's ``completion`` answers for the sidecar join), and
    the coverage records the completion-specific ``completion_floored_criteria`` /
    ``completion_floored_finding_ids`` (namespaced so they never collide with the novelty floor's
    keys) AND corrects its ``counts``. Pure (no LLM); the completion answers per index + the
    delivered-now id set are injected. A no-drop run leaves the verdict byte-identical.

    Same ``_recompute_verdict_after_drop``-free shape as :func:`_apply_floor_to_verdict` above,
    for the same reason: only ``advisory``/``dropped`` are touched here, never ``blocking``."""
    from . import completion_subcall

    advisory = verdict.get("advisory") or []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(advisory):
        ans = completion_map.get(i)
        if ans and completion_subcall.completion_floor_drop(
            ans,
            f.get("priority") or 0.0,
            f.get("criteria") or [],
            floor=floor,
            preserve=preserve,
            delivered_ids=delivered_ids,
        ):
            dropped.append({**f, "_floored": True, "drop_reason": "completion", "completion": ans})
        else:
            kept.append(f)
    if not dropped:
        return
    verdict["advisory"] = kept
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["completion_floored_criteria"] = sorted(
        {c for f in dropped for c in (f.get("criteria") or [])}
    )
    cov["completion_floored_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):  # keep the baked counts consistent with the post-floor buckets
        counts["advisory_surfaced"] = len(kept)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)


def _classify_completion(
    advisory: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
) -> dict[int, dict[str, Any]]:
    """Run the Pass-2 completion sub-call over the surfaced advisory findings against the given
    delivered-children ``manifest``, returning ``{advisory_index: {attribution, containment,
    layer}}``. Fail-safe: any error → ``{}`` (no drops). The sub-call itself also degrades to ``{}``
    on error, so this is defense-in-depth."""
    from rebar.llm.runner import get_runner

    from . import completion_subcall

    try:
        from rebar.llm.plan_review import _verifier_cfg

        runner_sel = runner or get_runner(cfg)
        return completion_subcall.pass2_completion(
            runner_sel,
            _verifier_cfg(cfg),
            plan=ctx.plan_text,
            findings=advisory,
            delivered_manifest=manifest,
        )
    except Exception:
        logger.warning("completion floor classification failed; running un-floored", exc_info=True)
        return {}


# ── Blocking fix-unit grouping (bug longish-ripe-snowyowl, superseding the lexical 5e64 key) ────
# One set-level LLM call over a verdict's BLOCKING findings returns the groups that state the same
# defect (the bakeoff's step-B shape: no pairwise votes, no transitive chaining, a finding in at
# most one group). STAMP-ONLY and FAIL-OPEN: any error or invalid output stamps nothing.
def _fix_unit_groups_model() -> type:
    """The grouping structured-output model: groups of finding ids that state one defect.
    Fields are permissive so a malformed answer reaches :func:`_valid_fix_unit_groups`, which
    rejects the whole answer, instead of failing inside the runner."""
    from pydantic import BaseModel, Field

    class FixUnitGroup(BaseModel):
        defect: str = Field(default="", description="One sentence naming the shared defect.")
        members: list[str] = Field(
            default_factory=list,
            description="The ids (exactly as listed) of the findings that state this defect.",
        )

    class FixUnitGroupsOutput(BaseModel):
        groups: list[FixUnitGroup] = Field(default_factory=list)

    return FixUnitGroupsOutput


def register_contracts() -> None:
    """Register the grouping contract (idempotent) before any grouping sub-call runs."""
    from rebar.llm import contracts

    contracts.register_contract("plan_review_fix_unit_groups", _fix_unit_groups_model)


register_contracts()


def _fix_unit_listing(blocking: list[dict[str, Any]]) -> str:
    """The id-keyed listing of the blocking findings the grouping call sees."""
    lines: list[str] = []
    for f in blocking:
        crits = ", ".join(f.get("criteria") or []) or "-"
        loc = " ".join(str(f.get("location") or "").split()) or "-"
        text = " ".join(str(f.get("finding") or "").split())
        lines.append(f"- id={f.get('id')} [{crits}] at {loc} :: {text}")
    return "\n".join(lines)


def _request_fix_unit_groups(
    blocking: list[dict[str, Any]], *, ctx, cfg: LLMConfig, runner: Runner | None
) -> dict[str, Any] | None:
    """Run the ONE grouping sub-call; the raw structured result, or None when it raised.
    Standard verifier class, greedy sampling so a re-review re-groups the same way."""
    from dataclasses import replace

    from rebar.llm.runner import RunRequest, get_runner

    from . import passes

    try:
        from rebar.llm.plan_review import _verifier_cfg  # lazy: avoid package cycle

        runner_sel = runner or get_runner(cfg)
        vcfg = replace(_verifier_cfg(cfg), temperature=0.0)
        req = RunRequest.for_structured(
            system_prompt=passes._resolve_system(
                passes.PASS_FIX_UNIT_GROUPING, ctx.plan_text, vcfg
            ),
            instructions=(
                f"## Blocking findings (by id)\n{_fix_unit_listing(blocking)}\n\n"
                "Emit one group per defect that two or more of these findings state."
            ),
            config=vcfg,
            reviewers=["plan-fix-unit-grouping"],
            output_schema="plan_review_fix_unit_groups",
            bounds=RunRequest.INHERIT_POLICY,
        )
        raw = runner_sel.run(req)
    except Exception:
        logger.warning("fix-unit grouping sub-call failed; no groups stamped", exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _valid_fix_unit_groups(raw: dict[str, Any], known: set[str]) -> list[list[str]] | None:
    """The validated member-id groups (each >= 2 distinct ids), or None when the answer must be
    rejected whole: ``groups`` not a list, a malformed group, an id not in ``known``, or one id
    in two groups. Single-member groups carry no grouping and are ignored."""
    groups = raw.get("groups")
    if not isinstance(groups, list):
        return None
    seen: set[str] = set()
    out: list[list[str]] = []
    for g in groups:
        members = g.get("members") if isinstance(g, dict) else None
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            return None
        ids = list(dict.fromkeys(members))
        if not set(ids) <= known or seen & set(ids):
            return None
        seen.update(ids)
        if len(ids) >= 2:
            out.append(ids)
    return out


def _stamp_fix_unit(members: list[dict[str, Any]]) -> None:
    """Stamp one group: every member gains ``group_id`` + ``is_primary``; the primary (highest
    priority; ties: alphabetically-first sorted criteria entry, then lowest id; missing priority
    sorts as 0.0) also gains ``group_criteria`` (the criteria union)."""

    def _rank(f: dict[str, Any]) -> tuple[float, str, str]:
        crits = sorted(f.get("criteria") or [])
        return (-(f.get("priority") or 0.0), crits[0] if crits else "", str(f.get("id") or ""))

    members = sorted(members, key=_rank)
    basis = "\n".join(sorted(str(f.get("id")) for f in members))
    key = "g" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    union = sorted({c for f in members for c in (f.get("criteria") or [])})
    for i, f in enumerate(members):
        f["group_id"] = key
        f["is_primary"] = i == 0
    members[0]["group_criteria"] = union


def _group_blocking_fix_units(
    verdict: dict[str, Any], *, ctx, cfg: LLMConfig, runner: Runner | None = None
) -> None:
    """Group a verdict's BLOCKING findings that state one defect (one defect co-cited by N
    criteria mints N blocking findings; bug longish-ripe-snowyowl). Runs ONE grouping sub-call
    only when there are >= 2 blocking findings with unique ids. STAMP-ONLY — no finding leaves
    ``verdict["blocking"]``, no decision, bucket, count or verdict string changes; only the CLI
    renderer collapses a group to its primary. FAIL-OPEN: a raising call or an invalid answer
    (malformed, unknown id, overlapping groups) stamps nothing and is logged.
    ``coverage.fix_unit_grouping`` records the outcome and the call's usage."""
    blocking = verdict.get("blocking") or []
    if len(blocking) < 2:
        return
    ids = [str(f.get("id") or "") for f in blocking]
    if "" in ids or len(set(ids)) != len(ids):
        logger.warning("fix-unit grouping skipped: blocking finding ids are missing or repeated")
        return
    raw = _request_fix_unit_groups(blocking, ctx=ctx, cfg=cfg, runner=runner)
    groups = _valid_fix_unit_groups(raw, set(ids)) if raw is not None else None
    record: dict[str, Any] = {"status": "failed", "groups": 0, "usage": {}}
    if raw is not None:
        record["usage"] = raw.get("_usage") or {}
    if groups is None:
        if raw is not None:
            logger.warning("fix-unit grouping answer rejected (malformed, unknown id, or overlap)")
    else:
        by_id = dict(zip(ids, blocking, strict=True))
        for members in groups:
            _stamp_fix_unit([by_id[m] for m in members])
        record["status"] = "grouped" if groups else "no_groups"
        record["groups"] = len(groups)
    verdict.setdefault("coverage", {})["fix_unit_grouping"] = record
