"""Completion-verification operation: verify a ticket's completion requirements are met.

``verify_completion(ticket_id)`` runs a tool-using LLM agent (the ``completion-verifier``
reviewer) that checks every completion requirement on a ticket — acceptance/success/close
criteria, definitions of done, and (for bugs) that the bug is resolved — is demonstrably
satisfied by the implementation, and returns a **``completion_verdict``** (``{verdict, findings,
…}``). The agent is read-only: line-numbered repo file tools plus a read-only rebar
``show_ticket`` tool; it never writes, transitions, signs, or closes.

Like the review ops, this owns the **deterministic** parts (assembling the ticket context from
rebar's own reads, resolving the reviewer prompt, picking the runner) and delegates the agent
run to a :class:`~rebar.llm.runner.Runner`. The structured-output **contract** is selected by
``output_schema="completion_verdict"`` (the pluggable-contract seam). The agent emits the
verdict; the operation then deterministically normalizes/reconciles it (the verdict is the
agent's, with a guardrail — see :func:`reconcile_verdict`) and resolves citations against the repo.

Optionality: stdlib-only at import; the agent stack is lazy-imported by the runner. The
pydantic_ai runner provides ``show_ticket`` natively (pai_tools.rebar_tools), so the verifier
needs no injected ticket tool.
"""

from __future__ import annotations

from dataclasses import replace
from time import monotonic_ns
from typing import Any

from rebar.llm.completion_child_gate import (
    build_child_closure_evidence,
    child_closure_findings,
)
from rebar.llm.completion_reconcile import (
    COMPLETION_REMEDIATION_GUIDANCE,
    INSUFFICIENT_EVIDENCE_REMEDIATION,
    completion_fail_returncode,
    deterministic_child_failure,
    reconcile_verdict,
)
from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.runner import Runner

# Public seam: these deterministic helpers are the completion gate's stable API, consumed by
# the workflow gate ops (rebar.llm.workflow.gate_ops) and external callers. They are exported
# (not leading-underscore privates) so a MANDATORY gate does not depend on another module's
# underscore-privates — a rename here is a visible contract change, not a silent break. Their
# implementations live in the focused modules this operation composes (completion_child_gate,
# completion_reconcile); this module re-exports them so the seam's import path is stable.
__all__ = [
    "COMPLETION_REMEDIATION_GUIDANCE",
    "INSUFFICIENT_EVIDENCE_REMEDIATION",
    "build_child_closure_evidence",
    "capture_completion_ticket_view",
    "child_closure_findings",
    "completion_fail_returncode",
    "deterministic_child_failure",
    "reconcile_verdict",
    "verify_completion",
]

# Bounded completion verification wants a DECISIVE model, not a maximally-thorough one: the
# framework default (opus) over-explores — it rabbit-holes on confirming code is "wired",
# blowing the step budget even on a 2-criterion ticket (it tripped recursion_limit=300 / 385s
# in testing) — whereas sonnet converges in ~12s. So default the verifier to sonnet (matching
# the DSO completion-verifier's `model: sonnet`). An operator who EXPLICITLY sets a
# non-default `[tool.rebar.llm].model` still wins (below). The literal lives in config.py
# (VERIFIER_DEFAULT_MODEL) as the single source shared with the plan-review verifier.
_VERIFIER_DEFAULT_MODEL = VERIFIER_DEFAULT_MODEL
# Completion verification is inherently more tool-heavy than a single-dimension review; the
# framework default (REBAR_LLM_MAX_STEPS=50 ≈ 25 tool calls) trips the recursion cap
# mid-verification (a false fail-closed block). A FLAT 480 floor manufactured exhaustion
# (epic 10ae/story 2948); the criteria-scaled floor replaced it, and ticket 8d74 RECALIBRATED
# it after live false unmets: runaway is already separately guarded (tool_calls_limit, loop
# detection), so the floor is generous and scales with the evidence surface rather than
# limiting authorized validation — the clamp below is a runaway ceiling, not a validation cap.
# The floor is AUTHORITATIVE over the framework default (it may LOWER a small ticket below the
# 250 default) but min-only against an explicit operator budget. Per-run step usage is logged
# by the runner (`… steps=N/limit`) so a resize can be sized from observed headroom.
_VERIFY_STEP_FLOOR_MAX = 960


def _record_elapsed(metrics: dict[str, int] | None, key: str, started_ns: int) -> None:
    if metrics is not None:
        metrics[key] = metrics.get(key, 0) + (monotonic_ns() - started_ns) // 1_000_000


def _pinned_ticket_view_selection(repo_root) -> tuple[bool, str | None]:
    """Resolve the experimental read mode once, before either snapshot is captured.

    The first rollout requires synchronous delivery because its receipt-aware remote
    reconciliation is part of the close operation.  ``async`` cannot safely hand the
    certified bundle to the generic background push path, and ``off`` cannot observe a
    remote conflict.  Both therefore retain the legacy materialized path rather than
    capturing a tickets OID and changing strategy midway through a run.
    """
    try:
        from rebar import config as _root_config

        requested = bool(
            _root_config.compose_config(repo_root).verify.completion_pinned_ticket_view
        )
        if not requested:
            return False, None
        push_mode = _root_config.resolve_push_mode(repo_root)
    except Exception:  # noqa: BLE001 — unreadable rollout config keeps the legacy path
        return False, None
    if push_mode != "always":
        return False, f"materialized_push_{push_mode}"
    return True, "lazy_pinned"


def _capture_requested_ticket_view(
    ticket_id: str, repo_root, *, fetch: bool
) -> tuple[Any | None, str]:
    """Capture the requested immutable store once, with the documented epic back-out."""
    from rebar._snapshot.ticket_view import PinnedTicketView

    ticket_view = PinnedTicketView.try_capture(repo_root, fetch=fetch)
    if ticket_view is None:
        return None, "materialized_unavailable"
    try:
        ticket_type = ticket_view.show_ticket(ticket_id).get("ticket_type")
    except BaseException:
        ticket_view.close()
        raise
    if ticket_type != "epic":
        return ticket_view, "lazy_pinned"
    ticket_view.close()
    return None, "materialized_epic"


def capture_completion_ticket_view(
    ticket_id: str, repo_root, *, fetch: bool = False
) -> tuple[Any | None, str | None]:
    """Capture one caller-owned ticket session for completion-specific close checks."""
    requested, mode = _pinned_ticket_view_selection(repo_root)
    if not requested:
        return None, mode
    return _capture_requested_ticket_view(ticket_id, repo_root, fetch=fetch)


def _resolve_completion_ticket_view(
    handle: Any,
    *,
    lazy_requested: bool,
    ticket_read_mode: str | None,
    ticket_id: str,
    repo_root,
    fetch: bool,
    phase_metrics: dict[str, int] | None,
) -> tuple[Any, Any | None, str | None]:
    """Choose the immutable ticket read root once and return its recorded disposition."""
    from rebar.llm import gate_source

    if not lazy_requested:
        return handle, None, ticket_read_mode
    if handle.source != gate_source.SOURCE_ATTESTED:
        return handle, None, "materialized_local_source"

    ticket_view, captured_mode = _capture_requested_ticket_view(ticket_id, repo_root, fetch=fetch)
    if ticket_view is None:
        materialized = gate_source.attach_materialized_tickets(
            handle, repo_root=repo_root, fetch=fetch, phase_metrics=phase_metrics
        )
        return materialized, None, captured_mode
    return handle, ticket_view, captured_mode


def _attach_completion_read_basis(
    result: dict,
    ticket_view: Any,
    *,
    ticket_id: str,
    code_sha: str,
    repo_root,
    phase_metrics: dict[str, int] | None,
) -> None:
    """Bind the verdict to the immutable ticket predicates actually consulted."""
    from rebar._snapshot.ticket_view import CodeOID
    from rebar.llm.plan_review.attest import current_material_fingerprint

    # Computing material while the view is active records the root and direct-child
    # membership. The full descendant closure is an explicit deterministic close dependency
    # so a nested-ticket change cannot slip between the landing scan and publication.
    result["material_fingerprint"] = current_material_fingerprint(ticket_id, repo_root=repo_root)
    ticket_view.transitive_descendant_ids(ticket_id)
    result["completion_read_basis"] = ticket_view.completion_basis(CodeOID(code_sha)).to_dict()
    run_metrics = result.get("metrics")
    if isinstance(run_metrics, dict):
        run_metrics.update(ticket_view.metrics)
    if phase_metrics is not None:
        phase_metrics.update(ticket_view.metrics)


def _run_completion_at_handle(
    ticket_id: str,
    *,
    graph: bool | None,
    repo_root,
    config: LLMConfig | None,
    runner: Runner | None,
    phase_metrics: dict[str, int] | None,
    handle: Any,
    ticket_view: Any | None,
    ticket_read_mode: str | None,
) -> dict:
    """Execute and annotate one verifier run inside its paired code/ticket read roots."""
    from rebar.llm import gate_source
    from rebar.llm.config_binding import compose_and_bind_llm_config

    read_kwargs: dict[str, Any] = {"phase_metrics": phase_metrics}
    if ticket_view is not None:
        read_kwargs["ticket_view"] = ticket_view
    read_context = gate_source.gate_read_root(handle, **read_kwargs)
    with read_context, compose_and_bind_llm_config(repo_root=repo_root, explicit=config) as bound:
        started_ns = monotonic_ns()
        resolved_config = gate_source.apply_handle(bound, handle)
        if ticket_view is not None:
            resolved_config = replace(resolved_config, ticket_view=ticket_view)
        _record_elapsed(phase_metrics, "verifier_handle_apply_ms", started_ns)
        result = _verify_completion_inner(
            ticket_id,
            graph=graph,
            repo_root=repo_root,
            config=resolved_config,
            runner=runner,
            verify_ref=handle.sha,
            phase_metrics=phase_metrics,
        )
        started_ns = monotonic_ns()
        if ticket_read_mode is not None:
            result["ticket_read_mode"] = ticket_read_mode
        if ticket_view is not None:
            _attach_completion_read_basis(
                result,
                ticket_view,
                ticket_id=ticket_id,
                code_sha=handle.sha,
                repo_root=repo_root,
                phase_metrics=phase_metrics,
            )
        annotated = gate_source.annotate_result(result, handle)
        _record_elapsed(phase_metrics, "verifier_annotation_ms", started_ns)
        return annotated


def verify_step_floor(criteria_count: int, verify_cfg, direct_children: int = 0) -> int:
    """The evidence-surface-scaled PRIMARY completion-verifier step floor:
    ``clamp(steps_per_criterion × c + child_traversal × direct_children + fixed_overhead,
    step_floor_min, 960)``. Runaway prevention ONLY (ticket 8d74) — for valid tool use it is
    generous, scaling with the whole evidence surface: ``c`` explicit criteria (floored at 1),
    a traversal term per DIRECT child (epic criteria read child tickets), and a fixed
    show_ticket+parse overhead. Config keys (``verify.completion_verify_*``):
    ``steps_per_criterion`` 24, ``step_floor_min`` 160, ``child_traversal_steps`` and
    ``fixed_overhead_steps`` both 16."""
    per = verify_cfg.completion_verify_steps_per_criterion
    lo = verify_cfg.completion_verify_step_floor_min
    child = verify_cfg.completion_verify_child_traversal_steps
    overhead = verify_cfg.completion_verify_fixed_overhead_steps
    scaled = per * max(int(criteria_count), 1) + child * max(int(direct_children), 0) + overhead
    return max(lo, min(scaled, _VERIFY_STEP_FLOOR_MAX))


def _verifier_model_for_completion(repo_root: str | None = None) -> str:
    """The completion verifier's model: the STANDARD model class (ticket 172e).

    This file carried its OWN copy of plan-review's equality test
    (``if cfg.model == DEFAULT_MODEL: replace(model=_VERIFIER_DEFAULT_MODEL)``), so the same defect
    lived on a second path: ANY provider-qualified or Bedrock model id read as an explicit operator
    choice and left the completion verifier on the frontier model. Resolving the class keeps the two
    gates in step.

    With nothing configured, ``standard`` resolves to the same model ``_VERIFIER_DEFAULT_MODEL``
    names -- but the returned string is now PROVIDER-QUALIFIED, so this is not byte-identical to the
    old rule. See :func:`rebar.llm.plan_review._verifier_cfg` for why qualifying is the deliberate
    and desirable direction.

    A separate function rather than an inline call so the resolution is unit-testable without
    standing up a whole ``verify_completion`` run.

    ``repo_root`` is the root the class table is read from — the caller threads ``cfg.repo_path``
    so the verifier's model comes from the SAME root the config resolved against instead of from
    ambient cwd discovery (bug 2876). Left ``None`` it falls back to the active gate root, then
    ambient discovery, exactly as every other class read does.
    """
    from rebar.llm.model_classes import STANDARD_CLASS, resolve_model_string

    return resolve_model_string(STANDARD_CLASS, repo_root)


def verify_completion(
    ticket_id: str,
    *,
    graph: bool | None = None,
    ref: str | None = None,
    source: str | None = None,
    fetch: bool = True,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
    phase_metrics: dict[str, int] | None = None,
    ticket_view: Any | None = None,
    ticket_read_mode: str | None = None,
) -> dict:
    """Verify a ticket's completion requirements and return a ``completion_verdict`` dict.

    Args:
        ticket_id: the ticket to verify (id, short id, or alias).
        graph: include the ticket's descendants in the context. Default: ``True`` for an
            epic (its acceptance criteria are met across children), else ``False``.
        repo_root: rebar repo root (defaults to the resolved root).
        config: an :class:`LLMConfig` (defaults to :meth:`LLMConfig.from_env`).
        runner: an explicit runner (test seam; defaults to the config-selected runner).

    Returns a validated ``completion_verdict`` dict ``{verdict: "PASS"|"FAIL", findings[],
    summary?, target, reviewers, runner, model, trace_id}``. On FAIL, ``findings`` is
    non-empty; each finding carries the failing ``criterion``, an explanation (``detail``),
    and ``citations`` resolved against the real repo. Raises :class:`LLMError` subclasses on
    missing deps/credentials or a failed/empty structured run.
    """
    phase_started_ns = monotonic_ns()
    from rebar.llm import gate_source

    if ticket_read_mode is None:
        lazy_requested, ticket_read_mode = _pinned_ticket_view_selection(repo_root)
    else:
        lazy_requested = ticket_read_mode == "lazy_pinned"
    if ticket_view is not None and not lazy_requested:
        raise ValueError("a pinned ticket session requires completion_pinned_ticket_view")
    owns_ticket_view = False
    _record_elapsed(phase_metrics, "verifier_attempt_setup_ms", phase_started_ns)
    phase_started_ns = monotonic_ns()
    from rebar.llm.gate_admission import gate_admission

    # Concurrency admission (ADR 0112 decision 5) is taken BEFORE resolve_gate_handle,
    # because materializing the snapshot is what spends the bytes the cap bounds. ONE
    # counter is shared with review_plan: both copy the repo at a ref, and two caps of N
    # each would admit 2N holders. At capacity this RAISES GateCongestedError.
    with gate_admission("verify_completion", ticket_id, repo_root):
        handle = gate_source.resolve_gate_handle(
            ref,
            source,
            repo_root,
            fetch=fetch,
            phase_metrics=phase_metrics,
            materialize_ticket_store=not lazy_requested,
        )
        _record_elapsed(phase_metrics, "verifier_handle_resolution_ms", phase_started_ns)
        phase_started_ns = monotonic_ns()
        if ticket_view is None:
            handle, ticket_view, ticket_read_mode = _resolve_completion_ticket_view(
                handle,
                lazy_requested=lazy_requested,
                ticket_read_mode=ticket_read_mode,
                ticket_id=ticket_id,
                repo_root=repo_root,
                fetch=fetch,
                phase_metrics=phase_metrics,
            )
            owns_ticket_view = ticket_view is not None
        elif handle.source != gate_source.SOURCE_ATTESTED:
            raise ValueError("a pinned ticket session requires an attested completion source")
        else:
            ticket_read_mode = "lazy_pinned"
        if lazy_requested:
            _record_elapsed(phase_metrics, "verifier_ticket_view_setup_ms", phase_started_ns)
        from rebar.llm.peak_rss import gate_peak_rss

        try:
            # Measurement only (bug 9ea3): emits the GATE_PEAK_RSS marker on completion,
            # including on the raising paths. Wrapping HERE covers both the MCP daemon and
            # the CLI, which both reach the gate through this function.
            with gate_peak_rss("verify_completion", ticket_id):
                return _run_completion_at_handle(
                    ticket_id,
                    graph=graph,
                    repo_root=repo_root,
                    config=config,
                    runner=runner,
                    phase_metrics=phase_metrics,
                    handle=handle,
                    ticket_view=ticket_view,
                    ticket_read_mode=ticket_read_mode,
                )
        finally:
            if owns_ticket_view and ticket_view is not None:
                ticket_view.close()


def _verify_completion_inner(
    ticket_id: str,
    *,
    graph: bool | None,
    repo_root,
    config: LLMConfig,
    runner: Runner | None,
    verify_ref: str | None = None,
    phase_metrics: dict[str, int] | None = None,
) -> dict:
    phase_started_ns = monotonic_ns() if phase_metrics is not None else 0
    from rebar import _reads

    cfg = config
    cfg = replace(cfg, model=_verifier_model_for_completion(cfg.repo_path))
    # Model-max output budget for the PRIMARY verifier call (bug 30a2): applied AFTER the model
    # swap so the raise matches the model that actually runs; only ever raises, so an explicit
    # higher operator REBAR_LLM_MAX_TOKENS still wins.
    from rebar.llm.review_kernel import max_output_cfg

    cfg = max_output_cfg(cfg)
    # Pin GREEDY decoding for the verifier (bug e458): an unpinned temperature runs at the
    # provider default (~1.0), whose sampling variance flips borderline judgments — e.g. whether
    # the agent's (fallible, free-form) search located a criterion's test — between runs on
    # IDENTICAL input (proven: ad9f FAIL→PASS same-sha). Mirrors the plan-review Pass-2 verifier's
    # greedy pin; an explicit operator REBAR_LLM_TEMPERATURE (cfg.temperature not None) still wins,
    # exactly like the model / step-floor tuning above. This is a variance MITIGATION, not the root
    # fix — the prompt guidance (search by ticket-id/exact-symbols, not regex/semantic phrases)
    # addresses the mechanism directly.
    if cfg.temperature is None:
        cfg = replace(cfg, temperature=0.0)
    # Resolve the ticket type once (one local read; no network). graph default depends on
    # ticket type (epics verify across children).
    root = _reads.show_ticket(ticket_id, repo_root=repo_root)
    resolved_graph = root.get("ticket_type") == "epic" if graph is None else bool(graph)

    # Criteria-scaled PRIMARY step budget (epic 10ae/story 2948, lever 1). Compute the scaled
    # floor from the ticket's explicit criteria count, then apply it: it is AUTHORITATIVE over the
    # framework default (== DEFAULT_MAX_ITERATIONS means no explicit operator step budget, so the
    # scaled floor becomes the budget even when that LOWERS it — the whole point of lever 1), but
    # min-only against an EXPLICIT operator budget (a different value the operator set is only ever
    # raised up to the floor, never lowered). Config read is fail-safe: an unreadable config falls
    # back to the packaged VerifyConfig defaults so the floor still applies.
    from rebar import config as _config
    from rebar._config_schema import VerifyConfig
    from rebar.llm.config import DEFAULT_MAX_ITERATIONS
    from rebar.llm.errors import CompletionRecoveryError
    from rebar.llm.workflow.completion_criteria import explicit_completion_criteria

    try:
        verify_cfg = _config.compose_config(repo_root).verify
    except Exception:  # noqa: BLE001 — config unreadable → packaged defaults, floor still applies
        verify_cfg = VerifyConfig()
    # Lever-1 floor scales with the ticket's explicit checkbox count. A ticket with no
    # enumerable checkboxes (a non-bug without an Acceptance Criteria block) makes
    # `explicit_completion_criteria` fail closed — but that is the VERDICT PRODUCTION path's
    # concern (the agents-extra guard / child-closure precheck in produce_completion_verdict own
    # it), NOT this pre-flight budget sizing. Enumeration must not raise HERE, ahead of the
    # agents guard, or a lean (no-extras) install degrades to CompletionRecoveryError instead of
    # the typed missing-extra LLMError (regression caught by the degradation-path gate). Fall
    # back to a zero criteria count (base floor) and let the downstream path decide.
    try:
        criteria_count = len(explicit_completion_criteria(root))
    except CompletionRecoveryError:
        criteria_count = 0
    # Evidence-surface child term (ticket 8d74): epic criteria traverse DIRECT children; the
    # shared enumerator fails OPEN to the childless floor. Runtime import: no cycle at load.
    from rebar.llm.workflow.completion_verdict_cache import direct_child_count

    step_floor = verify_step_floor(
        criteria_count, verify_cfg, direct_children=direct_child_count(ticket_id, repo_root)
    )
    if cfg.max_iterations == DEFAULT_MAX_ITERATIONS or cfg.max_iterations < step_floor:
        cfg = replace(cfg, max_iterations=step_floor)

    # Verdict PRODUCTION runs through the v3 engine workflow
    # (gates/completion-verification.yaml) — which owns its OWN deterministic child-closure
    # precheck → agentic verify → reconcile — and returns the reconciled completion_verdict.
    # (The child-closure precheck is the workflow's `completion_precheck` op, which reuses
    # `child_closure_findings` / `deterministic_child_failure` from this module, so there is
    # exactly ONE child-closure implementation and no double check.) The close gate's signing
    # wrapper (_commands.transition) is unchanged; cfg is already tuned (model + floor) above.
    from rebar.llm.workflow import gate_dispatch

    if phase_metrics is None:
        return gate_dispatch.produce_completion_verdict(
            ticket_id,
            graph=resolved_graph,
            repo_root=repo_root,
            cfg=cfg,
            runner=runner,
            verify_ref=verify_ref,
        )
    phase_metrics["verifier_inner_setup_ms"] = (
        phase_metrics.get("verifier_inner_setup_ms", 0)
        + (monotonic_ns() - phase_started_ns) // 1_000_000
    )
    phase_started_ns = monotonic_ns()
    result = gate_dispatch.produce_completion_verdict(
        ticket_id,
        graph=resolved_graph,
        repo_root=repo_root,
        cfg=cfg,
        runner=runner,
        verify_ref=verify_ref,
        phase_metrics=phase_metrics,
    )
    phase_metrics["verifier_dispatch_ms"] = (
        phase_metrics.get("verifier_dispatch_ms", 0)
        + (monotonic_ns() - phase_started_ns) // 1_000_000
    )
    return result
