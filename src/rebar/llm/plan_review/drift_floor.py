"""The Pass-3 DRIFT floor (bug 5e40) — convergent re-review on the plan-UNCHANGED + code-DRIFTED
axis.

Plan review already converges two re-review axes with deterministic Pass-3 floors: the NOVELTY
floor (ADR-0008 — plan CHANGED + code UNCHANGED, the remediation loop) and the COMPLETION floor
(ADR-0024 — a container's delivered children). A THIRD axis was uncovered: an already-signed,
plan-UNCHANGED attestation re-reviewed after HEAD drifts. For an UNSCOPED plan (no per-path dep
map) the whole-HEAD invalidation (``compute_validity`` verdict ``stale-head``) is the ONLY code-
drift guard, and it escalates the re-review to a FULL, non-deterministic run that can mint NEW
blocking findings on byte-identical plan text (5e40: ~32% verdict instability). The scoped
drift-refresh path (ADR-0002) never fires here because it has no deps to probe.

This module EXTENDS the SAME novelty machinery to that axis — it does NOT touch the whole-HEAD
invalidation TRIGGER (which stays as the sole code-drift guard, the reason the previous
"stop invalidating on drift" fix was blocked as a security regression). It converges only the
re-review OUTCOME: the Pass-3 drop predicate :func:`rebar.llm.review_kernel.decide.drift_floor_drop`
drops a NOVEL finding IFF its citations do NOT intersect the drifted file set, and KEEPS any novel
finding that cites drifted code — so unrelated-drift noise converges to the prior PASS while genuine
code-drift findings still surface.

Extracted from ``attest``/``__init__`` (both at/near the module-size cap) as its own
call-graph seam.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner

logger = logging.getLogger(__name__)


def _drifted_paths(
    base_sha: str | None, head_sha: str | None, *, repo_root=None
) -> list[str] | None:
    """The repo-relative file paths that changed between ``base_sha`` (the SHA the plan-review
    attestation signed against) and ``head_sha`` (current HEAD), via ``git diff --name-only
    base..head``, sorted. Returns ``None`` when it cannot be computed (missing SHA, unknown HEAD,
    git error) — the drift floor treats ``None`` as "drifted set UNKNOWN" and drops NOTHING
    (fail-safe: never suppress a finding when we cannot prove which files drifted)."""
    from rebar import config as _config

    if not base_sha or not head_sha or head_sha == "unknown":
        return None
    try:
        import subprocess

        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_sha}..{head_sha}"],
            cwd=str(_config.repo_root(repo_root)),
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:  # noqa: BLE001 — best-effort: unresolvable diff → unknown set → no drops
        return None
    return sorted({ln.strip() for ln in out.stdout.splitlines() if ln.strip()})


def drift_floor_candidate(
    ticket_id: str, *, window_minutes: int, now_ns: int | None = None, repo_root=None
) -> dict[str, Any]:
    """The DRIFT-FLOOR eligibility DECISION for ``ticket_id`` (bug 5e40) — the code-drift-axis
    complement of ``attest.remediation_mode_candidate`` and ``attest.drift_refresh_candidate``:

    - remediation → plan CHANGED + code UNCHANGED (the plan-edit loop).
    - drift-refresh → plan UNCHANGED + code DRIFTED, but the plan is SCOPED (per-path dep hashes
      exist to probe against).
    - THIS → plan UNCHANGED + code DRIFTED where there is NO scoped dep map to probe, so the
      whole-HEAD invalidation (``compute_validity`` verdict ``stale-head``) escalated the
      already-signed PASS to a FULL, non-deterministic re-review (the uncovered 5e40 regime,
      chiefly unscoped plans). Decides eligibility for the Pass-3 drift floor that converges that
      re-review's OUTCOME; the whole-HEAD invalidation TRIGGER is untouched.

    Requires a certified plan-review SIGNATURE baseline (5e40 is about an already-signed PASS; a
    BLOCK never signed, so there is no prior PASS to drift from — no sidecar branch here). Eligible
    IFF ALL hold: ``signed`` (a certified plan-review manifest), ``plan_unchanged`` (current
    material fingerprint == the signed one), ``code_drifted`` (the signed ``verified_at_sha`` and
    current HEAD both present and DIFFER — the exact stale-head condition), ``registry_unchanged``,
    ``prior_sidecar`` (a REVIEW_RESULT with finding text — the novelty prior set), and
    ``within_window``. Carries ``drifted_files`` (the signed-SHA→HEAD diff, or ``None`` if it could
    not be computed) so the floor keeps any novel finding that cites a drifted file. NEVER raises:
    any read error leaves the failing precondition False and yields ``eligible=False`` → full
    review (a broken signal can only DENY convergence, never suppress incorrectly)."""
    from rebar import config as _config
    from rebar import signing

    from . import attest, sidecar

    reasons: dict[str, bool] = {
        "signed": False,
        "plan_unchanged": False,
        "code_drifted": False,
        "registry_unchanged": False,
        "prior_sidecar": False,
        "within_window": False,
    }
    drifted_files: list[str] | None = None
    try:
        try:
            result = signing.verify_signature(ticket_id, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — signing unavailable → not eligible → full review
            result = {}
        manifest = (result or {}).get("manifest")
        if not (result or {}).get("verified") or not attest.is_plan_review_manifest(manifest):
            return {"eligible": False, "reasons": reasons, "drifted_files": None}
        reasons["signed"] = True

        # plan UNCHANGED: current material fingerprint EQUALS the prior signed one (the inverse of
        # remediation's plan_changed — over-loosening here would floor an EDITED plan).
        signed_material = attest.manifest_material(manifest)
        current_material = attest.current_material_fingerprint(ticket_id, repo_root=repo_root)
        reasons["plan_unchanged"] = (
            signed_material is not None
            and current_material is not None
            and current_material == signed_material
        )

        # code DRIFTED: the signed verified_at_sha and current HEAD both present and DIFFER. This
        # mirrors compute_validity's unscoped whole-HEAD "stale-head" trigger anchor exactly.
        signed_sha = signing.verified_at_sha_from_manifest(manifest)
        current_sha = signing.head_sha(_config.repo_root(repo_root))
        reasons["code_drifted"] = (
            bool(signed_sha)
            and bool(current_sha)
            and current_sha != "unknown"
            and signed_sha != current_sha
        )

        reasons["registry_unchanged"] = attest.manifest_regver(manifest) == attest.registry_version(
            repo_root
        )

        prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
        reasons["prior_sidecar"] = bool(prior) and any(
            (f.get("finding") or "").strip() for f in (prior or {}).get("findings", [])
        )

        last_ts = sidecar.latest_review_timestamp(ticket_id, repo_root=repo_root)
        if last_ts is not None:
            current_ns = now_ns if now_ns is not None else time.time_ns()
            reasons["within_window"] = (
                0 <= (current_ns - last_ts) <= window_minutes * 60 * 1_000_000_000
            )

        if reasons["code_drifted"]:
            drifted_files = _drifted_paths(signed_sha, current_sha, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — fail-safe: any read error → not eligible → full review
        logger.warning(
            "drift-floor candidate check failed; treating as not eligible", exc_info=True
        )
        return {"eligible": False, "reasons": reasons, "drifted_files": None}
    return {
        "eligible": all(reasons.values()),
        "reasons": reasons,
        "drifted_files": drifted_files,
    }


def decision(ticket_id: str, repo_root) -> dict[str, Any] | None:
    """The DRIFT-FLOOR eligibility decision for ``ticket_id`` (bug 5e40), or ``None`` when config is
    unreadable — in which case the gate runs a byte-identical full review. Reuses the remediation
    freshness window (the same "recent re-review" notion, on the opposite code axis)."""
    from rebar import config as _config

    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → conservative full review (no drift floor)
        return None
    return drift_floor_candidate(
        ticket_id, window_minutes=verify_cfg.remediation_window_minutes, repo_root=repo_root
    )


def _finding_cited_paths(finding: dict[str, Any]) -> set[str]:
    """The ``kind == "file"`` citation paths of ONE in-memory finding (mirrors
    ``manifest._cited_paths``' per-finding rule). Free-text citations with no ``path`` are ignored,
    never guessed."""
    return {
        str(c["path"])
        for c in (finding.get("citations") or [])
        if isinstance(c, dict) and c.get("kind") == "file" and c.get("path")
    }


def _recompute_verdict_after_drop(verdict: dict[str, Any]) -> None:
    """Re-derive the verdict STRING after the drift floor drops findings, using the SAME rule as
    ``orchestrator.finalize_verdict``. Only ever DOWNGRADES a ``BLOCK`` whose ``blocking``
    bucket the floor emptied (dropping a novel non-drift blocking finding is the PASS→BLOCK flip
    must converge back). A DET block is never novel (the novelty sub-call never scores it), so it
    survives as ``blocking`` and keeps the verdict at BLOCK. Leaves any non-BLOCK verdict
    untouched."""
    from . import orchestrator

    if verdict.get("verdict") != "BLOCK" or verdict.get("blocking"):
        return
    cov = verdict.get("coverage") or {}
    if cov.get("llm_unavailable"):
        verdict["verdict"] = "INDETERMINATE"
    elif cov.get("hierarchy_incomplete"):
        # Checked before verify_failed's fail-open path, mirroring finalize_verdict: a missing
        # hierarchy must never be masked by an advisory-only verify failure falling open to PASS.
        verdict["verdict"] = "INDETERMINATE"
    elif cov.get("verify_failed"):
        verdict["verdict"] = (
            "INDETERMINATE"
            if orchestrator._any_blocking_criterion(verdict.get("indeterminate") or [])
            else "PASS"
        )
    else:
        verdict["verdict"] = "PASS"


def apply_to_verdict(
    verdict: dict[str, Any],
    novelty_map: dict[int, float],
    *,
    drifted_files: set[str] | None,
    t_novel: float,
) -> None:
    """Apply the Pass-3 DRIFT floor (bug 5e40) IN PLACE over the verdict's surfaced BLOCKING +
    ADVISORY findings (indexed as ``[*blocking, *advisory]`` — the SAME order the novelty scorer is
    fed). A finding is DROPPED iff :func:`decide.drift_floor_drop` (novel AND
    its citations do NOT intersect ``drifted_files``); a novel finding that DOES cite a drifted file
    is KEPT, and a carryover finding is KEPT. Dropped findings move into the verdict's ``dropped``
    bucket with ``drop_reason="drift"`` (the sidecar persists them with ``norm_id``); coverage
    records ``narrowed`` / ``drift_floor`` / ``drift_floored_criteria`` /
    ``drift_floored_finding_ids`` /
    ``drifted_files`` (drift-namespaced so they never collide with the novelty or completion floors)
    and ``counts`` are corrected. Dropping a BLOCKING finding can flip BLOCK→PASS, so
    string is re-derived. Pure (no LLM); the novelty per index is injected. ``drifted_files=None``
    (drift set UNKNOWN) or a no-drop run leaves the verdict byte-identical (fail-safe:
    when we cannot prove which files drifted)."""
    from rebar.llm.review_kernel import decide

    if drifted_files is None:
        return
    blocking = verdict.get("blocking") or []
    advisory = verdict.get("advisory") or []
    combined = [*blocking, *advisory]
    n_block = len(blocking)
    kept_block: list[dict[str, Any]] = []
    kept_adv: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for i, f in enumerate(combined):
        nov = novelty_map.get(i, 0.0)
        cited = _finding_cited_paths(f)
        if decide.drift_floor_drop(
            nov, cited_paths=cited, drifted_files=drifted_files, t_novel=t_novel
        ):
            dropped.append({**f, "_floored": True, "novelty": nov, "drop_reason": "drift"})
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
    cov["drift_floor"] = True
    cov["drift_floored_criteria"] = sorted({c for f in dropped for c in (f.get("criteria") or [])})
    cov["drift_floored_finding_ids"] = [f.get("id") for f in dropped]
    cov["drifted_files"] = sorted(drifted_files)
    counts = cov.get("counts")
    if isinstance(counts, dict):  # keep the baked counts consistent with the post-floor buckets
        counts["blocking"] = len(kept_block)
        counts["advisory_surfaced"] = len(kept_adv)
        counts["dropped"] = (counts.get("dropped") or 0) + len(dropped)
    _recompute_verdict_after_drop(verdict)


def maybe_apply(
    ticket_id: str,
    verdict: dict[str, Any],
    drift: dict[str, Any] | None,
    *,
    ctx,
    cfg: LLMConfig,
    runner: Runner | None,
    repo_root,
) -> None:
    """The gated Pass-3 DRIFT-floor entry (bug 5e40): apply the floor only when the re-review is a
    code-drift re-review of an already-signed, plan-UNCHANGED attestation (``drift`` eligible — the
    stale-head regime the whole-HEAD invalidation escalated to a full, non-deterministic re-review).
    REUSES the SAME 150b novelty sub-call (``__init__._score_floor_novelty``, lazily imported to
    avoid a package import cycle) and the shared kernel drop math; the drift-intersection predicate
    (``decide.drift_floor_drop``) is what KEEPS any novel finding that cites a drifted file,
    preserving the code-drift detection the whole-HEAD trigger exists for. The whole-HEAD
    invalidation TRIGGER (``compute_validity`` ``stale-head``) is untouched — this
    re-review OUTCOME. Self-gates inert (verdict byte-identical) when not eligible, the
    unknown, or there is no prior surfaced memory."""
    from rebar import config as _config
    from rebar.llm.plan_review import _score_floor_novelty  # lazy: package loaded at call time

    from . import sidecar

    if not (drift and drift.get("eligible")):
        return
    drifted_raw = drift.get("drifted_files")
    if drifted_raw is None:  # drift set unknown → fail-safe: never suppress
        return
    try:
        verify_cfg = _config.load_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → run un-floored
        return
    blocking = verdict.get("blocking") or []
    advisory = verdict.get("advisory") or []
    combined = [*blocking, *advisory]
    prior = sidecar.latest_review_result(ticket_id, repo_root=repo_root)
    # SURFACED-ONLY prior set (bug old-frilly-plankton): a previously-dropped finding must not
    # re-enter and score itself "carryover", escaping the floor that dropped it.
    prior_findings = sidecar.surfaced_findings(prior)
    if not combined or not prior_findings:
        return
    novelty_map = _score_floor_novelty(
        combined, prior_findings, ctx=ctx, cfg=cfg, runner=runner, repo_root=repo_root
    )
    apply_to_verdict(
        verdict,
        novelty_map,
        drifted_files=set(drifted_raw),
        t_novel=verify_cfg.novelty_drop_threshold,
    )
    floored = (verdict.get("coverage") or {}).get("drift_floored_finding_ids") or []
    if floored:
        logger.info(
            "drift floor dropped %d finding(s) on %s: %s "
            '(audit via sidecar dropped[] drop_reason="drift")',
            len(floored),
            ticket_id,
            ", ".join(str(x) for x in floored),
        )
