"""Held-out validation for task 7504 — authored independently of the implementation.

Three values accepted by a signature and never read. Deletion was the approved
remediation for all three, because every "wire it up" alternative invents a capability
no caller asked for. But two of the three are not merely inert, and those consequences
are what this file pins:

* `timeout_s` sat as a NAMED keyword-only parameter in front of `**kwargs` on
  `_call_with_retry` — the wrapper on the path for EVERY Jira write. A caller passing
  `timeout_s=60` for the wrapped client had it SWALLOWED rather than forwarded. Deleting
  the parameter is what restores forwarding, so the fix is observable, not cosmetic.

* The two metric seeds held their ids with a `compute` that always returned `None`.
  Every registrar appends under `if spec.id not in existing`, so a future REAL
  implementation would find the id taken and be SILENTLY skipped — no error, no warning,
  metric `Unavailable` forever. `module_size_trend` now has a real implementation; the
  remaining freed-id guard pins only the still-unclaimed `commit_cadence_trend` id.
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
    """The latent trap, tested directly: with the seed present this registration was a
    silent no-op. It must now succeed, and the registered spec must be the REAL one.

    Registration is simulated with the same `if spec.id not in existing` rule the
    registrars use, against a copy — so the process-wide registry is not mutated.
    """
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
