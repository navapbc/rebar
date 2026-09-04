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

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic_ns
from typing import Any, NamedTuple

# Back-compat re-export (load-bearing): tests/unit/test_code_review_fp_ledger.py calls
# ``gate_dispatch._attach_code_review_metrics`` at 7 sites. The code-review finalization cluster
# moved to the code_review/finalize.py strict leaf; this keeps the attribute resolving here.
from rebar.llm.code_review.finalize import (  # noqa: F401
    CODE_REVIEW_STEP_IDS,
    _attach_code_review_metrics,
)
from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError
from rebar.llm.gate_error_sidecar import emit_gate_error
from rebar.llm.run_identity import with_identity
from rebar.llm.tracing import run_span
from rebar.llm.workflow import plan_review_recovery as _plan_review_recovery
from rebar.llm.workflow.completion_metrics import (  # noqa: F401
    WORKFLOW_STEP_IDS,
    _add_phase,
    _attach_completion_metrics,
    _sum_run_consumption,
    attach_completion_workflow_phases,
)


def _gate_doc(name: str, repo_root) -> dict[str, Any]:
    """Load a packaged gate workflow (``src/rebar/llm/workflow/gates/<name>.yaml``).

    The gate workflows are PACKAGE data, not under ``.rebar/workflows`` / ``examples``,
    so they are resolved by absolute path (not via the name-resolver)."""
    from .runs import load_workflow_doc

    p = Path(__file__).resolve().parent / "gates" / f"{name}.yaml"
    return load_workflow_doc(p, repo_root)


# ── plan-review ───────────────────────────────────────────────────────────────────


def produce_plan_review_verdict(
    ctx,
    cfg,
    *,
    runner=None,
    advisory_cap: int,
    repo_root=None,
    probe_criteria=None,
    prerequisite_blocks=None,
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

    from rebar.llm import review_kernel
    from rebar.llm.config import gate_config
    from rebar.llm.plan_review import generation
    from rebar.llm.plan_review.context_assembly import assemble_context_cache
    from rebar.llm.plan_review.pass1 import material_fingerprint
    from rebar.llm.plan_review.prerequisites import focused_inputs
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
    from rebar.llm.runner import get_runner
    from rebar.llm.step_failures import collect_step_failures

    from . import executor as _ex
    from .recorder import MemoryRecorder
    from .runs import RunnerAgentStep

    runner_sel = runner or get_runner(cfg)
    try:
        runner_sel.preflight()
    except LLMUnavailableError as exc:
        # Write-then-degrade (ticket 8bc5): capture the env/integration-diagnosis interval as a
        # dedicated gate_error_v1 sidecar, THEN preserve the existing soft-degrade outcome.
        # No consumed counters (df94, Part 2): this is the PREFLIGHT outage — it raises before
        # the run and before any recorder exists (`rec` is created only past this point), so
        # zero billable work has happened; there is legitimately nothing consumed to record.
        emit_gate_error(ctx.ticket_id, "plan_review", cause=str(exc), repo_root=repo_root)
        return _plan_review_recovery._degraded_plan_review_verdict(
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
    _validate_gate_step_ids = _plan_review_recovery._validate_gate_step_ids
    # Catch a step-id rename in gates/plan-review.yaml LOUDLY here — before the billable run —
    # rather than letting a recovery lookup silently return None and degrade to INDETERMINATE.
    _validate_gate_step_ids(
        doc, _plan_review_recovery._PLAN_REVIEW_REQUIRED_STEP_IDS, gate_name="plan-review"
    )
    rec = MemoryRecorder()
    _t_total = time.monotonic()
    cancel: Any = None
    # One run-scoped assemble_context memo for the whole workflow: the four plan-review ops
    # (precheck / assemble_criteria / verify_inputs / coach_inputs) each call assemble_context
    # with the SAME (ticket_id, repo_root) inside this run, so the cache collapses their N+1
    # graph reads to a single read (and returns an identical PlanContext, so verdict bytes are
    # unchanged). The scope is dropped on exit — it never leaks across runs/tickets.
    try:
        # Resolve the caller's config ONCE for the whole run: gate_config publishes `cfg` so every
        # op (and the non-step ProductionBatchRunner) reads the SAME resolved config via
        # resolve_gate_config instead of re-deriving from env (epic veiny-trout-brink).
        with (
            # FIRST in the tuple, so this span is already current when the `with_identity(...)`
            # ARGUMENT below is evaluated — mint_run_identity then READS this run's trace id
            # instead of minting a fresh one, and the whole run is one trace.
            run_span("review-plan", ticket_id=ctx.ticket_id),
            assemble_context_cache(),
            review_kernel.collect_contract_violations(),
            # Tally LLM step calls that fail but do not fail the run, so repeated silent
            # degradation (the motivating case: every overlap-judge batch dying) is visible on
            # the verdict coverage instead of only in the logs. Additive observability — see
            # rebar.llm.step_failures.
            collect_step_failures(),
            gate_config(with_identity(cfg, ctx.ticket_id, "review-plan")),
            focused_inputs(list(prerequisite_blocks or [])),
            # Mid-run cancellation (story 2c89): a run-scoped token the seam probes
            # (plan_review_verify_inputs / plan_review_coach_inputs) and the Pass-1 chunk
            # funnel read. The baseline is the SAME own-material hash the sign-time
            # re-check compares (initial_generation.own_material).
            generation.cancel_scope(
                ctx.ticket_id, material_fingerprint(ctx), repo_root=repo_root
            ) as cancel,
        ):
            res = _ex.run_workflow(
                doc,
                {
                    "ticket_id": ctx.ticket_id,
                    "probe_criteria": list(probe_criteria or []),
                    "subject_plan": ctx.plan_text,
                    "prerequisites": list(prerequisite_blocks or []),
                },
                target_ticket=ctx.ticket_id,
                repo_root=repo_root,
                agent_runner=RunnerAgentStep(runner=runner_sel, repo_root=repo_root, config=cfg),
                batch_runner=ProductionBatchRunner(runner=runner_sel),
                recorder=rec,
            )
    except (LLMUnavailableError, LLMInputRejectedError) as exc:
        # LLMInputRejectedError (bug 43d4) joins this arm: this try block has NO broad
        # `except Exception`/`except LLMError`, so the new type would ESCAPE and take the
        # gate_error_v1 sidecar plus the degraded-verdict contract with it.
        # Write-then-degrade (ticket 8bc5): same additive gate_error capture on the mid-run
        # infra outage, before preserving the soft-degrade.
        # Consumed counters (df94, Part 2): the `rec` recorder IS in scope here and may hold
        # finder/verify steps that ran (and recorded `_usage`) BEFORE the outage struck, so
        # thread what was spent so far onto the diagnostic (None when the outage hit at/near the
        # first call, i.e. nothing was consumed — then it emits without one).
        emit_gate_error(
            ctx.ticket_id,
            "plan_review",
            cause=str(exc),
            diagnostic=_consumed_diagnostic(rec),
            repo_root=repo_root,
        )
        return _plan_review_recovery._degraded_plan_review_verdict(
            ctx, cfg, error=exc, advisory_cap=advisory_cap, runner_name=runner_sel.name
        )
    except generation.PlanReviewCancelledStale:
        # The batch (finders) path propagates step exceptions RAW (interpreter._run_batch
        # has no try around runner.run), unlike scripted ops (captured in-band) — accept
        # both routes into the same cancelled verdict.
        return _plan_review_recovery._cancelled_plan_review_verdict(ctx, cfg, scope=cancel)
    total_ms = round((time.monotonic() - _t_total) * 1000, 1)

    # Mid-run cancellation (story 2c89): checked BEFORE the recovery reconstructions
    # below — a cancelled run must yield the cancelled INDETERMINATE, never a verdict
    # "recovered" from the pre-edit passes (which review_plan would sign/sidecar).
    if cancel is not None and cancel.event.is_set():
        return _plan_review_recovery._cancelled_plan_review_verdict(ctx, cfg, scope=cancel)

    verdict = res.terminal_output
    if res.status == "succeeded" and isinstance(verdict, dict) and "verdict" in verdict:
        _plan_review_recovery._attach_plan_review_metrics(verdict, rec, total_ms)
        return verdict

    # A criteria/configuration parse fault is a local deterministic failure, not an LLM outage.
    # The assemble step carries this exact exception identity through the generic interpreter as
    # a stable structured marker; discriminate it before any source-blind degraded fallback.
    criteria_config_error = _plan_review_recovery._criteria_config_failure(rec)
    if criteria_config_error is not None:
        return _plan_review_recovery._config_fault_plan_review_verdict(
            ctx,
            cfg,
            error=criteria_config_error,
            advisory_cap=advisory_cap,
            runner_name=runner_sel.name,
        )

    # The run failed mid-tail. Pass-4 coach is advisory POLISH — bespoke run_review treats a
    # coach failure as NON-fatal (it still emits the verdict, sans coaching). Mirror that: if
    # Pass-3 `decide` succeeded (so finders+verify ran), reconstruct the verdict from the
    # decide partition with empty coaching — NOT a hollow INDETERMINATE that would discard the
    # real findings and wrongly block the claim.
    recovered = _plan_review_recovery._recover_plan_review_coach_failure(rec, cfg, error=res.error)
    if recovered is not None:
        _plan_review_recovery._attach_plan_review_metrics(recovered, rec, total_ms)
        return recovered

    # Pass-2 verify failed but Pass-1 finders SUCCEEDED (e.g. the agentic verifier exhausted its
    # step budget on a finding-rich ticket — bug 59bc). The LLM tier WAS available and produced
    # findings; treating that as a systemic outage discards them and (fail-closed) wrongly blocks
    # the claim. Recover: preserve the Pass-1 findings as unverified → INDETERMINATE, and let
    # finalize_verdict fail-OPEN unless a preserved finding is on a blocking-enabled criterion.
    recovered = _plan_review_recovery._recover_plan_review_verify_failure(rec, cfg, error=res.error)
    if recovered is not None:
        _plan_review_recovery._attach_plan_review_metrics(recovered, rec, total_ms)
        return recovered

    # finders failed (the LLM tier did not produce findings) — degrade to INDETERMINATE,
    # never sign a hollow PASS, mirroring run_review's broad-except → llm_unavailable path.
    return _plan_review_recovery._degraded_plan_review_verdict(
        ctx,
        cfg,
        error=(res.error or "plan-review workflow LLM tier failed"),
        advisory_cap=advisory_cap,
        runner_name=runner_sel.name,
    )


# ── code-review (epic b744 / WS4) ─────────────────────────────────────────────────────
# The code-review gate reuses STEP_VERIFY/STEP_DECIDE. Its post-verdict finalization cluster —
# metrics/deps/novelty-floor/session-artifact emit, plus the STEP_ASSEMBLE_DIFF step id and
# _attach_code_review_metrics (re-exported above) — lives in the code_review/finalize.py leaf.


def code_review_enabled(repo_root=None) -> bool:
    """Whether code-review DISPATCH is enabled (``verify.enable_code_review``) — consulted only
    when a :class:`CodeReviewRequest` leaves ``enabled=None``. The explicit ``review_code``
    surface always passes ``enabled=True`` (bug 5b32-37c4-f99a-4315)."""
    from rebar import config as _config

    try:
        return bool(_config.compose_config(repo_root).verify.enable_code_review)
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
    map a retryable code-review outage → exit 11 the same way plan-review does.

    NO ``provider_provenance`` here, deliberately (task e951, mirroring 343b's three no-record
    sites): ``coverage.llm_unavailable`` means no provider record accompanies this verdict. This
    site holds ``cfg``, so deriving one from ``cfg.model`` is the obvious move and it is WRONG —
    nothing ran, and a cfg-derived record would make the verdict claim a provider served it when
    none did, the exact misattribution the record exists to remove. The sidecar tolerates absence.
    """
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
            **_failure.degrade_cause_flags(error),
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
    # One rebar-owned root span for the whole multi-pass fan-out, so its per-candidate spans
    # nest into ONE trace. No `with_identity` here: review-code has no ticket, so it gains the
    # grouping but not header correlation — its `${run:...}` headers are omitted by design.
    with run_span("review-code", ticket_id=request.target_ticket):
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
        # No consumed counters (df94, Part 2): the PREFLIGHT outage raises before the run is
        # assembled and before any recorder exists (`prep.rec` is built downstream in
        # `_assemble_code_review_run`), so no billable work has happened — legitimately none.
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
    _validate_gate_step_ids = _plan_review_recovery._validate_gate_step_ids
    # Same guard the plan-review dispatch gets at :154 — catch a step-id rename in
    # gates/code-review.yaml LOUDLY, before the billable run, instead of letting
    # finalize's lookups silently return None (mirror F13).
    _validate_gate_step_ids(doc, CODE_REVIEW_STEP_IDS, gate_name="code-review")
    rec = MemoryRecorder()
    t_total = time.monotonic()
    inputs = {
        "base": request.base,
        "head": request.head,
        # Reuse the assembled diff (assemble_diff won't re-shell git diff) + thread commit_message.
        "diff_text": dc.diff_text,
        "changed_files": list(dc.changed_files),
        "commit_message": request.commit_message,
        # Carry-forward (story nitro-zombie-mealworm): the standing findings of the PRIOR patchset
        # under this change/session. Resolved HERE because this is the only site that holds the
        # memory key's ingredients (session_id / change_id); the workflow itself stays key-blind.
        "standing_findings": _standing_findings(request),
    }
    return _CodeReviewPrep(dc, doc, rec, inputs, context_overrides, t_total)


def _standing_findings(request: CodeReviewRequest) -> list[dict[str, Any]]:
    """The prior patchset's carriable findings for this request's memory key — ``[]`` when there
    is no key, no prior review, or nothing eligible. Best-effort: `standing_items` never raises."""
    from rebar.llm.code_review import carry_forward, sidecar

    key = sidecar.memory_key(request.session_id, request.change_id)
    if not key:
        return []
    return carry_forward.standing_items(key, repo_root=request.repo_root)


def _activated_code_review_project_criteria(
    repo_root: str | None, changed_files: Sequence[str] = ()
) -> tuple[dict[str, str], ...]:
    """Resolve active project-owned code-review criteria to their physical prompt ids.

    The shared overlay registry carries logical ids (``project.<name>``), while the prompt
    library uses gate-qualified filesystem-safe ids. Keeping that translation here gives the
    batch runner both forms: the physical id drives the prompt, and the logical id remains on
    emitted findings for routing and user-visible attribution.

    A criterion declaring ``applies_to`` globs is dropped when NO changed file matches (bug
    d343-47c6 — the key used to be stored and never read, so the criterion ran on every
    review). ``changed_files`` is the SAME list the ``triggers`` step glob-matches, so both
    trigger paths agree. An empty/absent ``applies_to`` stays ungated.
    """
    from rebar.llm.code_review.registry import (
        effective_criteria,
        effective_routing,
        project_criterion_applies,
    )
    from rebar.llm.criteria.ids import criterion_prompt_id

    routing = effective_routing(repo_root)
    return tuple(
        {
            "criterion_id": criterion_id,
            "prompt": criterion_prompt_id(criterion_id, gate_key="code_review"),
        }
        for criterion_id in effective_criteria(repo_root)
        if criterion_id.startswith("project.")
        and str((routing.get(criterion_id) or {}).get("exec", "1-TURN")).upper() != "DET"
        and project_criterion_applies(criterion_id, changed_files, repo_root)
    )


def _run_code_review_gate(request: CodeReviewRequest, prep: _CodeReviewPrep) -> dict[str, Any]:
    """Run the four-pass gate in a snapshot session, then finalize.

    Systemic runtime outages degrade to ``INDETERMINATE``. Project configuration errors,
    including invalid project-criterion prompts, fail loudly so they cannot silently remove
    review coverage.
    """
    import time

    from rebar.llm import gate_source, review_kernel
    from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
    from rebar.llm.runner import get_runner
    from rebar.llm.step_failures import collect_step_failures

    from . import executor as _ex
    from .runs import RunnerAgentStep

    # SNAPSHOT GATE (raze-vet-ditch): run via gate_source (resolve/apply/gate_read_root) like every
    # code-reading gate. Attested pins a code snapshot AND a ticket-store clone — REQUIRED: reviewed
    # tickets live on the orphan `tickets` branch, else rebar tools fail. WS4 had dropped it.
    handle = gate_source.resolve_gate_handle(
        ref=request.head, source=request.source, repo_root=request.repo_root
    )
    cfg = gate_source.apply_handle(request.cfg, handle)
    if handle.source == gate_source.SOURCE_LOCAL:
        cfg = replace(cfg, repo_path=str(handle.path))
    execution_repo_root = cfg.repo_path
    # Rebuild the runner from the RE-ROOTED cfg (bug pelt-mead-aeon): the preflight runner baked the
    # pre-snapshot cfg; reusing it hits the bare clone (missing .tickets-tracker); injected kept.
    runner_sel = request.runner or get_runner(cfg)
    try:
        with (
            gate_source.gate_read_root(handle),
            review_kernel.collect_contract_violations(),
            # Same survived-LLM-step-failure tally the plan-review scope activates above: a
            # non-fatal sub-call dying is otherwise visible only in the logs.
            collect_step_failures(),
        ):
            res = _ex.run_workflow(
                prep.doc,
                prep.inputs,
                target_ticket=request.target_ticket,
                repo_root=execution_repo_root,
                agent_runner=RunnerAgentStep(
                    runner=runner_sel, repo_root=request.repo_root, config=cfg
                ),
                batch_runner=CodeReviewBatchRunner(
                    context=prep.dc.context,
                    context_overrides=prep.context_overrides,
                    project_criteria=_activated_code_review_project_criteria(
                        execution_repo_root, prep.dc.changed_files
                    ),
                    # Thread the ONE resolved (possibly re-rooted snapshot) root through BOTH
                    # discovery and rubric resolution, so the runner's agreement check can
                    # catch a future divergence instead of it surfacing as "unknown prompt".
                    project_criteria_root=execution_repo_root,
                ),
                recorder=prep.rec,
            )
    except (LLMUnavailableError, LLMInputRejectedError) as exc:
        # LLMInputRejectedError (bug 43d4) joins this arm: this try block has NO broad
        # `except Exception`/`except LLMError`, so the new type would ESCAPE and take the
        # gate_error_v1 sidecar plus the degraded-verdict contract with it.
        # Write-then-degrade (ticket 8bc5): same additive gate_error capture on the mid-run
        # infra outage, before preserving the soft-degrade.
        # Consumed counters (df94, Part 2): `prep.rec` IS in scope and may hold batch/agent
        # review steps that ran (recording `_usage`) before the outage, so thread the
        # consumed-so-far counters onto the diagnostic (None when nothing was consumed yet).
        if request.target_ticket:
            emit_gate_error(
                request.target_ticket,
                "code_review",
                cause=str(exc),
                diagnostic=_consumed_diagnostic(prep.rec),
                repo_root=request.repo_root,
            )
        return gate_source.annotate_result(
            _degraded_code_review_verdict(error=exc, runner_name=runner_sel.name), handle
        )
    total_ms = round((time.monotonic() - prep.t_total) * 1000, 1)
    verdict = res.terminal_output
    if res.status == "succeeded" and isinstance(verdict, dict) and "verdict" in verdict:
        # Delegate the whole post-verdict finalization tail (metrics + WS5 fail-closed + deps +
        # region floor + durable emit) to the code_review/finalize.py strict leaf. Lazy import
        # matches this module's all-lazy cross-module import style.
        from rebar.llm.code_review import finalize as _finalize

        # Stamp the provenance of the handle the review ACTUALLY ran under (source /
        # verified_at_sha / signable), like every other code-reading gate — the
        # `review_code` shim propagates it onto the returned `review_result`, which is what
        # decides whether the review can be signed.
        return gate_source.annotate_result(
            _finalize.finalize_code_review_verdict(
                verdict,
                request=request,
                prep=prep,
                cfg=cfg,
                runner_sel=runner_sel,
                total_ms=total_ms,
            ),
            handle,
        )
    return gate_source.annotate_result(
        _degraded_code_review_verdict(
            error=(res.error or "code-review workflow LLM tier failed"),
            runner_name=runner_sel.name,
        ),
        handle,
    )


def _consumed_diagnostic(rec: Any) -> dict[str, Any] | None:
    """Best-effort CONSUMED counters for a mid-run gate_error sidecar (df94, Part 2).

    A mid-run :class:`LLMUnavailableError` can strike AFTER some agent/batch steps already ran
    and recorded their ``_usage``; this sums the ``requests``/``tool_calls`` consumed so far off
    the in-scope recorder so the gate_error record carries what was spent before the outage (not
    only the ceiling). Sums each succeeded step's aggregate ``_usage`` and any per-call records a
    batch step carries. Returns None when NO step recorded usage (the outage struck at/near the
    first call — nothing was consumed), in which case the caller emits without a diagnostic."""
    requests = 0
    tool_calls = 0
    saw_usage = False
    for s in getattr(rec, "steps", []) or []:
        if not isinstance(s, dict) or s.get("status") != "succeeded":
            continue
        outputs = s.get("outputs")
        outputs = outputs if isinstance(outputs, dict) else {}
        usage = outputs.get("_usage")
        if not isinstance(usage, dict) or not usage:
            continue
        per_call = usage.get("per_call")
        if isinstance(per_call, list) and per_call:
            for call in per_call:
                if isinstance(call, dict):
                    saw_usage = True
                    requests += int(call.get("requests") or 0)
                    tool_calls += int(call.get("tool_calls") or 0)
        else:
            saw_usage = True
            requests += int(usage.get("requests") or 0)
            tool_calls += int(usage.get("tool_calls") or 0)
    if not saw_usage:
        return None
    return {"requests": requests, "tool_calls": tool_calls}


# ── completion ──────────────────────────────────────────────────────────────────────
def produce_completion_verdict(
    ticket_id: str,
    *,
    graph: bool,
    repo_root=None,
    cfg,
    runner=None,
    verify_ref=None,
    phase_metrics: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Produce a ``completion_verdict`` by running ``gates/completion-verification.yaml``.

    The verdict-production half of ``completion.verify_completion``. The caller has already
    tuned ``cfg`` (verifier model + step floor) and resolved ``graph``; the workflow's own
    ``completion_precheck`` op runs the deterministic child-closure check, then the agentic
    verify + reconcile produce the terminal verdict. Preflights and lets
    :class:`LLMUnavailableError` PROPAGATE so the close gate fail-closes."""
    dispatch_started_ns = monotonic_ns() if phase_metrics is not None else 0
    from rebar.llm.config import gate_config
    from rebar.llm.runner import get_runner
    from rebar.llm.step_failures import collect_step_failures

    from . import executor as _ex
    from .completion_recovery import CompletionAgentStep, raise_completion_workflow_failure
    from .recorder import MemoryRecorder

    runner_sel = get_runner(cfg, override=runner)
    try:
        runner_sel.preflight()  # raises LLMUnavailableError → close gate fail-closes (faithful)
    except LLMUnavailableError as exc:
        # Write-then-reraise (ticket 8bc5): capture the env/integration-diagnosis interval as a
        # dedicated gate_error_v1 sidecar, THEN re-raise so the close gate STILL fail-closes
        # (the propagation is preserved — we never swallow it).
        # No consumed counters (df94, Part 2): the PREFLIGHT outage raises before the run and
        # before the recorder exists (`recorder` is created only past this point), so no billable
        # work has happened. The MID-run completion outage is handled separately in
        # completion_recovery.raise_completion_workflow_failure, which DOES carry the consumed
        # requests/tool_calls onto its gate_error diagnostic.
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
    _validate_gate_step_ids = _plan_review_recovery._validate_gate_step_ids
    # Mirror F13. This gate degrades even more quietly than the other two: a renamed
    # step falls through completion_metrics' mapping into the "unclassified" bucket,
    # so the timing is silently mis-filed and nothing errors.
    _validate_gate_step_ids(doc, WORKFLOW_STEP_IDS, gate_name="completion-verification")
    # Publish the caller-resolved cfg for the run so the completion ops (precheck child-failure,
    # reconcile) read the SAME config via resolve_gate_config, not a per-op from_env (586c).
    completion_step = CompletionAgentStep(
        runner=runner_sel,
        repo_root=repo_root,
        config=cfg,
        verify_ref=verify_ref,
    )
    recorder = MemoryRecorder()
    if phase_metrics is not None:
        _add_phase(
            phase_metrics, "verifier_dispatch_setup_ms", monotonic_ns() - dispatch_started_ns
        )
        workflow_started_ns = monotonic_ns()
    _t_total = time.monotonic()
    # collect_step_failures wraps the WHOLE run so the reconcile op's drain can see failures
    # from the verify agent step; see completion_reconcile for where the tally lands.
    # run_span FIRST: it must ENCLOSE the `gate_config(with_identity(...))` statement, because
    # `with_identity(...)` is evaluated as an ARGUMENT and so runs before that item is entered.
    with (
        run_span("verify-completion", ticket_id=ticket_id),
        gate_config(with_identity(cfg, ticket_id, "verify-completion")),
        collect_step_failures(),
    ):
        res = _ex.run_workflow(
            doc,
            {"ticket_id": ticket_id, "graph": bool(graph)},
            target_ticket=ticket_id,
            repo_root=repo_root,
            agent_runner=completion_step,
            recorder=recorder,
        )
    total_ms = round((time.monotonic() - _t_total) * 1000, 1)
    if phase_metrics is not None:
        attach_completion_workflow_phases(
            phase_metrics, recorder.steps, monotonic_ns() - workflow_started_ns
        )
        finalization_started_ns = monotonic_ns()
    verdict = res.terminal_output
    if res.status != "succeeded" or not isinstance(verdict, dict) or "verdict" not in verdict:
        # The verifier failed mid-run — fail closed (never a silent PASS). Raise so the
        # close gate blocks, mirroring the bespoke path's raise-on-failed-run.
        raise_completion_workflow_failure(
            ticket_id, res, completion_step.failure_diagnostic, len(recorder.steps), repo_root
        )
    _attach_completion_metrics(verdict, recorder, total_ms)
    if phase_metrics is not None:
        _add_phase(
            phase_metrics,
            "verifier_dispatch_finalization_ms",
            monotonic_ns() - finalization_started_ns,
        )
        phase_metrics["verifier_workflow_step_count"] = phase_metrics.get(
            "verifier_workflow_step_count", 0
        ) + len(recorder.steps)
    return verdict


__all__ = [
    "CodeReviewRequest",
    "produce_completion_verdict",
    "produce_plan_review_verdict",
]
