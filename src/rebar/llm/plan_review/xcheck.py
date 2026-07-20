"""Validation-assessment cross-checks (bug 5e40) — two per-verdict CONSISTENCY drops that converge
a non-deterministic re-review, complementing the code-drift/novelty/completion FLOORS.

The plan-review pipeline's Pass-3 floors converge re-review on the CODE/COMPLETION axes. This
module adds the two VALIDATION-ASSESSMENT drops the 5e40 triage identified as missing (the review
pipeline's "deciding whether a candidate is real" stage):

* CONTRADICTION cross-check — findings WITHIN one verdict that mutually contradict (5e40 A1: a
  BLOCKING "no one is tasked with capturing the snapshot" alongside an ADVISORY "the parent
  explicitly assigns capture to S1"). One verdict cannot both assert a thing absent and present; the
  contradicted/weaker member is dropped.
* COMMENT-TRAIL consultation — a finding that re-litigates a point the ticket's RECORDED comment
  trail already resolved (5e40 B3: the ``rebase:chain`` endpoint, conceded in-trail after a prior
  round fact-checked it). It is dropped.

Both follow the drift-floor shape exactly: a PURE ``apply_*`` mutation that is deterministic given
an injected judgment (the unit-testable drop math), plus a gated ``maybe_apply_*`` that runs the LLM
detection sub-call (mirroring the novelty/completion sub-calls, fail-safe to no-drops) and the
deterministic drop predicates in :mod:`rebar.llm.review_kernel.decide`. Both are gated inert by
default behind an evidence-gate config flag (like the completion floor); when inert the verdict is
byte-identical. The verdict-string re-derivation reuses
``drift_floor._recompute_verdict_after_drop`` so a dropped BLOCK converges BLOCK→PASS by the SAME
rule as the other floors.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

logger = logging.getLogger(__name__)


# ── the two cross-check sub-call output contracts (bug 5e40). Both are plan-review-SPECIFIC
#    per-verdict consistency shapes (not generic kernel axes), so they live here with their
#    consumer (not in passes.py, which is at the module-size cap). Fields are permissive so ONE
#    malformed item reshapes to the fail-safe KEEP rather than failing the whole structured batch. ─
def _contradiction_model() -> type:
    """The CONTRADICTION structured-output model: a flat list of pairwise judgments, each naming two
    finding indices (``a``/``b``), whether they mutually ``contradiction`` each other, and which
    member (``drop``) is the FALSE/contradicted one (``-1`` ⇒ let the deterministic tiebreak
    decide). The deterministic drop math lives in :func:`decide.contradiction_drop_index`."""
    from pydantic import BaseModel, Field

    class ContradictionPair(BaseModel):
        a: int = Field(description="The 0-based combined index of the first finding in the pair.")
        b: int = Field(description="The 0-based combined index of the second finding in the pair.")
        contradiction: bool = Field(
            default=False, description="True IFF the two findings mutually contradict."
        )
        drop: int = Field(
            default=-1,
            description="The index (a or b) of the FALSE/contradicted member to drop; -1 to let "
            "the deterministic tiebreak decide.",
        )
        rationale: str = Field(default="", description="A short reason (≤1 sentence).")

    class ContradictionOutput(BaseModel):
        pairs: list[ContradictionPair] = Field(default_factory=list)

    return ContradictionOutput


def _comment_trail_model() -> type:
    """The COMMENT-TRAIL structured-output model: one assessment per finding (by ``index``) stating
    whether the ticket's recorded comment trail ALREADY resolved the point the finding re-raises
    (``resolved_in_trail`` ∈ {yes, no, insufficient}) and, if so, which comment settled it
    (``comment_ref``). The deterministic drop math lives in :func:`decide.comment_trail_drop`."""
    from pydantic import BaseModel, Field

    class TrailAssessment(BaseModel):
        index: int = Field(description="The 0-based combined index of the finding being assessed.")
        resolved_in_trail: str = Field(
            default="no",
            description="yes | no | insufficient — does the comment trail already RESOLVE/concede "
            "this exact point?",
        )
        comment_ref: str = Field(
            default="",
            description="A short reference to the comment that settled it (empty if no).",
        )

    class CommentTrailOutput(BaseModel):
        assessments: list[TrailAssessment] = Field(default_factory=list)

    return CommentTrailOutput


def register_contracts() -> None:
    """Register the two cross-check structured-output contracts (idempotent). Called at import so a
    real runner can build the output model before a ``maybe_apply_*`` sub-call runs."""
    from rebar.llm import contracts

    contracts.register_contract("plan_review_contradiction", _contradiction_model)
    contracts.register_contract("plan_review_comment_trail", _comment_trail_model)


register_contracts()


def _finding_listing(findings: list[dict[str, Any]]) -> str:
    """A compact, index-keyed listing of the verdict's findings for a cross-check sub-call. Uses the
    SAME 0-based combined index the drop math consumes, so the model's answers map back directly."""
    lines: list[str] = []
    for i, f in enumerate(findings):
        crits = ", ".join(f.get("criteria") or []) or "-"
        text = " ".join((f.get("finding") or "").split())
        lines.append(f"[{i}] ({crits}) {text}")
    return "\n".join(lines)


def _comment_trail_block(comments: list[dict[str, Any]]) -> str:
    """Render the ticket's recorded comment trail (in order) for the comment-trail sub-call."""
    lines: list[str] = []
    for c in comments:
        author = c.get("author") or "?"
        body = " ".join((c.get("body") or "").split())
        lines.append(f"- ({author}) {body}")
    return "\n".join(lines)


# ── CONTRADICTION cross-check (5e40 A1) ─────────────────────────────────────────────────────────
def apply_contradiction_drops(verdict: dict[str, Any], pairs: list[dict[str, Any]]) -> None:
    """Apply the intra-verdict CONTRADICTION drop (bug 5e40) IN PLACE over the verdict's surfaced
    BLOCKING + ADVISORY findings (indexed ``[*blocking, *advisory]``). For each judged-contradictory
    ``pair`` the deterministic :func:`decide.contradiction_drop_index` picks the member to DROP (the
    model-identified false one, else the lower-priority one); dropped findings move into ``dropped``
    with ``drop_reason="contradiction"`` and ``contradicts`` = the surviving counterpart's id.
    Coverage records ``narrowed``/``contradiction_xcheck``/``contradiction_dropped_finding_ids`` and
    ``counts`` are corrected; dropping a BLOCKING finding can flip BLOCK→PASS, so the verdict string
    is re-derived. Pure (no LLM); the judgment is injected. Empty ``pairs`` or no drop leaves the
    verdict byte-identical."""
    from rebar.llm.review_kernel import decide

    from . import drift_floor

    blocking = verdict.get("blocking") or []
    advisory = verdict.get("advisory") or []
    combined = [*blocking, *advisory]
    if not combined or not pairs:
        return
    priorities = [float(f.get("priority") or 0.0) for f in combined]
    drop_map: dict[int, Any] = {}  # combined index → surviving counterpart id
    for pair in pairs:
        idx = decide.contradiction_drop_index(pair, priorities)
        if idx is None or idx in drop_map:
            continue
        keep = pair["b"] if idx == pair["a"] else pair["a"]
        drop_map[idx] = combined[keep].get("id")
    if not drop_map:
        return
    n_block = len(blocking)
    kept_block: list[dict[str, Any]] = []
    kept_adv: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(combined):
        if i in drop_map:
            dropped.append(
                {**f, "_xchecked": True, "drop_reason": "contradiction", "contradicts": drop_map[i]}
            )
        elif i < n_block:
            kept_block.append(f)
        else:
            kept_adv.append(f)
    verdict["blocking"] = kept_block
    verdict["advisory"] = kept_adv
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["contradiction_xcheck"] = True
    cov["contradiction_dropped_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):
        counts["blocking"] = len(kept_block)
        counts["advisory_surfaced"] = len(kept_adv)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)
    drift_floor._recompute_verdict_after_drop(verdict)


def _assess_contradictions(
    findings: list[dict[str, Any]], *, ctx, cfg: LLMConfig, runner: Runner | None, repo_root
) -> list[dict[str, Any]]:
    """Run the CONTRADICTION detection sub-call over the verdict's findings, returning the list of
    pairwise judgments. Mirrors the novelty/completion sub-calls: a single-turn structured call,
    fail-safe to ``[]`` (no drops) on any error or with fewer than two findings."""
    from rebar.llm.plan_review import _verifier_cfg  # lazy: avoid package import cycle
    from rebar.llm.runner import RunRequest, get_runner

    from . import passes

    if len(findings) < 2:
        return []
    try:
        runner_sel = runner or get_runner(cfg)
        vcfg = _verifier_cfg(cfg)
        req = RunRequest(
            system_prompt=passes._resolve_system(passes.PASS_CONTRADICTION, ctx.plan_text, vcfg),
            instructions=(
                "## Findings in THIS verdict (by combined index)\n"
                f"{_finding_listing(findings)}\n\n"
                "Emit a pair for EACH mutually-contradictory pair of findings, naming the "
                "false/contradicted member to drop. Emit nothing for findings that do not "
                "contradict."
            ),
            config=vcfg,
            reviewers=["plan-contradiction"],
            mode="structured",
            output_schema="plan_review_contradiction",
            execution_mode="single_turn",
        )
        return runner_sel.run(req).get("pairs", []) or []
    except Exception:  # noqa: BLE001 — fail-safe: any sub-call failure → [] (drop nothing)
        logger.warning("contradiction sub-call failed; cross-checking nothing", exc_info=True)
        return []


def maybe_apply_contradiction(
    ticket_id: str,
    verdict: dict[str, Any],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> None:
    """The gated CONTRADICTION cross-check entry (bug 5e40). Runs only when
    ``verify.contradiction_xcheck_active`` is true (the evidence gate — inert, verdict
    byte-identical, by default) and the verdict has ≥2 surfaced findings. Self-gates inert on a
    config-read failure. Fail-safe throughout: a failed sub-call drops nothing."""
    from rebar import config as _config

    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-cross-checked
        return
    if not getattr(verify_cfg, "contradiction_xcheck_active", False):
        return
    combined = [*(verdict.get("blocking") or []), *(verdict.get("advisory") or [])]
    if len(combined) < 2:
        return
    pairs = _assess_contradictions(combined, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root)
    apply_contradiction_drops(verdict, pairs)
    floored = (verdict.get("coverage") or {}).get("contradiction_dropped_finding_ids") or []
    if floored:
        logger.info(
            "contradiction cross-check dropped %d finding(s) on %s: %s "
            '(audit via sidecar dropped[] drop_reason="contradiction")',
            len(floored),
            ticket_id,
            ", ".join(str(x) for x in floored),
        )


# ── COMMENT-TRAIL consultation (5e40 B3) ────────────────────────────────────────────────────────
def apply_comment_trail_drops(
    verdict: dict[str, Any], resolved_map: dict[int, dict[str, Any]]
) -> None:
    """Apply the COMMENT-TRAIL consultation drop (bug 5e40) IN PLACE over the verdict's surfaced
    BLOCKING + ADVISORY findings. A finding at combined index ``i`` is DROPPED iff
    :func:`decide.comment_trail_drop` says its per-finding sub-answer marks the point ALREADY
    RESOLVED in the trail; the dropped finding moves into ``dropped`` with
    ``drop_reason="comment_trail"`` and the settling ``comment_ref``. Coverage records
    ``narrowed``/``comment_trail_xcheck``/``comment_trail_dropped_finding_ids`` and ``counts`` are
    corrected; a dropped BLOCK re-derives the verdict string. Pure (no LLM); the judgment is
    injected. No resolved finding → byte-identical."""
    from rebar.llm.review_kernel import decide

    from . import drift_floor

    blocking = verdict.get("blocking") or []
    advisory = verdict.get("advisory") or []
    combined = [*blocking, *advisory]
    if not combined or not resolved_map:
        return
    n_block = len(blocking)
    kept_block: list[dict[str, Any]] = []
    kept_adv: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(combined):
        ans = resolved_map.get(i)
        if decide.comment_trail_drop(ans):
            dropped.append(
                {
                    **f,
                    "_xchecked": True,
                    "drop_reason": "comment_trail",
                    "comment_ref": (ans or {}).get("comment_ref", ""),
                }
            )
        elif i < n_block:
            kept_block.append(f)
        else:
            kept_adv.append(f)
    if not dropped:
        return
    verdict["blocking"] = kept_block
    verdict["advisory"] = kept_adv
    verdict.setdefault("dropped", []).extend(dropped)
    cov = verdict.setdefault("coverage", {})
    cov["narrowed"] = True
    cov["comment_trail_xcheck"] = True
    cov["comment_trail_dropped_finding_ids"] = [f.get("id") for f in dropped]
    counts = cov.get("counts")
    if isinstance(counts, dict):
        counts["blocking"] = len(kept_block)
        counts["advisory_surfaced"] = len(kept_adv)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)
    drift_floor._recompute_verdict_after_drop(verdict)


def _assess_comment_trail(
    findings: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> dict[int, dict[str, Any]]:
    """Run the COMMENT-TRAIL detection sub-call over the verdict's findings + the ticket comment
    trail, returning ``{combined_index: assessment}``. Mirrors the completion sub-call reshape (a
    non-int / out-of-range index is dropped; a later item wins). Fail-safe to ``{}`` (no drops) on
    any error or with no findings / no trail."""
    from rebar.llm.plan_review import _verifier_cfg  # lazy: avoid package import cycle
    from rebar.llm.runner import RunRequest, get_runner

    from . import passes

    if not findings or not comments:
        return {}
    try:
        runner_sel = runner or get_runner(cfg)
        vcfg = _verifier_cfg(cfg)
        req = RunRequest(
            system_prompt=passes._resolve_system(passes.PASS_COMMENT_TRAIL, ctx.plan_text, vcfg),
            instructions=(
                "## Ticket comment trail (in order)\n"
                f"{_comment_trail_block(comments)}\n\n"
                "## Findings to assess (by combined index)\n"
                f"{_finding_listing(findings)}\n\n"
                "For EACH finding, by its index, say whether the comment trail ALREADY resolves "
                "the point it raises. Answer 'no' unless the trail genuinely settles it."
            ),
            config=vcfg,
            reviewers=["plan-comment-trail"],
            mode="structured",
            output_schema="plan_review_comment_trail",
            execution_mode="single_turn",
        )
        raw = runner_sel.run(req).get("assessments", []) or []
    except Exception:  # noqa: BLE001 — fail-safe: any sub-call failure → {} (drop nothing)
        logger.warning("comment-trail sub-call failed; consulting nothing", exc_info=True)
        return {}
    by_index: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if isinstance(idx, int) and 0 <= idx < len(findings):
            by_index[idx] = item
    return by_index


def maybe_apply_comment_trail(
    ticket_id: str,
    verdict: dict[str, Any],
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> None:
    """The gated COMMENT-TRAIL consultation entry (bug 5e40). Runs only when
    ``verify.comment_trail_xcheck_active`` is true (evidence gate — inert by default) and the ticket
    has both surfaced findings and a recorded comment trail. The trail comes from the assembled
    context (``ctx.state["comments"]`` — already read at context assembly, no extra store read).
    Self-gates inert on a config-read failure; fail-safe throughout."""
    from rebar import config as _config

    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-consulted
        return
    if not getattr(verify_cfg, "comment_trail_xcheck_active", False):
        return
    combined = [*(verdict.get("blocking") or []), *(verdict.get("advisory") or [])]
    comments = (getattr(ctx, "state", {}) or {}).get("comments") or []
    if not combined or not comments:
        return
    resolved = _assess_comment_trail(
        combined, comments, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )
    apply_comment_trail_drops(verdict, resolved)
    floored = (verdict.get("coverage") or {}).get("comment_trail_dropped_finding_ids") or []
    if floored:
        logger.info(
            "comment-trail cross-check dropped %d finding(s) on %s: %s "
            '(audit via sidecar dropped[] drop_reason="comment_trail")',
            len(floored),
            ticket_id,
            ", ".join(str(x) for x in floored),
        )
