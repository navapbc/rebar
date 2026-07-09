"""Baseline dual-write shadow — convergence rollout Phase 1 (epic 3006-e198, 7d23).

The one high-risk seam of the convergence work is swapping direction arbitration
from ``prev_snapshot`` to the per-binding ``baseline`` (ADR 0026). Failure mode:
silently reverting a teammate's Jira-side edit — reversible but hard to notice. The
derisk (experiment E3) is to run the new source of truth in SHADOW for N live
passes before any consumer reads it:

* **Dual-write** — every pass, advance a per-binding baseline from the current
  fetch snapshot (mirroring how ``prev_snapshot`` is advanced by ``copy2``), WITHOUT
  changing any consumer (``outbound_fields`` still reads ``prev_snapshot``).
* **Equivalence check** — before advancing, compare the STORED baseline (from the
  prior pass) against the value the live consumer uses this pass (the
  ``prev_snapshot`` entry) over the five mirrored fields, and log
  ``baseline_shadow_check`` with the equal/divergent counts. Divergence is exactly
  the persistence-bug class behind drift B; the prev_snapshot→baseline consumer swap
  (Phase 3) is gated on ``>= N`` consecutive clean (divergent == 0) passes.

This module is READ-MOSTLY: it only mutates the per-binding baseline (in-memory
until the existing binding-store commit path saves it), never the live consumer.
It is a no-op unless ``reconciler.baseline_dual_write`` is enabled.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

# The five last-synced Jira-side fields (twin of binding_store._BASELINE_FIELDS).
_MIRRORED_FIELDS = ("summary", "description", "priority", "status", "assignee")


def _mirrored_equal(baseline: Mapping[str, Any], prev_val: Mapping[str, Any] | None) -> bool:
    """True when the stored baseline matches the prev_snapshot entry on all five
    mirrored fields — i.e. the shadow source agrees with the live consumer."""
    prev = prev_val or {}
    return all(baseline.get(f) == prev.get(f) for f in _MIRRORED_FIELDS)


def run_dual_write_shadow(
    binding_store: Any,
    curr_snapshot: Mapping[str, Any],
    prev_snapshot: Mapping[str, Any],
    *,
    sync_logger: Any = None,
) -> dict:
    """Advance per-binding baselines from ``curr_snapshot`` and log the equivalence
    check against ``prev_snapshot`` (see module docstring).

    Returns the shadow record ``{equal, divergent, seeded, divergent_keys}``. Emits
    ``baseline_shadow_check`` (and, on divergence, ``baseline_shadow_divergence``)
    via ``sync_logger`` when one is supplied. Only confirmed bindings whose Jira key
    is present in the current fetch window are considered (an out-of-window key has
    no fresh value to advance to this pass).
    """
    equal = 0
    divergent = 0
    seeded = 0
    divergent_keys: list[str] = []

    for local_id, entry in binding_store.all_bindings().items():
        if entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if not jira_key or jira_key not in curr_snapshot:
            continue

        baseline = binding_store.get_baseline(local_id)
        prev_val = prev_snapshot.get(jira_key)
        if baseline is None:
            # First observation of this binding under the shadow — nothing to
            # compare yet; the seed below establishes the ancestor.
            seeded += 1
        elif _mirrored_equal(baseline, prev_val):
            equal += 1
        else:
            divergent += 1
            divergent_keys.append(jira_key)

        # Dual-write: advance the baseline to the current snapshot value, exactly
        # as prev_snapshot is advanced (copy2) at pass end. set_baseline filters to
        # the mirrored fields and no-ops on an unbound id.
        binding_store.set_baseline(local_id, curr_snapshot[jira_key])

    record = {
        "equal": equal,
        "divergent": divergent,
        "seeded": seeded,
        "divergent_keys": divergent_keys[:20],
    }
    if sync_logger is not None:
        sync_logger.log("baseline_shadow_check", **record)
        if divergent:
            sync_logger.log(
                "baseline_shadow_divergence",
                divergent=divergent,
                keys=divergent_keys[:20],
            )
    # Story a118: the SyncLogger writes only the gitignored, ephemeral
    # bridge_state/sync-log; emit a durable RECON: line to STDERR (the same stream
    # as applier.py's RECON: diagnostics) so the >=10-clean-shadow-pass rollout
    # streak is derivable from the GHA reconcile-bridge run logs
    # (`gh run view <id> --log | grep baseline_shadow_check`).
    print(  # noqa: T201 — operator-facing rollout diagnostic on the RECON: stderr stream
        f"RECON: baseline_shadow_check divergent={divergent} equal={equal} seeded={seeded}",
        file=sys.stderr,
        flush=True,
    )
    return record
