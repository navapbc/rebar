"""Asymmetric manifest renderer for the reconciler rollout modes.

Per epic 4047, each rollout mode emits a manifest shape calibrated to its risk
profile:

* ``dry-run`` / ``bootstrap-strict``: outbound writes summarized as totals
  (create / update / delete counts); inbound writes enumerated per-ticket
  with full field detail. Rationale: during early phases, inbound work
  (touching the local tracker) is the dangerous side, so operators need
  per-ticket evidence; outbound counts suffice.
* ``bootstrap-throttle``: both directions summarized to totals, plus a
  10% deterministic ``spot_check`` sample selected by a stable SHA-256 hash
  of the target (Python's built-in ``hash()`` is randomized per-process).
* legacy ``live``: no manifest file (GHA log only); canonical ``bridge sync``
  retains the field-comparable plan rendered here. Route selection remains the
  caller's responsibility.

All renderers return plain dicts (JSON-serializable) and are pure functions:
no I/O, no time, no environment access. The caller writes the result to disk.

Both ``mutations_applied`` and ``mutations_deferred`` are iterables of either
Mutation dataclass instances or legacy dict-shaped batch mutations. The
renderer normalizes via best-effort attribute / key lookup so the two surfaces
compose.

Contract: ``docs/contracts/asymmetric-manifest.md``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any


def _direction_of(m: Any) -> str:
    """Return the canonical ``"inbound"`` / ``"outbound"`` string for *m*."""
    d = getattr(m, "direction", None)
    if d is None and isinstance(m, dict):
        d = m.get("direction", "")
    return str(getattr(d, "value", d) or "")


def _action_of(m: Any) -> str:
    a = getattr(m, "action", None)
    if a is None and isinstance(m, dict):
        a = m.get("action", "")
    return str(getattr(a, "value", a) or "")


def _target_of(m: Any) -> str:
    t = getattr(m, "target", None)
    if t is None and isinstance(m, dict):
        t = m.get("key", "") or m.get("target", "")
    return str(t or "")


def _payload_of(m: Any) -> dict:
    p = getattr(m, "payload", None)
    if p is None and isinstance(m, dict):
        # Legacy batch dicts carry their per-mutation fields under "fields".
        p = m.get("fields", {})
    if not isinstance(p, Mapping):
        return {}
    return dict(p)


def _local_id_of(m: Any) -> str:
    """Return the local ticket id carried by a typed or legacy mutation."""
    if isinstance(m, dict):
        return str(m.get("local_id", "") or "")
    provenance = getattr(m, "provenance", None)
    if isinstance(provenance, Mapping):
        return str(provenance.get("local_id", "") or "")
    return ""


def render_plan(mutations: Iterable[Any]) -> list[dict]:
    """Render canonical field-comparable entries for one pass's proposals."""
    return [
        {
            "direction": _direction_of(m),
            "action": _action_of(m),
            "target": _target_of(m),
            "local_id": _local_id_of(m),
            "fields": _payload_of(m),
        }
        for m in mutations
    ]


def _totals(mutations: Iterable[Any]) -> dict[str, int]:
    """Return per-action totals across *mutations*."""
    totals = {"create": 0, "update": 0, "delete": 0}
    for m in mutations:
        action = _action_of(m)
        if action in totals:
            totals[action] += 1
    return totals


def _partition_by_direction(
    mutations: Iterable[Any],
) -> tuple[list[Any], list[Any]]:
    inbound: list[Any] = []
    outbound: list[Any] = []
    for m in mutations:
        direction = _direction_of(m)
        if direction == "inbound":
            inbound.append(m)
        else:
            outbound.append(m)
    return inbound, outbound


def _enumerate_inbound(mutations: Iterable[Any]) -> list[dict]:
    """Render inbound mutations as a per-ticket array with full field detail."""
    entries: list[dict] = []
    for m in mutations:
        entries.append(
            {
                "key": _target_of(m),
                "action": _action_of(m),
                "fields": _payload_of(m),
            }
        )
    return entries


def render_dry_run_or_strict(
    mutations_applied: Iterable[Any],
    mutations_deferred: Iterable[Any],
) -> dict:
    """Manifest shape for ``dry-run`` and ``bootstrap-strict``.

    Combines applied + deferred mutations into a single view because in
    ``dry-run`` nothing is applied (everything is deferred), and in
    ``bootstrap-strict`` the manifest documents both what ran and what was
    held back. Outbound is summarized; inbound is enumerated per-ticket.
    """
    applied_list = list(mutations_applied)
    deferred_list = list(mutations_deferred)
    combined = applied_list + deferred_list

    inbound, outbound = _partition_by_direction(combined)
    return {
        "outbound": {"totals": _totals(outbound)},
        "inbound": _enumerate_inbound(inbound),
        "applied_count": len(applied_list),
        "deferred_count": len(deferred_list),
    }


def _stable_bucket(target: str) -> int:
    """Map *target* to a stable bucket in [0, 10) using SHA-256.

    Python's built-in ``hash()`` is randomized per-process (unless
    ``PYTHONHASHSEED`` is pinned), which breaks the renderer's "Stable across
    runs" contract. SHA-256 is deterministic across processes and platforms.
    """
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return int(digest, 16) % 10


def _spot_check_sample(mutations: Iterable[Any]) -> list[dict]:
    """Select a deterministic 10% sample of *mutations* keyed by target hash.

    Uses a SHA-256-derived bucket (``_stable_bucket(target) == 0``) so the
    sample is stable across runs and processes as long as the target
    identifier is stable. Each sampled mutation is rendered with the same
    shape as ``_enumerate_inbound`` so spot-check consumers have full field
    detail.
    """
    sample: list[dict] = []
    for m in mutations:
        target = _target_of(m)
        if _stable_bucket(target) == 0:
            sample.append(
                {
                    "key": target,
                    "direction": _direction_of(m),
                    "action": _action_of(m),
                    "fields": _payload_of(m),
                }
            )
    return sample


def render_throttle(
    mutations_applied: Iterable[Any],
    mutations_deferred: Iterable[Any],
) -> dict:
    """Manifest shape for ``bootstrap-throttle``.

    Both directions summarized to totals plus a 10% deterministic spot-check.
    """
    applied_list = list(mutations_applied)
    deferred_list = list(mutations_deferred)
    combined = applied_list + deferred_list

    inbound, outbound = _partition_by_direction(combined)
    return {
        "outbound": {"totals": _totals(outbound)},
        "inbound": {"totals": _totals(inbound)},
        "spot_check": _spot_check_sample(combined),
        "applied_count": len(applied_list),
        "deferred_count": len(deferred_list),
    }


_LIFECYCLE_INTENTS_SCHEMA_VERSION = 1


def _observation_version_dict(ov: Any) -> dict | None:
    """Render an ObservationVersion as ``{"pass_id", "fingerprint"}`` (or None)."""
    if ov is None:
        return None
    return {"pass_id": ov.pass_id, "fingerprint": ov.fingerprint}


def _lifecycle_plan_entry(plan: Any) -> dict:
    """One plan's lifecycle-intents entry: identity, disposition, defer reason,
    dependencies, and each intent's KIND / target / version ONLY. The raw intent /
    mutation ``payload`` is deliberately excluded — payloads may carry secrets."""
    return {
        "identity": plan.identity,
        "disposition": plan.disposition.value,
        "defer_reason": plan.defer_reason.value if plan.defer_reason else None,
        "dependencies": list(plan.dependencies),
        "intents": [
            {
                "kind": i.kind.value,
                "target": i.target,
                "version": {
                    "pass_id": i.version.pass_id,
                    "fingerprint": i.version.fingerprint,
                },
            }
            for i in plan.intents
        ],
    }


def render_lifecycle_intents(ticket_plans: Iterable[Any]) -> dict:
    """Render an additive, versioned lifecycle-intents section for the shadow plans.

    Exposes each plan's disposition / dependencies and its intents' KIND / target /
    version — never the raw payload (no secret leakage). Pure, JSON-serializable, no I/O.
    """
    plans = list(ticket_plans)
    ov = plans[0].observation_version if plans else None
    return {
        "schema_version": _LIFECYCLE_INTENTS_SCHEMA_VERSION,
        "observation_version": _observation_version_dict(ov),
        "plans": [_lifecycle_plan_entry(p) for p in plans],
    }


# ── REB-3115 S5 T2 — the sealed, additive, versioned pass-outcomes section ───────
#
# ``render_pass_outcomes`` folds a pass's proven logical-operation outcomes into ONE
# additive, versioned section exposing every named lifecycle / retry / fuse / outcome
# field (AC2): disposition, failure scope, replay safety, logical + physical attempts,
# delay source / value, the retry budget envelope, ``retry_not_before``, per-outcome fuse
# state, and the exact five-bucket pass tally + degraded exit signal.
#
# It NEVER participates in the canonical mutation array or its hash (AC1): it is a pure
# sibling projection the caller adds as a separate top-level key. Every field is redacted
# and every collection is bounded (AC6): diagnostics run through the ADR-0041 sanitizer +
# 8-entry bound (``operation_outcome.bound_diagnostics``), and the outcomes / fuse-state
# lists are capped with an honest truncation count. Duck-typed over both
# ``OperationOutcome`` (the rich per-op record) and the coordinator's
# ``TicketOutcome`` / ``CutoverOutcome`` so either surface composes.

_PASS_OUTCOMES_SCHEMA_VERSION = 1
_MAX_PASS_OUTCOMES = 1000
_OUTCOME_BUCKETS: tuple[str, ...] = ("applied", "recovered", "deferred", "failed", "skipped")

# The retry-budget bounds are single-sourced in retry_budget.py; surfaced here so the
# rendered ``budget`` envelope names the caps a consumer measures consumption against. A
# best-effort import keeps this leaf pure/standalone-loadable — an absent module degrades
# the caps to ``None`` rather than raising.
try:  # pragma: no cover - trivial import guard
    from rebar_reconciler.retry_budget import MAX_CUMULATIVE_SLEEP_MS, MAX_INVOCATIONS
except ImportError:  # pragma: no cover - standalone/partial load
    MAX_INVOCATIONS = None
    MAX_CUMULATIVE_SLEEP_MS = None


def _enum_value(value: Any, default: str | None = None) -> str | None:
    """Return an enum member's ``.value`` (or a plain string), else *default*."""
    if value is None:
        return default
    return str(getattr(value, "value", value))


def _bounded_diagnostics(raw: Any) -> list[dict]:
    """Redact + bound a diagnostics collection via the operation_outcome seam (AC6)."""
    from rebar_reconciler.operation_outcome import bound_diagnostics

    return [dict(entry) for entry in bound_diagnostics(raw or ())]


def _fuse_decision_dict(decision: Any) -> dict | None:
    """Render a ``FuseDecision`` as its five named fields (or ``None`` when unfused)."""
    if decision is None:
        return None
    return {
        "scope": _enum_value(getattr(decision, "scope", None)),
        "reason": getattr(decision, "reason", None),
        "retry_not_before": getattr(decision, "retry_not_before", None),
        "provider": getattr(decision, "provider", None),
        "endpoint": getattr(decision, "endpoint", None),
    }


def _pass_outcome_entry(outcome: Any) -> dict:
    """One outcome's entry carrying every named lifecycle/retry/fuse field (AC2).

    Read duck-typed so an ``OperationOutcome`` (``logical_id`` + full attempt/delay detail)
    and a coordinator ``CutoverOutcome`` (``identity`` + attached ``fuse_decision``) both
    render; absent fields default to their neutral value rather than raising."""
    logical_id = getattr(outcome, "logical_id", None) or getattr(outcome, "identity", "")
    invocations = int(getattr(outcome, "invocation_count", 0) or 0)
    requests = int(getattr(outcome, "request_count", 0) or 0)
    return {
        "logical_id": str(logical_id),
        "disposition": _enum_value(getattr(outcome, "disposition", None), ""),
        "failure_scope": _enum_value(getattr(outcome, "failure_scope", None), "none"),
        "replay_safety": _enum_value(getattr(outcome, "replay_safety", None), "not_applicable"),
        "logical_attempts": invocations,
        "physical_attempts": requests,
        "delay_source": _enum_value(getattr(outcome, "delay_source", None), "none"),
        "delay_value_ms": getattr(outcome, "provider_delay_ms", None),
        "budget": {
            "max_invocations": MAX_INVOCATIONS,
            "max_cumulative_sleep_ms": MAX_CUMULATIVE_SLEEP_MS,
            "invocations_used": invocations,
            "requests_used": requests,
        },
        "retry_not_before": getattr(outcome, "retry_not_before", None),
        "fuse": _fuse_decision_dict(getattr(outcome, "fuse_decision", None)),
        "diagnostics": _bounded_diagnostics(getattr(outcome, "diagnostics", ())),
    }


def _pass_tally_dict(tally: Any) -> dict[str, int]:
    """Project a tally mapping onto the five canonical buckets, 0-filled and exact."""
    source = tally or {}
    return {bucket: int(source.get(bucket, 0) or 0) for bucket in _OUTCOME_BUCKETS}


def render_pass_outcomes(
    outcomes: Iterable[Any],
    *,
    fuse_decisions: Iterable[Any] = (),
    tally: Any = None,
    degraded: bool = False,
    observation_version: Any = None,
) -> dict:
    """Render the additive, versioned pass-outcomes section (REB-3115 S5 T2).

    Pure and JSON-serializable: identical inputs render byte-identical output. Additive —
    the caller attaches the result as a NEW top-level manifest/log key and the canonical
    mutation array is untouched (AC1). Version-tagged so a legacy reader can ignore it.
    Redacts every diagnostic and bounds every collection (AC6).
    """
    all_outcomes = list(outcomes)
    kept = all_outcomes[:_MAX_PASS_OUTCOMES]
    all_fuse = list(fuse_decisions)
    return {
        "schema_version": _PASS_OUTCOMES_SCHEMA_VERSION,
        "observation_version": _observation_version_dict(observation_version),
        "tally": _pass_tally_dict(tally),
        "degraded": bool(degraded),
        "outcomes": [_pass_outcome_entry(o) for o in kept],
        "outcomes_truncated": max(0, len(all_outcomes) - len(kept)),
        "fuse_state": [
            _fuse_decision_dict(d) for d in all_fuse[:_MAX_PASS_OUTCOMES] if d is not None
        ],
    }
