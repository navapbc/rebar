"""Pass-3 advisory-finding floors + blocking fix-unit grouping for the plan-review gate
(ticket 02b7 moved these five functions out of ``rebar.llm.plan_review`` to clear the
800-line module-size cap; see that module's docstring for the gate this is part of).

These are pure relocation targets — no logic changed. They are re-imported into
``rebar.llm.plan_review`` (see that module's re-export block) so every existing
caller and every ``monkeypatch.setattr(plan_review, "...")`` target keeps working
unchanged; this module is not meant to be imported directly by consumers.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

from . import sidecar

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
    novelty per index is injected. A no-drop run leaves the verdict byte-identical."""
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
    delivered-now id set are injected. A no-drop run leaves the verdict byte-identical."""
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


def _group_blocking_fix_units(verdict: dict[str, Any]) -> None:
    """Blocking fix-unit grouping (story 5e64): one plan defect co-cited by N criteria mints N
    blocking findings (observed x10, ticket a879). STAMP-ONLY — no finding ever leaves
    ``verdict["blocking"]`` (the sidecar's ``build_payload`` concatenates the existing buckets, so
    moving findings would silently drop them): every finding in a multi-member group gains
    ``group_id`` (the criteria-free :func:`sidecar.fix_unit_key`) and ``is_primary``; the primary
    (highest priority; ties: alphabetically-first sorted criteria entry, then lowest id; missing
    priority sorts as 0.0) also gains ``group_criteria`` (the group's criteria union). Only the
    CLI renderer collapses a group to its primary — library/MCP consumers see all findings."""
    blocking = verdict.get("blocking") or []
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in blocking:
        groups.setdefault(sidecar.fix_unit_key(f), []).append(f)
    for key, members in groups.items():
        if len(members) < 2:
            continue

        def _rank(f: dict[str, Any]) -> tuple[float, str, str]:
            crits = sorted(f.get("criteria") or [])
            return (-(f.get("priority") or 0.0), crits[0] if crits else "", str(f.get("id") or ""))

        members.sort(key=_rank)
        union = sorted({c for f in members for c in (f.get("criteria") or [])})
        for i, f in enumerate(members):
            f["group_id"] = key
            f["is_primary"] = i == 0
        members[0]["group_criteria"] = union
