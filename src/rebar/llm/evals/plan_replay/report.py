"""Render a Tier-0 :func:`rebar.llm.evals.plan_replay.tier0.run_tier0` result as Markdown
(ticket bouncy-peacockish-titmouse / 5d19-52e0-7c26-47fb)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def render_report(result: dict[str, Any], *, now: datetime | None = None) -> str:
    """A Markdown summary of one Tier-0 run: the harness self-check, the registry-drift
    vs. candidate-induced flip counts, friction rate, and (when computed) label-proxy
    metrics with their coverage fraction."""
    now = now or datetime.now(timezone.utc)
    matrix = result["flip_matrix"]
    label_proxy = result.get("label_proxy_metrics")

    lines = [
        f"# Tier-0 Pass-3 replay -- candidate `{result['candidate']}`",
        "",
        f"Generated: {now.date().isoformat()}",
        f"Corpus content hash: `{result['content_hash']}`",
        f"Replayed rows: {result['row_count']}  (skipped: {result['skipped']})",
        f"Findings scored: {matrix['total_findings']}",
        "",
        "## Harness self-check",
        "",
        f"- replayed-stored vs stored mismatches: {matrix['self_check_mismatches']}",
        f"- not replayed (prerequisite-coverage override, review-level, not a mismatch): "
        f"{matrix['not_replayed_prerequisite_coverage']}",
        "",
        "## Flip matrix",
        "",
        f"- registry-drift flips (stored vs live-baseline): {matrix['registry_drift_flips']}",
        f"- candidate-induced newly-blocking flips (live-baseline vs candidate): "
        f"{matrix['candidate_newly_blocking']}",
        f"- friction rate: {_fmt_rate(matrix['friction_rate'])}",
        f"- relief count (candidate unblocks a live-baseline block): {matrix['relief_count']}",
        "",
        "## Label-proxy metrics",
        "",
    ]
    if label_proxy is None:
        lines.append("- not computed (no labels file supplied)")
    else:
        lines += [
            f"- blocking agreement rate: {_fmt_rate(label_proxy['blocking_agreement_rate'])}",
            f"- proxy precision: {_fmt_rate(label_proxy['proxy_precision'])}",
            f"- proxy recall: {_fmt_rate(label_proxy['proxy_recall'])}",
            f"- coverage fraction: {_fmt_rate(label_proxy['coverage_fraction'])}",
        ]
    return "\n".join(lines) + "\n"
