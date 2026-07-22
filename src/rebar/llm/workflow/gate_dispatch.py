"""Gate-engine dispatch: PRODUCE the gate verdicts via the v3 engine workflows
(epic B, story B5 — the cutover).

The plan-review claim gate and the completion close gate each have a *verdict
production* step and a *signing* step. This module owns ONLY verdict production via
the engine workflows (``gates/plan-review.yaml`` + ``gates/completion-verification.yaml``);
the SIGNING wrappers (``review_plan`` / ``_commands.transition``) are left untouched, so
the signed attestations stay byte-compatible regardless of which engine produced the
verdict (the cutover requirement).

Degradation semantics it guarantees:

* **Plan-review INDETERMINATE-on-outage.** A systemic LLM outage (preflight raises
  :class:`LLMUnavailableError`) — or any mid-run LLM-tier failure — degrades to an
  unsigned INDETERMINATE verdict, never a hollow PASS (bug ``fuel-posse-ball``).
* **Completion fail-closed-on-outage.** The completion verifier preflights and lets
  :class:`LLMUnavailableError` PROPAGATE (the close gate catches it and fail-closes),
  and consumes the cfg the caller already tuned (verifier model + step-budget floor).

The workflow runs IN-MEMORY (``MemoryRecorder``) so a gate run writes NO workflow-run
events to the gated ticket — it only emits a sidecar / signs. The plan-review batch is
driven by the B1 ``ProductionBatchRunner``; agent steps (verify/coach) run through the
``RunnerAgentStep`` bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

# Back-compat re-export (load-bearing): tests/unit/test_code_review_fp_ledger.py calls
# ``gate_dispatch._attach_code_review_metrics`` at 7 sites. The code-review finalization cluster
# moved to the code_review/finalize.py strict leaf; this keeps the attribute resolving here.
from rebar.llm.code_review.finalize import _attach_code_review_metrics  # noqa: F401
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.gate_error_sidecar import emit_gate_error


def _gate_doc(name: str, repo_root) -> dict[str, Any]:
    """Load a packaged gate workflow (``src/rebar/llm/workflow/gates/<name>.yaml``).

    The gate workflows are PACKAGE data, not under ``.rebar/workflows`` / ``examples``,
    so they are resolved by absolute path (not via the name-resolver)."""
    from .runs import load_workflow_doc

    p = Path(__file__).resolve().parent / "gates" / f"{name}.yaml"
    return load_workflow_doc(p, repo_root)


# ── plan-review ───────────────────────────────────────────────────────────────────
# Named step ids for gates/plan-review.yaml. The dispatcher's mid-tail RECOVERY and the metrics
# reconstruction below key off these ids (a run's succeeded-step partition is looked up by id); a
# YAML rename that dropped one would make the lookup silently return None, so a recoverable run
# would degrade to a hollow INDETERMINATE with NO error (the exact silent-failure this centralizes
# away). Keep the literals here, once, and validate them against the loaded doc at dispatch time
# (see `_validate_gate_step_ids`) so a rename is caught LOUDLY instead of silently degraded.
STEP_PRECHECK = "precheck"
STEP_ASSEMBLE = "assemble"
STEP_FINDERS = "finders"
STEP_VERIFY = "verify"
STEP_DECIDE = "decide"
STEP_COACH = "coach"

# The step ids the recovery/metrics logic depends on being present in the loaded gate doc.
_PLAN_REVIEW_REQUIRED_STEP_IDS = frozenset(
    {STEP_PRECHECK, STEP_ASSEMBLE, STEP_FINDERS, STEP_VERIFY, STEP_DECIDE, STEP_COACH}
)


class GateContractError(RuntimeError):
    """A loaded gate workflow is missing a step id the dispatcher's recovery/metrics logic
    references — i.e. a YAML step was renamed/dropped out from under the recovery code. Raised
    LOUDLY at dispatch (NOT silently degraded to INDETERMINATE) so the break surfaces where it
    can be fixed instead of quietly discarding real findings."""


def _collect_step_ids(node: Any) -> set[str]:
    """Every step ``id`` in a loaded workflow doc, including ids nested inside ``branch``
    then/else arms (a recursive walk over the plain dict/list doc structure)."""
    ids: set[str] = set()
    if isinstance(node, dict):
        sid = node.get("id")
        if isinstance(sid, str):
            ids.add(sid)
        for value in node.values():
            ids |= _collect_step_ids(value)
    elif isinstance(node, list):
        for item in node:
            ids |= _collect_step_ids(item)
    return ids


def _validate_gate_step_ids(doc: dict[str, Any], required: frozenset, *, gate_name: str) -> None:
    """Fail LOUDLY if the loaded gate doc is missing any step id the dispatcher references.

    A step-id rename in ``gates/<gate_name>.yaml`` would otherwise make the recovery lookups
    silently return ``None`` and degrade a recoverable run to INDETERMINATE. Called at dispatch
    time (right after the doc is loaded) so drift is caught here, not swallowed downstream."""
    present = _collect_step_ids(doc.get("steps"))
    missing = sorted(required - present)
    if missing:
        raise GateContractError(
            f"gate workflow {gate_name!r} is missing step id(s) {missing} that the dispatcher's "
            f"recovery/metrics logic references (present step ids: {sorted(present)}). A step was "
            f"likely renamed in gates/{gate_name}.yaml — update the STEP_* constants in "
            f"gate_dispatch.py to match, or restore the id."
        )


def produce_plan_review_verdict(
    ctx, cfg, *, runner=None, advisory_cap: int, repo_root=None, probe_criteria=None
) -> dict[str, Any]:
    """Produce a ``plan_review_verdict`` by running ``gates/plan-review.yaml`` in-memory.

    The verdict-production half of ``review_plan``. Preflights the runner so a systemic
    outage degrades to INDETERMINATE (unsigned) before any billable call; a mid-run
    LLM-tier failure degrades the same way (never a hollow PASS).

    ``probe_criteria`` (PROBE MODE, drift-refresh tripwire): when a non-empty id list, the
    finder runs ONLY those criteria (the cheap E4+G1G2 probe) instead of the full routed set.
    Always threaded as a workflow input (``[]`` = normal full review) so the gate's
    ``${{ inputs.probe_criteria }}`` reference always resolves."""
    import time

    from rebar.llm.config import gate_config
    from rebar.llm.plan_review.orchestrator import (
        assemble_context_cache,
        collect_contract_violations,
    )
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
    from rebar.llm.runner import get_runner

    from . import executor as _ex
    from .recorder import MemoryRecorder
    from .runs import RunnerAgentStep

    runner_sel = runner or get_runner(cfg)
    try:
        runner_sel.preflight()
    except LLMUnavailableError as exc:
        # Write-then-degrade (ticket 8bc5): capture the env/integration-diagnosis interval as a
        # dedicated gate_error_v1 sidecar, THEN preserve the existing soft-degrade outcome.
        emit_gate_error(ctx.ticket_id, "plan_review", cause=str(exc), repo_root=repo_root)
        return _degraded_plan_review_verdict(
            ctx, cfg, error=exc, advisory_cap=advisory_cap, runner_name=runner_sel.name
        )

    # NOTE: the workflow's `repo_root` is the TICKET-store read-root (it reaches
    # assemble_context's `rebar.show_ticket(repo_root=...)` via StepContext) — NOT the code
    # read-root, which is a SEPARATE snapshot (cfg.repo_path/current_code_root). The det-floor
    # / grounding ops resolve the code root themselves via `resolve_code_root`
    # (assemble_context's `repo_root` FIELD), and the agentic verifier reads code via
    # cfg.repo_path; so we must NOT thread the code snapshot here, or ticket reads would look
    # for the store under the .git-less code snapshot and miss it.
    doc = _gate_doc("plan-review", repo_root)
    # Catch a step-id rename in gates/plan-review.yaml LOUDLY here — before the billable run —
    # rather than letting a recovery lookup silently return None and degrade to INDETERMINATE.
    _validate_gate_step_ids(doc, _PLAN_REVIEW_REQUIRED_STEP_IDS, gate_name="plan-review")
    rec = MemoryRecorder()
    _t_total = time.monotonic()
    # One run-scoped assemble_context memo for the whole workflow: the four plan-review ops
    # (precheck / assemble_criteria / verify_inputs / coach_inputs) each call assemble_context
    # with the SAME (ticket_id, repo_root) inside this run, so the cache collapses their N+1
    # graph reads to a single read (and returns an identical PlanContext, so verdict bytes are
    # unchanged). The scope is dropped on exit — it never leaks across runs/tickets.
    try:
        # Resolve the caller's config ONCE for the whole run: gate_config publishes `cfg` so every
        # op (and the non-step ProductionBatchRunner) reads the SAME resolved config via
        # resolve_gate_config instead of re-deriving from env (epic veiny-trout-brink).
        with assemble_context_cache(), collect_contract_violations(), gate_config(cfg):
            res = _ex.run_workflow(
                doc,
                {"ticket_id": ctx.ticket_id, "probe_criteria": list(probe_criteria or [])},
                target_ticket=ctx.ticket_id,
                repo_root=repo_root,
                agent_runner=RunnerAgentStep(runner=runner_sel, repo_root=repo_root, config=cfg),
                batch_runner=ProductionBatchRunner(runner=runner_sel),
                recorder=rec,
            )
    except LLMUnavailableError as exc:
        # Write-then-degrade (ticket 8bc5): same additive gate_error capture on the mid-run
        # infra outage, before preserving the soft-degrade.
        emit_gate_error(ctx.ticket_id, "plan_review", cause=str(exc), repo_root=repo_root)
        return _degraded_plan_review_verdict(
            ctx, cfg, error=exc, advisory_cap=advisory_cap, runner_name=runner_sel.name
        )
    total_ms = round((time.monotonic() - _t_total) * 1000, 1)

    verdict = res.terminal_output
    if res.status == "succeeded" and isinstance(verdict, dict) and "verdict" in verdict:
        _attach_plan_review_metrics(verdict, rec, total_ms)
        return verdict

    # The run failed mid-tail. Pass-4 coach is advisory POLISH — bespoke run_review treats a
    # coach failure as NON-fatal (it still emits the verdict, sans coaching). Mirror that: if
    # Pass-3 `decide` succeeded (so finders+verify ran), reconstruct the verdict from the
    # decide partition with empty coaching — NOT a hollow INDETERMINATE that would discard the
    # real findings and wrongly block the claim.
    recovered = _recover_plan_review_coach_failure(rec, cfg, error=res.error)
    if recovered is not None:
        _attach_plan_review_metrics(recovered, rec, total_ms)
        return recovered

    # Pass-2 verify failed but Pass-1 finders SUCCEEDED (e.g. the agentic verifier exhausted its
    # step budget on a finding-rich ticket — bug 59bc). The LLM tier WAS available and produced
    # findings; treating that as a systemic outage discards them and (fail-closed) wrongly blocks
    # the claim. Recover: preserve the Pass-1 findings as unverified → INDETERMINATE, and let
    # finalize_verdict fail-OPEN unless a preserved finding is on a blocking-enabled criterion.
    recovered = _recover_plan_review_verify_failure(rec, cfg, error=res.error)
    if recovered is not None:
        _attach_plan_review_metrics(recovered, rec, total_ms)
        return recovered

    # finders failed (the LLM tier did not produce findings) — degrade to INDETERMINATE,
    # never sign a hollow PASS, mirroring run_review's broad-except → llm_unavailable path.
    return _degraded_plan_review_verdict(
        ctx,
        cfg,
        error=(res.error or "plan-review workflow LLM tier failed"),
        advisory_cap=advisory_cap,
        runner_name=runner_sel.name,
    )


# Step ids/kinds that partition a plan-review run into its latency tiers (toy-kink-ire).
_DET_STEP_IDS = frozenset({STEP_PRECHECK})  # the deterministic floor tier
_LLM_STEP_KINDS = frozenset({"agent", "batch"})  # the billable LLM tier (finders/verify/coach)


def _attach_plan_review_metrics(verdict: dict[str, Any], rec, total_ms: float) -> None:
    """Reinstate ``coverage['metrics']`` on the WORKFLOW plan-review path (toy-kink-ire).

    B-RETIRE removed bespoke ``run_review``, the only producer of the per-pass latency/cost
    metrics (db7b AC5). This reconstructs the equivalent from the workflow run's recorder
    step timings (added by the interpreter) so the sidecar carries them again for passive
    latency/cost-target refinement:

    - ``det_ms``    — wall-clock of the deterministic floor (the ``precheck`` step).
    - ``llm_ms``    — wall-clock of the billable LLM tier (the ``agent``/``batch`` steps:
                      Pass-1 ``finders``, Pass-2 ``verify``, Pass-4 ``coach_notes``).
    - ``total_ms``  — the whole run's wall-clock (measured around ``run_workflow``).
    - ``llm_calls`` — a cost proxy: the Pass-1 finder ``criteria_count`` + one per succeeded
                      agent step (``verify`` / ``coach_notes``). Mirrors run_review's proxy.
    - ``claim_path``— the structural marker (the fast claim check is a local HMAC verify,
                      LLM/network-free).

    ``det_ms + llm_ms`` deliberately does NOT equal ``total_ms``: the scripted prep/decision
    steps (``assemble`` / ``grounding`` / ``verify_inputs`` / ``decide`` / ``coach_inputs`` /
    ``coach``) are non-LLM overhead, counted into neither tier — absorbed only into ``total_ms``
    (the same split the bespoke ``run_review`` reported).

    Mutates ``verdict['coverage']['metrics']`` in place (only that key; existing coverage is
    preserved). Tolerant of untimed/partial records (a missing ``duration_ms`` contributes 0)
    so it never raises inside the gate.
    """
    det_ms = 0.0
    llm_ms = 0.0
    finder_criteria = 0
    agent_calls = 0
    verify_requests = 0  # Pass-2 verifier model-request count — step usage vs its budget (bug 59bc)
    for s in rec.steps:
        if not isinstance(s, dict) or s.get("status") != "succeeded":
            continue
        step_id = s.get("step_id")
        kind = s.get("kind")
        dur = s.get("duration_ms")
        if isinstance(dur, (int, float)):
            if step_id in _DET_STEP_IDS:
                det_ms += dur
            elif kind in _LLM_STEP_KINDS:
                llm_ms += dur
        if kind == "batch":
            finder_criteria += int((s.get("outputs") or {}).get("criteria_count") or 0)
        elif kind == "agent":
            agent_calls += 1
            if step_id == STEP_VERIFY:
                verify_requests += int(
                    ((s.get("outputs") or {}).get("_usage") or {}).get("requests") or 0
                )
    metrics = {
        "det_ms": round(det_ms, 1),
        "llm_ms": round(llm_ms, 1),
        "total_ms": round(total_ms, 1),
        "llm_calls": finder_criteria + agent_calls,
        # Pass-2 verify step usage: model requests (~tool-call cycles) the verifier actually
        # consumed, so headroom vs the per-finding budget (`step_budget_per_item`) is observable.
        "verify_requests": verify_requests,
        "claim_path": "no-llm/no-network (structural; the fast claim check is a local HMAC verify)",
    }
    coverage = verdict.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
        verdict["coverage"] = coverage
    coverage["metrics"] = metrics


def _recover_plan_review_coach_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """If the only failure was in the Pass-4 coach tail (Pass-3 ``decide`` succeeded),
    reassemble the verdict from the recorded ``decide`` partition with EMPTY coaching —
    the same non-fatal-coach result bespoke run_review emits. Returns None if ``decide``
    did not succeed (then the LLM tier genuinely failed → caller degrades to INDETERMINATE)."""
    from rebar.llm import findings as _findings
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    # Latest-wins map of succeeded step outputs by their step id (frame-key tail).
    succeeded: dict[str, dict] = {}
    for s in rec.steps:
        if s.get("status") != "succeeded":
            continue
        fk = s.get("frame_key") or s.get("step_id") or ""
        succeeded[str(fk).rsplit("/", 1)[-1]] = s.get("outputs") or {}

    decide = succeeded.get(STEP_DECIDE)
    precheck = succeeded.get(STEP_PRECHECK)
    if not decide or not precheck or "blocking" not in decide:
        return None  # Pass-3 did not complete → the LLM tier failed, not just the coach

    parts = {
        k: list(decide.get(k) or [])
        for k in ("blocking", "surfaced", "overflow", "indeterminate", "dropped")
    }
    coverage = {
        "det": precheck.get("det_coverage") or {},
        "routing": (succeeded.get(STEP_ASSEMBLE) or {}).get("routing") or {},
        "llm_ran": True,
        "coach_error": str(error) if error else "pass-4 coach failed; verdict emitted without it",
        "hierarchy_incomplete": precheck.get("hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": precheck.get("hierarchy_incomplete_detail", []),
    }
    pctx = PlanContext(
        ticket_id=str(precheck.get("canonical_id") or ""),
        ticket_type=str(precheck.get("ticket_type") or ""),
        title="",
        description="",
    )
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=[], coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


def _recover_plan_review_verify_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """If Pass-1 ``finders`` SUCCEEDED but Pass-2/3 did not (the verify step failed — e.g. the
    agentic verifier exhausted its step budget), reassemble the verdict from the Pass-1 findings
    PRESERVED as unverified → INDETERMINATE, with ``coverage.verify_failed`` (NOT
    ``llm_unavailable``). ``finalize_verdict`` then fails OPEN unless a preserved finding sits on
    a blocking-enabled criterion (bug 59bc). Returns None if ``finders`` did not succeed (then the
    LLM tier genuinely failed → caller degrades to INDETERMINATE)."""
    from rebar.llm import findings as _findings
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    succeeded: dict[str, dict] = {}
    for s in rec.steps:
        if s.get("status") != "succeeded":
            continue
        fk = s.get("frame_key") or s.get("step_id") or ""
        succeeded[str(fk).rsplit("/", 1)[-1]] = s.get("outputs") or {}

    finders = succeeded.get(STEP_FINDERS)
    precheck = succeeded.get(STEP_PRECHECK)
    if not finders or not precheck or STEP_DECIDE in succeeded:
        # finders did not run (genuine LLM-tier failure), or decide DID run (a different
        # failure the coach-recovery handles) → not a verify-only failure.
        return None
    pass1 = list(finders.get("findings") or [])
    if not pass1:
        return None  # no findings to preserve → nothing to recover; let it degrade

    # Route the preserved Pass-1 findings through Pass-3 with EMPTY verifications: each finding
    # then takes pass3_decide(None) → the kernel's documented no-verification degrade
    # (decision=indeterminate, validity/impact/priority=0, severity=none, verification=None). This
    # reuses the existing decision path — the verdict stays schema-valid and NO new decision state
    # is introduced — rather than hand-stamping a partial finding shape.
    decided = orchestrator.pass3_over_findings(pass1, {})
    parts = orchestrator.partition_findings(
        list(precheck.get("det_blocking") or []),
        list(precheck.get("det_advisory") or []),
        decided,
    )
    coverage = {
        "det": precheck.get("det_coverage") or {},
        "routing": (succeeded.get(STEP_ASSEMBLE) or {}).get("routing") or {},
        "llm_ran": True,
        "verify_failed": True,
        "verify_error": str(error)
        if error
        else "pass-2 verify failed; findings preserved unverified",
        "hierarchy_incomplete": precheck.get("hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": precheck.get("hierarchy_incomplete_detail", []),
    }
    pctx = PlanContext(
        ticket_id=str(precheck.get("canonical_id") or ""),
        ticket_type=str(precheck.get("ticket_type") or ""),
        title="",
        description="",
    )
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=[], coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


def _degraded_plan_review_verdict(
    ctx, cfg, *, error, advisory_cap: int, runner_name: str | None
) -> dict[str, Any]:
    """The unsigned INDETERMINATE verdict a systemic LLM outage degrades to — the SAME
    shape ``run_review`` produces (DET floor ran, LLM did not): DET findings partitioned,
    ``coverage.llm_unavailable=True`` (so ``finalize_verdict`` ⇒ INDETERMINATE and
    ``review_plan`` never signs it)."""
    from rebar.llm import failure as _failure
    from rebar.llm.plan_review import det_floor, orchestrator

    det_results = det_floor.run_det_floor(ctx)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    # Disposition (story blackbear): when the raised error carries an ``.outcome`` (the genuine
    # outage paths — preflight / mid-run LLMUnavailableError), persist resolution_class/retryable/
    # diagnostic onto coverage so the CLI can map a retryable outage → exit 11. A string-error
    # tail (finders produced nothing) carries no outcome → no disposition → plain INDETERMINATE.
    outcome = _failure.outcome_of(error)
    coverage = {
        "det": det_floor.det_coverage(det_results),
        "llm_ran": False,
        "llm_unavailable": True,
        "llm_error": str(error),
        **_failure.resolution_fields(outcome),
        "hierarchy_incomplete": getattr(ctx, "hierarchy_incomplete", False),
        "hierarchy_incomplete_detail": getattr(ctx, "hierarchy_incomplete_detail", []),
    }
    _failure.log_degrade(outcome, gate="plan-review", ticket_id=getattr(ctx, "ticket_id", None))
    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, [], advisory_cap=advisory_cap
    )
    return orchestrator.finalize_verdict(
        ctx, parts, coaching=[], coverage=coverage, runner_name=runner_name, model=cfg.model
    )


# ── code-review (epic b744 / WS4) ─────────────────────────────────────────────────────
# The code-review gate reuses STEP_VERIFY/STEP_DECIDE. Its post-verdict finalization cluster —
# metrics/deps/novelty-floor/session-artifact emit, plus the STEP_ASSEMBLE_DIFF step id and
# _attach_code_review_metrics (re-exported above) — lives in the code_review/finalize.py leaf.


def code_review_enabled(repo_root=None) -> bool:
    """Whether the off-by-default code-review capability is enabled (verify.enable_code_review)."""
    from rebar import config as _config

    try:
        return bool(_config.load_config(repo_root).verify.enable_code_review)
    except Exception:  # noqa: BLE001 — unreadable config ⇒ treat as disabled (inert/safe)
        return False


def _inert_code_review_verdict() -> dict[str, Any]:
    """DISABLED — INERT, zero LLM calls: clean PASS, no findings, `coverage.enabled=False`."""
    return {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "coverage": {"enabled": False, "llm_ran": False},
    }


def _degraded_code_review_verdict(*, error, runner_name: str | None) -> dict[str, Any]:
    """Unsigned INDETERMINATE degrade (outage / mid-run failure) — never a hollow PASS. Carries
    the LLM disposition (story blackbear) when the raised error classified one, so the CLI can
    map a retryable code-review outage → exit 11 the same way plan-review does."""
    from rebar.llm import failure as _failure

    outcome = _failure.outcome_of(error)
    _failure.log_degrade(outcome, gate="code-review")
    return {
        "verdict": "INDETERMINATE",
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "coverage": {
            "llm_ran": False,
            "llm_unavailable": True,
            "llm_error": str(error),
            **_failure.resolution_fields(outcome),
        },
        "runner": runner_name,
    }


@dataclass(frozen=True)
class CodeReviewRequest:
    """Bundled request for :func:`produce_code_review_verdict` (the 11 params it replaces)."""

    cfg: Any
    base: str = "HEAD~1"
    head: str = "HEAD"
    source: str | None = None
    diff_text: str | None = None
    changed_files: list[str] | None = None
    commit_message: str = ""
    runner: Any = None
    target_ticket: str | None = None
    # Local session key (story paradoxal-balsamic-bubblefish): when set (and no explicit
    # target_ticket), the gate resolves-or-creates a `code-review: session:<id>` artifact, stamps
    # verdict["session_id"], and emits onto it — giving `rebar review-code` cross-run memory.
    session_id: str | None = None
    # Gerrit change id (story blameless-grindable-noctule): selects the `change:<id>` novelty
    # keyspace for the region-gated floor when the review-bot supplies it (local uses session_id).
    change_id: str = ""
    repo_root: Any = None
    enabled: bool | None = None


class _CodeReviewPrep(NamedTuple):
    dc: Any
    doc: Any
    rec: Any
    inputs: dict[str, Any]
    context_overrides: dict[str, Any] | None
    t_total: float


def produce_code_review_verdict(request: CodeReviewRequest) -> dict[str, Any]:
    """Run ``gates/code-review.yaml`` in-memory over a DIFF — short orchestrator (preflight ->
    assemble -> run-and-finalize). OFF by default (INERT, no LLM); ``enabled=True`` force-enables it
    (Gerrit voter, WS6/ADR 0015). Outage/mid-run -> INDETERMINATE; sidecar only if target_ticket."""
    early = _code_review_preflight(request)
    if early is not None:
        return early
    prep = _assemble_code_review_run(request)
    return _run_code_review_gate(request, prep)


def _code_review_preflight(request: CodeReviewRequest) -> dict[str, Any] | None:
    """Enabled-check + runner preflight → an EARLY short-circuit verdict, or None to proceed."""
    from rebar.llm.runner import get_runner

    is_enabled = (
        code_review_enabled(request.repo_root) if request.enabled is None else request.enabled
    )
    if not is_enabled:
        return _inert_code_review_verdict()

    runner_sel = request.runner or get_runner(request.cfg)
    try:
        runner_sel.preflight()
    except LLMUnavailableError as exc:
        # Write-then-degrade (ticket 8bc5): additively capture the gate_error interval when a
        # ticket-addressed code review is running (the sidecar streams key on a ticket).
        if request.target_ticket:
            emit_gate_error(
                request.target_ticket, "code_review", cause=str(exc), repo_root=request.repo_root
            )
        return _degraded_code_review_verdict(error=exc, runner_name=runner_sel.name)
    return None


def _assemble_code_review_run(request: CodeReviewRequest) -> _CodeReviewPrep:
    """Assemble the diff context, scope-intent overlay, gate doc, recorder, and workflow inputs."""
    import time

    from rebar.llm.code_review import assemble

    from .recorder import MemoryRecorder

    dc = assemble.assemble_diff_context(
        base=request.base,
        head=request.head,
        diff_text=request.diff_text,
        changed_files=request.changed_files,
        repo_root=request.repo_root,
        commit_message=request.commit_message,
    )
    # scope-intent overlay (ONLY ticket-aware one): commit-trailer scope/AC, ONLY when >=1 resolved.
    context_overrides = {"code-review-scope-intent": dc.scope_context} if dc.scope_context else None
    doc = _gate_doc("code-review", request.repo_root)
    rec = MemoryRecorder()
    t_total = time.monotonic()
    inputs = {
        "base": request.base,
        "head": request.head,
        # Reuse the assembled diff (assemble_diff won't re-shell git diff) + thread commit_message.
        "diff_text": dc.diff_text,
        "changed_files": list(dc.changed_files),
        "commit_message": request.commit_message,
    }
    return _CodeReviewPrep(dc, doc, rec, inputs, context_overrides, t_total)


def _run_code_review_gate(request: CodeReviewRequest, prep: _CodeReviewPrep) -> dict[str, Any]:
    """Run the four-pass gate (snapshot session) + finalize; outage/mid-tail -> INDETERMINATE."""
    import time

    from rebar.llm import gate_source, review_kernel
    from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
    from rebar.llm.runner import get_runner

    from . import executor as _ex
    from .runs import RunnerAgentStep

    # SNAPSHOT GATE (raze-vet-ditch): run via gate_source (resolve/apply/gate_read_root) like every
    # code-reading gate. Attested pins a code snapshot AND a ticket-store clone — REQUIRED: reviewed
    # tickets live on the orphan `tickets` branch, else rebar tools fail. WS4 had dropped it.
    handle = gate_source.resolve_gate_handle(
        ref=request.head, source=request.source, repo_root=request.repo_root
    )
    cfg = gate_source.apply_handle(request.cfg, handle)
    # Rebuild the runner from the RE-ROOTED cfg (bug pelt-mead-aeon): the preflight runner baked the
    # pre-snapshot cfg; reusing it hits the bare clone (missing .tickets-tracker); injected kept.
    runner_sel = request.runner or get_runner(cfg)
    try:
        with gate_source.gate_read_root(handle), review_kernel.collect_contract_violations():
            res = _ex.run_workflow(
                prep.doc,
                prep.inputs,
                target_ticket=request.target_ticket,
                repo_root=request.repo_root,
                agent_runner=RunnerAgentStep(
                    runner=runner_sel, repo_root=request.repo_root, config=cfg
                ),
                batch_runner=CodeReviewBatchRunner(
                    context=prep.dc.context, context_overrides=prep.context_overrides
                ),
                recorder=prep.rec,
            )
    except LLMUnavailableError as exc:
        # Write-then-degrade (ticket 8bc5): same additive gate_error capture on the mid-run
        # infra outage, before preserving the soft-degrade.
        if request.target_ticket:
            emit_gate_error(
                request.target_ticket, "code_review", cause=str(exc), repo_root=request.repo_root
            )
        return _degraded_code_review_verdict(error=exc, runner_name=runner_sel.name)
    total_ms = round((time.monotonic() - prep.t_total) * 1000, 1)
    verdict = res.terminal_output
    if res.status == "succeeded" and isinstance(verdict, dict) and "verdict" in verdict:
        # Delegate the whole post-verdict finalization tail (metrics + WS5 fail-closed + deps +
        # region floor + durable emit) to the code_review/finalize.py strict leaf. Lazy import
        # matches this module's all-lazy cross-module import style.
        from rebar.llm.code_review import finalize as _finalize

        return _finalize.finalize_code_review_verdict(
            verdict,
            request=request,
            prep=prep,
            cfg=cfg,
            runner_sel=runner_sel,
            total_ms=total_ms,
        )
    return _degraded_code_review_verdict(
        error=(res.error or "code-review workflow LLM tier failed"),
        runner_name=runner_sel.name,
    )


# ── completion ──────────────────────────────────────────────────────────────────────
def produce_completion_verdict(
    ticket_id: str, *, graph: bool, repo_root=None, cfg, runner=None
) -> dict[str, Any]:
    """Produce a ``completion_verdict`` by running ``gates/completion-verification.yaml``.

    The verdict-production half of ``completion.verify_completion``. The caller has already
    tuned ``cfg`` (verifier model + step floor) and resolved ``graph``; the workflow's own
    ``completion_precheck`` op runs the deterministic child-closure check, then the agentic
    verify + reconcile produce the terminal verdict. Preflights and lets
    :class:`LLMUnavailableError` PROPAGATE so the close gate fail-closes."""
    from rebar.llm.config import gate_config
    from rebar.llm.runner import get_runner

    from . import executor as _ex
    from .recorder import MemoryRecorder
    from .runs import RunnerAgentStep

    runner_sel = get_runner(cfg, override=runner)
    try:
        runner_sel.preflight()  # raises LLMUnavailableError → close gate fail-closes (faithful)
    except LLMUnavailableError as exc:
        # Write-then-reraise (ticket 8bc5): capture the env/integration-diagnosis interval as a
        # dedicated gate_error_v1 sidecar, THEN re-raise so the close gate STILL fail-closes
        # (the propagation is preserved — we never swallow it).
        emit_gate_error(ticket_id, "completion", cause=str(exc), repo_root=repo_root)
        raise

    # The completion gate is self-contained: `completion_precheck` runs the deterministic
    # child-closure gate, then assembles the verifier's fenced ticket context — HONORING the
    # caller-resolved `graph`. The close gate passes graph=False so an epic close verifies its OWN
    # completion criteria, not its whole descendant subtree (children are trusted via their
    # certified closure, not re-verified). Thread it through so the precheck no longer re-derives
    # graph by ticket type — that override made an epic close re-verify every descendant and blew
    # the step budget (see the step-floor history in completion.py).
    doc = _gate_doc("completion-verification", repo_root)
    # Publish the caller-resolved cfg for the run so the completion ops (precheck child-failure,
    # reconcile) read the SAME config via resolve_gate_config, not a per-op from_env (586c).
    with gate_config(cfg):
        res = _ex.run_workflow(
            doc,
            {"ticket_id": ticket_id, "graph": bool(graph)},
            target_ticket=ticket_id,
            repo_root=repo_root,
            agent_runner=RunnerAgentStep(runner=runner_sel, repo_root=repo_root, config=cfg),
            recorder=MemoryRecorder(),
        )
    verdict = res.terminal_output
    if res.status != "succeeded" or not isinstance(verdict, dict) or "verdict" not in verdict:
        # The verifier failed mid-run — fail closed (never a silent PASS). Raise so the
        # close gate blocks, mirroring the bespoke path's raise-on-failed-run.
        from rebar.llm.errors import LLMError

        raise LLMError(
            f"completion verification workflow did not produce a verdict: "
            f"{res.error or 'LLM tier failed'}"
        )
    return verdict


__all__ = [
    "CodeReviewRequest",
    "produce_plan_review_verdict",
    "produce_completion_verdict",
]
