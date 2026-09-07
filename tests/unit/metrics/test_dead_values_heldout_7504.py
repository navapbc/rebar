"""Deleting unused parameters restores two observable contracts.

Removing ``timeout_s`` from ``_call_with_retry`` forwards that keyword to Jira clients.
Removing inert metric seeds leaves registry IDs available for implementations.
``module_size_trend`` retains its implemented ownership, while ``commit_cadence_trend``
remains available.
"""

from __future__ import annotations

import inspect

import pytest

# ── (b) run_eval no longer advertises a switch it does not have ─────────────


def test_run_eval_rejects_dirty() -> None:
    from rebar.llm.evals.eval import run_eval

    assert "dirty" not in inspect.signature(run_eval).parameters
    with pytest.raises(TypeError):
        run_eval(dirty=True)  # type: ignore[call-arg]


# ── (c) the freed metric ids can now actually be claimed ────────────────────


def test_a_real_spec_can_now_claim_the_remaining_freed_id() -> None:
    """An implementation can claim ``commit_cadence_trend`` without mutating the registry."""
    import rebar.metrics  # noqa: F401  (import-time hydration)
    from rebar.metrics.registry import REGISTRY, MetricSpec

    metric_id = "commit_cadence_trend"
    existing = {s.id for s in REGISTRY}
    assert metric_id not in existing, (
        f"{metric_id} is still held by a placeholder; a real implementation would be "
        "silently skipped by the `if spec.id not in existing` guard"
    )

    real = MetricSpec(
        id=metric_id,
        lens="code-health",
        source="git",
        confidence="measured",
        compute=lambda _ctx: 42,
        accruing_since="2026-08-06",
    )
    registered = list(REGISTRY)
    if real.id not in {s.id for s in registered}:
        registered.append(real)
    got = [s for s in registered if s.id == metric_id]
    assert len(got) == 1
    assert got[0].compute(None) == 42, "the REAL spec must be the one that lands"


def test_the_registry_is_still_hydrated_by_the_readers() -> None:
    """Deleting the seeds must not leave the registry empty — the reader modules'
    `register()` hooks are now solely responsible for filling it."""
    import rebar.metrics  # noqa: F401
    from rebar.metrics.registry import REGISTRY

    assert REGISTRY, "no metrics registered at all; hydration is broken"
    ids = {s.id for s in REGISTRY}
    assert "module_size_distribution" in ids, "the real git metrics should still register"


def test_no_registered_metric_is_a_permanent_placeholder() -> None:
    """The general form of the defect: a spec whose compute can never return data is an
    id squatter, and squatting is what silently blocks a real implementation."""
    import rebar.metrics  # noqa: F401
    from rebar.metrics.registry import REGISTRY

    squatters = [s.id for s in REGISTRY if getattr(s.compute, "__name__", "") == "_no_data_yet"]
    assert not squatters, f"placeholder specs still holding ids: {squatters}"
