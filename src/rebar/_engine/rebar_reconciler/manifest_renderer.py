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
* ``live``: no manifest file (GHA log only); the dispatch in ``applier.apply``
  is the caller's responsibility — this module does NOT emit a renderer for
  LIVE.

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
from typing import Any
from collections.abc import Iterable


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
    if not isinstance(p, dict):
        return {}
    return dict(p)


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
