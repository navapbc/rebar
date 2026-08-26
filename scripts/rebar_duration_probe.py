#!/usr/bin/env python3
"""Reconstruct operator-visible rebar gate durations from Codex rollout logs.

The probe is read-only: it scans Codex JSONL rollouts and a rebar tracker, links yielded
cells and PTY polls to their originating shell command, then matches that interval to the
ticket's gate sidecar. Completion output also decomposes the close using sidecar metrics
and tracker event timestamps, explicitly distinguishing direct measurements from inferred
residuals.

Frozen 2026-08-23 analysis::

    python scripts/rebar_duration_probe.py summary \
      --tracker .tickets-tracker \
      --since 2026-08-07T12:34:40Z \
      --until 2026-08-23T21:15:00Z \
      --provider-prefix bedrock: \
      --current-since 2026-08-22T00:00:00Z

By default the log roots are ``~/.codex/sessions`` and
``~/.codex/archived_sessions`` and the tracker is ``.tickets-tracker``. Time bounds and
provider filtering are opt-in. Malformed records are counted and skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import rebar_duration_probe_support as support  # noqa: E402

# Keep the small import surface used by the focused synthetic tests available from the
# executable module. The implementation lives in the support module so it can be reused.
DEFAULT_LOG_ROOTS = support.DEFAULT_LOG_ROOTS
ProbeConfig = support.ProbeConfig
Invocation = support.Invocation
classify_command = support.classify_command
parse_logs = support.parse_logs
percentile = support.percentile
cohort = support.cohort
_completion_phase_timestamps = support._completion_phase_timestamps

DIRECT_PHASE_FIELDS = {
    "pre_verifier_total": "pre_verifier_total_ms",
    "structural_scan": "structural_scan_ms",
    "material_policy": "material_policy_ms",
    "descendant_scope": "descendant_scope_ms",
    "landing_check": "landing_check_ms",
    "verifier_call": "verifier_call_ms",
    "git_history_read": "git_history_read_ms",
    "alias_index_build": "alias_index_build_ms",
    "ticket_ref_resolution": "ticket_ref_resolution_ms",
    "diff_validation": "diff_validation_ms",
}

VERIFIER_WRAPPER_PHASE_FIELDS = {
    "verifier_wrapper_setup": "verifier_wrapper_setup_ms",
    "verifier_reusable_lookup": "verifier_reusable_lookup_ms",
    "verifier_resume_config": "verifier_resume_config_ms",
    "verifier_attempts": "verifier_attempts_ms",
    "verifier_between_attempts": "verifier_between_attempts_ms",
    "verifier_wrapper_finalization": "verifier_wrapper_finalization_ms",
    "verifier_wrapper_total": "verifier_wrapper_total_ms",
}
VERIFIER_WRAPPER_PARTITION_FIELDS = (
    "verifier_wrapper_setup_ms",
    "verifier_reusable_lookup_ms",
    "verifier_resume_config_ms",
    "verifier_attempts_ms",
    "verifier_between_attempts_ms",
    "verifier_wrapper_finalization_ms",
)
VERIFIER_ATTEMPT_PHASE_FIELDS = {
    "verifier_attempt_setup": "verifier_attempt_setup_ms",
    "verifier_handle_resolution": "verifier_handle_resolution_ms",
    "verifier_snapshot_enter": "verifier_snapshot_enter_ms",
    "verifier_handle_apply": "verifier_handle_apply_ms",
    "verifier_inner_setup": "verifier_inner_setup_ms",
    "verifier_dispatch": "verifier_dispatch_ms",
    "verifier_annotation": "verifier_annotation_ms",
    "verifier_snapshot_exit": "verifier_snapshot_exit_ms",
}
VERIFIER_HANDLE_PHASE_FIELDS = {
    "verifier_handle_defaults": "verifier_handle_defaults_ms",
    "verifier_code_snapshot": "verifier_code_snapshot_ms",
    "verifier_build_drift": "verifier_build_drift_ms",
    "verifier_ticket_snapshot": "verifier_ticket_snapshot_ms",
    "verifier_snapshot_gc": "verifier_snapshot_gc_ms",
}
VERIFIER_WORKFLOW_PHASE_FIELDS = {
    "verifier_precheck_context": "verifier_precheck_context_ms",
    "verifier_completion_agent": "verifier_completion_agent_ms",
    "verifier_verdict_reconcile": "verifier_verdict_reconcile_ms",
    "verifier_no_llm_passthrough": "verifier_no_llm_passthrough_ms",
    "verifier_unclassified_workflow_steps": "verifier_unclassified_workflow_steps_ms",
    "verifier_workflow_residual": "verifier_workflow_residual_ms",
}
VERIFIER_WORKFLOW_PARTITION_FIELDS = tuple(VERIFIER_WORKFLOW_PHASE_FIELDS.values())
VERIFIER_DISPATCH_PHASE_FIELDS = {
    "verifier_dispatch_setup": "verifier_dispatch_setup_ms",
    "verifier_workflow_total": "verifier_workflow_ms",
    **VERIFIER_WORKFLOW_PHASE_FIELDS,
    "verifier_dispatch_finalization": "verifier_dispatch_finalization_ms",
}
VERIFIER_PHASE_FIELDS = {
    **VERIFIER_WRAPPER_PHASE_FIELDS,
    **VERIFIER_ATTEMPT_PHASE_FIELDS,
    **VERIFIER_HANDLE_PHASE_FIELDS,
    **VERIFIER_DISPATCH_PHASE_FIELDS,
}

PHASE_SOURCES = {
    **{phase: f"direct-sidecar-metric({field})" for phase, field in DIRECT_PHASE_FIELDS.items()},
    **{phase: f"direct-sidecar-metric({field})" for phase, field in VERIFIER_PHASE_FIELDS.items()},
    "verifier_workflow_total": "direct-sidecar-aggregate(verifier_workflow_ms)",
    "verifier_precheck_context": (
        "direct-workflow-step(precheck: closure checks + ticket context/prefetch assembly)"
    ),
    "verifier_completion_agent": (
        "direct-workflow-step(verify: model + tools + bounded recovery/finalizer)"
    ),
    "verifier_verdict_reconcile": (
        "direct-workflow-step(reconcile: normalize + citations + verdict invariants)"
    ),
    "verifier_no_llm_passthrough": (
        "direct-workflow-step(passthrough: deterministic no-LLM verdict)"
    ),
    "verifier_unclassified_workflow_steps": (
        "direct-workflow-step(unrecognized step_id; telemetry mapping required)"
    ),
    "verifier_workflow_residual": (
        "arithmetic-residual(workflow-total-minus-timed-leaves; validation/routing/recording)"
    ),
    "unattributed_workflow": (
        "arithmetic-gap(workflow-total-minus-complete-workflow-partition; tolerance=1ms)"
    ),
    "unattributed_verifier": (
        "arithmetic-residual(verifier_call_ms-minus-nonoverlapping-wrapper-partition)"
    ),
    "legacy_uninstrumented": "unexplained-legacy-residual(no-causal-attribution)",
    "deterministic_verifier": "direct-sidecar-metric(det_ms)",
    "llm_verifier": "direct-sidecar-metric(llm_ms)",
    "verifier_overhead": "inferred-residual(total_ms-det_ms-llm_ms)",
    "verdict_to_status": "direct-tracker-event-interval",
    "status_to_signature": "direct-tracker-event-interval",
    "post_write_tail": "inferred-residual(last-event-to-command-result)",
}

CLOSE_WORKLOAD_FIELDS = (
    "commits_inspected",
    "distinct_references",
    "descendant_ids",
    "referencing_commits_found",
    "verifier_attempt_count",
    "verifier_resume_count",
    "verifier_workflow_step_count",
)


def close_workload_values(invocation: Invocation) -> dict[str, int | float | None]:
    """Return directly measured close-workload counts."""
    event = invocation.event or {}
    return {
        field: value if isinstance(value := event.get(field), (int, float)) else None
        for field in CLOSE_WORKLOAD_FIELDS
    }


def unattributed_verifier_ms(invocation: Invocation) -> float | None:
    """Return only verifier-call time not covered by the top-level direct partition."""
    event = invocation.event or {}
    verifier_call_ms = event.get("verifier_call_ms")
    partition_values = [event.get(field) for field in VERIFIER_WRAPPER_PARTITION_FIELDS]
    numeric_partition = [value for value in partition_values if isinstance(value, (int, float))]
    if not isinstance(verifier_call_ms, (int, float)) or len(numeric_partition) != len(
        partition_values
    ):
        return None
    gap_ms = verifier_call_ms - sum(numeric_partition)
    # Each integer phase can lose <1 ms independently when its nanoseconds are floored.
    tolerance_ms = len(VERIFIER_WRAPPER_PARTITION_FIELDS)
    return max(0.0, gap_ms) if gap_ms > tolerance_ms else 0.0


def unattributed_workflow_ms(invocation: Invocation) -> float | None:
    """Return workflow time outside the complete recorded-step + residual partition."""
    event = invocation.event or {}
    workflow_ms = event.get("verifier_workflow_ms")
    partition_values = [event.get(field) for field in VERIFIER_WORKFLOW_PARTITION_FIELDS]
    numeric_partition = [value for value in partition_values if isinstance(value, (int, float))]
    if not isinstance(workflow_ms, (int, float)) or len(numeric_partition) != len(
        partition_values
    ):
        return None
    gap_ms = workflow_ms - sum(numeric_partition)
    return max(0.0, gap_ms) if gap_ms > 1 else 0.0


def close_phase_values(invocation: Invocation) -> dict[str, float | None]:
    """Return directly measured and residual close-phase durations in seconds."""
    event = invocation.event or {}
    direct_phases = {
        phase: value / 1000 if isinstance(value := event.get(field), (int, float)) else None
        for phase, field in DIRECT_PHASE_FIELDS.items()
    }
    verifier_phases = {
        phase: value / 1000 if isinstance(value := event.get(field), (int, float)) else None
        for phase, field in VERIFIER_PHASE_FIELDS.items()
    }
    directly_instrumented = all(value is not None for value in direct_phases.values())
    total_ms = event.get("total_ms")
    det_ms = event.get("det_ms")
    llm_ms = event.get("llm_ms")
    verdict_at = event.get("timestamp")
    status_at = event.get("status_at")
    signature_at = event.get("signature_at")
    total = total_ms / 1000 if isinstance(total_ms, (int, float)) else None
    det = det_ms / 1000 if isinstance(det_ms, (int, float)) else None
    llm = llm_ms / 1000 if isinstance(llm_ms, (int, float)) else None

    legacy_uninstrumented = None
    if not directly_instrumented and total is not None and isinstance(verdict_at, (int, float)):
        legacy_uninstrumented = verdict_at - total - invocation.start
    overhead = None
    if total is not None and det is not None and llm is not None:
        overhead = total - det - llm
    unattributed_ms = unattributed_verifier_ms(invocation)
    unattributed_verifier = unattributed_ms / 1000 if unattributed_ms is not None else None
    workflow_gap_ms = unattributed_workflow_ms(invocation)
    unattributed_workflow = workflow_gap_ms / 1000 if workflow_gap_ms is not None else None
    verdict_to_status = None
    if isinstance(verdict_at, (int, float)) and isinstance(status_at, (int, float)):
        verdict_to_status = status_at - verdict_at
    status_to_signature = None
    if isinstance(status_at, (int, float)) and isinstance(signature_at, (int, float)):
        status_to_signature = signature_at - status_at
    last_event = next(
        (
            value
            for value in (signature_at, status_at, verdict_at)
            if isinstance(value, (int, float))
        ),
        None,
    )
    post = (
        invocation.end - last_event
        if invocation.end is not None and last_event is not None
        else None
    )
    return {
        **direct_phases,
        **verifier_phases,
        "unattributed_verifier": unattributed_verifier,
        "unattributed_workflow": unattributed_workflow,
        "legacy_uninstrumented": legacy_uninstrumented,
        "deterministic_verifier": det,
        "llm_verifier": llm,
        "verifier_overhead": overhead,
        "verdict_to_status": verdict_to_status,
        "status_to_signature": status_to_signature,
        "post_write_tail": post,
    }


def _quantiles(values: list[float]) -> str:
    return "/".join(f"{support.percentile(values, q):.3f}" for q in (0.5, 0.9, 0.99))


def _matched_event(item: Invocation) -> dict[str, Any]:
    event = item.event
    if event is None:
        raise ValueError("matched cohort item has no tracker event")
    return event


def print_phase_summary(items: list[Invocation], label: str) -> None:
    print(f"phase_breakdown_seconds cohort={label} n={len(items)}")
    values_by_phase: dict[str, list[float]] = defaultdict(list)
    values_by_workload: dict[str, list[float]] = defaultdict(list)
    for item in items:
        for phase, value in close_phase_values(item).items():
            if value is not None and value >= 0:
                values_by_phase[phase].append(value)
        for field, value in close_workload_values(item).items():
            if value is not None and value >= 0:
                values_by_workload[field].append(value)
    for phase, source in PHASE_SOURCES.items():
        values = values_by_phase[phase]
        if values:
            print(f"{phase}\tsource={source}\tn={len(values)}\tp50/p90/p99={_quantiles(values)}")
    for field in CLOSE_WORKLOAD_FIELDS:
        values = values_by_workload[field]
        if values:
            print(
                f"{field}\tsource=direct-sidecar-count({field})\t"
                f"n={len(values)}\tp50/p90/p99={_quantiles(values)}"
            )


def _classification(candidates: list[Invocation], config: ProbeConfig) -> Counter[str]:
    reasons: Counter[str] = Counter()
    for item in candidates:
        if item.excluded:
            reasons[f"excluded:{item.excluded}"] += 1
        elif item.duration is None:
            reasons["incomplete"] += 1
        elif item.exit_code in {130, 137, 143}:
            reasons["interrupted"] += 1
        elif config.since is not None and item.start < config.since:
            reasons["pre_since"] += 1
        elif config.until is not None and item.start > config.until:
            reasons["post_until"] += 1
        elif item.event is None:
            reasons["no_event_match"] += 1
        elif (
            not isinstance(item.event.get("llm_calls"), (int, float))
            or item.event["llm_calls"] <= 0
        ):
            reasons["no_llm"] += 1
        elif config.provider_prefix is not None and (
            not isinstance(item.event.get("ran_model"), str)
            or not item.event["ran_model"].startswith(config.provider_prefix)
        ):
            reasons["provider"] += 1
        else:
            reasons["clean"] += 1
    return reasons


def _print_subset_stats(clean: list[Invocation]) -> None:
    subsets = (
        ("unique_event", [item for item in clean if item.event_matches == 1]),
        (
            "simple_unique",
            [
                item
                for item in clean
                if item.event_matches == 1 and not item.compound and not item.multi
            ],
        ),
        (
            "pass_only",
            [item for item in clean if _matched_event(item).get("verdict") == "PASS"],
        ),
    )
    for label, subset in subsets:
        values = [item.duration for item in subset if item.duration is not None]
        if values:
            print(label, "n=", len(values), "p50/p90/p99=", _quantiles(values))


def _print_inner_stats(clean: list[Invocation]) -> None:
    inner = [
        _matched_event(item)["total_ms"] / 1000
        for item in clean
        if isinstance(_matched_event(item).get("total_ms"), (int, float))
    ]
    omitted = [
        item.duration - _matched_event(item)["total_ms"] / 1000
        for item in clean
        if item.duration is not None
        and isinstance(_matched_event(item).get("total_ms"), (int, float))
    ]
    if inner and omitted:
        print("matched_inner_p50/p90/p99", _quantiles(inner))
        print("omitted_p50/p90/p99", _quantiles(omitted))

    pre_workflow = [
        _matched_event(item)["timestamp"] - item.start - _matched_event(item)["total_ms"] / 1000
        for item in clean
        if isinstance(_matched_event(item).get("timestamp"), (int, float))
        and isinstance(_matched_event(item).get("total_ms"), (int, float))
    ]
    post_event = [
        item.end - _matched_event(item)["timestamp"]
        for item in clean
        if item.end is not None and isinstance(_matched_event(item).get("timestamp"), (int, float))
    ]
    if pre_workflow and post_event:
        print("pre_workflow_p50/p90/p99", _quantiles(pre_workflow))
        print("post_event_p50/p90/p99", _quantiles(post_event))


def _current_regime(clean: list[Invocation], config: ProbeConfig) -> list[Invocation]:
    if config.current_since is None:
        return []
    current = [item for item in clean if item.start >= config.current_since]
    values = [item.duration for item in current if item.duration is not None]
    if values:
        print(
            "current_regime",
            support.iso(config.current_since),
            "n=",
            len(values),
            "p50/p90/p99=",
            _quantiles(values),
            "padded=",
            f"{support.percentile(values, 0.5) * 1.2:.3f}/"
            f"{support.percentile(values, 0.9) * 1.2:.3f}",
        )
    return current


def _print_clean_stats(operation: str, clean: list[Invocation], config: ProbeConfig) -> None:
    values = [item.duration for item in clean if item.duration is not None]
    stats = {f"p{int(q * 100)}": support.percentile(values, q) for q in (0.5, 0.9, 0.99)}
    stats.update(
        min=min(values),
        max=max(values),
        padded_low=stats["p50"] * 1.2,
        padded_high=stats["p90"] * 1.2,
    )
    print("stats_seconds", json.dumps(stats, sort_keys=True))
    print(
        "stats_minutes",
        json.dumps({key: value / 60 for key, value in stats.items()}, sort_keys=True),
    )
    print(
        "window",
        support.iso(min(item.start for item in clean)),
        support.iso(max(item.end or item.start for item in clean)),
    )
    print(
        "verdicts",
        json.dumps(
            dict(Counter(_matched_event(item).get("verdict") for item in clean)),
            sort_keys=True,
        ),
    )
    print(
        "exit_codes",
        json.dumps(dict(Counter(str(item.exit_code) for item in clean)), sort_keys=True),
    )
    print(
        "event_match_counts",
        json.dumps(dict(Counter(str(item.event_matches) for item in clean)), sort_keys=True),
    )
    print(
        "compound", sum(item.compound for item in clean), "multi", sum(item.multi for item in clean)
    )
    _print_subset_stats(clean)
    _print_inner_stats(clean)
    current = _current_regime(clean, config)
    if operation == "close":
        print_phase_summary(clean, "full")
        if current:
            print_phase_summary(current, "current-regime")


def _print_censoring(eligible: list[Invocation]) -> None:
    censored = [
        item for item in eligible if item.duration is None or item.exit_code in {130, 137, 143}
    ]
    print(
        "eligible_with_censoring",
        len(eligible),
        "right_censored",
        len(censored),
        "incomplete_censored",
        sum(item.duration is None for item in censored),
        "interrupted_censored",
        sum(item.exit_code in {130, 137, 143} for item in censored),
    )
    if censored:
        elapsed = [item.observed_elapsed for item in censored if item.observed_elapsed is not None]
        print(
            "censored_elapsed_p50/p90/max",
            "/".join(
                f"{value:.3f}"
                for value in (
                    support.percentile(elapsed, 0.5),
                    support.percentile(elapsed, 0.9),
                    max(elapsed),
                )
            ),
        )
    print(
        "kaplan_meier_seconds",
        json.dumps(support.kaplan_meier_quantiles(eligible), sort_keys=True),
    )
    unique_eligible = [
        item
        for item in eligible
        if item.event_matches == 1 and not item.compound and not item.multi
    ]
    print(
        "kaplan_meier_unique_simple_seconds",
        json.dumps(support.kaplan_meier_quantiles(unique_eligible), sort_keys=True),
        "n",
        len(unique_eligible),
    )


def print_summary(
    invocations: list[Invocation],
    audit: Counter[str],
    tracker_audit: Counter[str],
    config: ProbeConfig,
) -> None:
    print("parser_audit", json.dumps(dict(audit), sort_keys=True))
    print("tracker_audit", json.dumps(dict(tracker_audit), sort_keys=True))
    print(
        "candidate_counts",
        json.dumps(dict(Counter(item.operation for item in invocations)), sort_keys=True),
    )
    for operation in ("plan", "close"):
        candidates = [item for item in invocations if item.operation == operation]
        clean = support.cohort(invocations, operation, config)
        eligible = support.eligible_with_censoring(invocations, operation, config)
        print(f"\n{operation.upper()} candidates={len(candidates)} clean={len(clean)}")
        print(
            "classification", json.dumps(dict(_classification(candidates, config)), sort_keys=True)
        )
        if clean:
            _print_clean_stats(operation, clean, config)
        _print_censoring(eligible)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("summary", "clean", "all", "unmatched", "table"),
        nargs="?",
        default="summary",
    )
    parser.add_argument("--operation", choices=("plan", "close"))
    parser.add_argument(
        "--log-root",
        action="append",
        type=Path,
        help="Codex rollout root (repeatable; supplying one replaces the two defaults)",
    )
    parser.add_argument("--tracker", type=Path, default=Path(".tickets-tracker"))
    parser.add_argument("--since", type=support.ts_seconds, metavar="ISO8601")
    parser.add_argument("--until", type=support.ts_seconds, metavar="ISO8601")
    parser.add_argument("--provider-prefix", help="require provider_provenance.ran_model prefix")
    parser.add_argument("--current-since", type=support.ts_seconds, metavar="ISO8601")
    return parser


def _print_table(selected: list[Invocation]) -> None:
    print(
        "start\tseconds\texit\tmatches\tcompound\ttarget\tverdict\tinner_s\t"
        "pre_s\tdet_s\tllm_s\tverdict_status_s\tstatus_signature_s\tpost_s\tworkdir"
    )
    phase_names = (
        "deterministic_verifier",
        "llm_verifier",
        "verdict_to_status",
        "status_to_signature",
        "post_write_tail",
    )
    for item in sorted(selected, key=lambda invocation: invocation.start):
        inner = (
            item.event["total_ms"] / 1000
            if item.event and isinstance(item.event.get("total_ms"), (int, float))
            else None
        )
        phases = close_phase_values(item) if item.operation == "close" else {}
        pre_verifier = phases.get("pre_verifier_total")
        if pre_verifier is None:
            pre_verifier = phases.get("legacy_uninstrumented")
        print(
            "\t".join(
                str(value)
                for value in (
                    support.iso(item.start),
                    f"{item.duration:.3f}" if item.duration is not None else "",
                    item.exit_code,
                    item.event_matches,
                    item.compound,
                    item.target,
                    item.event.get("verdict") if item.event else None,
                    f"{inner:.3f}" if inner is not None else "",
                    f"{pre_verifier:.3f}" if pre_verifier is not None else "",
                    *(
                        f"{phases[name]:.3f}" if phases.get(name) is not None else ""
                        for name in phase_names
                    ),
                    item.workdir,
                )
            )
        )


def _selected_output(
    mode: str,
    operation: str | None,
    invocations: list[Invocation],
    config: ProbeConfig,
) -> list[Invocation]:
    selected = invocations
    if operation:
        selected = [item for item in selected if item.operation == operation]
    if mode == "clean":
        selected = [
            item for item in selected if item in support.cohort(invocations, item.operation, config)
        ]
    elif mode == "unmatched":
        selected = [
            item
            for item in selected
            if not item.excluded
            and item.duration is not None
            and item.event is None
            and (config.since is None or item.start >= config.since)
            and (config.until is None or item.start <= config.until)
        ]
    return selected


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since must not be later than --until")
    config = ProbeConfig(
        log_roots=tuple(args.log_root or DEFAULT_LOG_ROOTS),
        tracker=args.tracker,
        since=args.since,
        until=args.until,
        provider_prefix=args.provider_prefix,
        current_since=args.current_since,
    )
    invocations, audit = support.parse_logs(config.log_roots)
    aliases, events, tracker_audit = support.load_tracker(config)
    support.attach_events(invocations, aliases, events, config)
    if args.mode == "summary":
        print_summary(invocations, audit, tracker_audit, config)
        return
    if args.mode == "table":
        selected = (
            support.eligible_with_censoring(invocations, args.operation, config)
            if args.operation
            else []
        )
        _print_table(selected)
        return
    for item in sorted(
        _selected_output(args.mode, args.operation, invocations, config),
        key=lambda invocation: invocation.start,
    ):
        print(json.dumps(support.serializable(item), sort_keys=True))


if __name__ == "__main__":
    main()
