"""Binding-store-driven acting walk (drift classes A + C; epic 3006-e198).

The level-triggered controller step. Everywhere else the reconciler iterates the
ACTIVE local snapshot (``rebar list`` — archived/deleted tickets are dropped), so
a bound pair whose local ticket has LEFT the active set is invisible to the field
differ. That blind spot is the shared root cause of two drift classes:

* **class A (444d)** — a locally archived/deleted ticket never drives its still-live
  Jira issue to a terminal status (stranded live issues).
* **class C (13eb)** — a confirmed binding whose Jira issue was deleted is never
  garbage-collected once the local ticket is archived (dead entries accumulate).

Both are healed by ONE iteration over the BINDING STORE that *targeted-reads* the
off-snapshot local ticket (13eb's contract: "SAME walk as 444d — one shared
iteration, not two loops"). Each off-snapshot binding is classified by the pure
:func:`classify` and dispatched:

* ``TERMINAL_TRANSITION`` → an ordinary outbound ``update {status: Done}`` mutation
  (the existing transition apply path handles it — no new MutationAction).
* ``PROBE_GET`` / ``RETIRE_AFTER_GRACE`` → a bounded direct GET; a CONFIRMED 404
  counted to grace retires the binding (reversible soft-delete). Reuses the
  binding-store absence machinery UNCHANGED (``note_absent`` / ``clear_absent``).

Safety rails (ADR 0028 §1; lessons L13/L16/L17):

* **Snapshot-absence is NEVER deletion.** A key merely absent from the fetch window
  routes to a bounded direct GET; only a proven 404 counted to grace retires. A
  ``TRANSPORT_ERROR`` defers without touching the counter.
* **The circuit breaker is evaluated over the whole pass BEFORE any state
  mutation.** A fetch/JQL regression that would mass-retire or mass-transition
  trips :func:`check_blast_radius` and the walk emits NOTHING and advances NO
  counter (Argo ``allowEmpty`` / Entra deletion threshold; 13eb guardrail #4).

The walk is READ-ONLY in phase 1 (build a Decision per binding), breaker-gated in
phase 2, and only mutates state in phase 3. Retirement and counter increments are
side effects on the binding store (as in the existing bound-but-absent path);
terminal transitions are returned as typed :class:`Mutation` objects for the
unified applier.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# The convergence-foundation grace default (twin of binding_store /
# classify defaults; kept in lock-step).
_DEFAULT_ABSENT_RETIRE_GRACE = 3

# The Jira workflow status a terminal local ticket is driven to (ADR 0029: the
# reverse status map treats Done as terminal; the annotation labels preserve the
# lossless local status).
_TERMINAL_JIRA_STATUS = "Done"


@dataclass
class BindingWalkResult:
    """The outcome of one binding-store acting walk.

    ``mutations`` are the terminal-transition mutations to feed the applier;
    ``retired`` / ``probed`` / ``alerted`` name the Jira keys the walk acted on
    (for logging + tests). ``census`` is the decision-census record and
    ``breaker`` the blast-radius verdict; ``refused`` is True when the breaker
    aborted the acting phase (no mutation, no counter advance).
    """

    mutations: list[Any] = field(default_factory=list)
    census: dict = field(default_factory=dict)
    breaker: Any = None
    refused: bool = False
    retired: list[str] = field(default_factory=list)
    probed: list[str] = field(default_factory=list)
    alerted: list[str] = field(default_factory=list)


def _resolve_grace() -> int:
    """Resolve the consecutive-404 retire grace (matches binding_store)."""
    raw = os.environ.get("RECONCILER_ABSENT_RETIRE_GRACE")
    if raw is None:
        return _DEFAULT_ABSENT_RETIRE_GRACE
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_ABSENT_RETIRE_GRACE


def compute_binding_walk_mutations(
    binding_store: Any,
    curr_snapshot: Mapping[str, Any],
    active_local_ids: set[str],
    *,
    client: Any,
    local_reader: Callable[[str], Mapping[str, Any] | None],
    max_acting_fraction: float,
    classify_mod: Any,
    mutation_mod: Any,
    outbound_differ_mod: Any,
    grace: int | None = None,
    probe_get: Callable[[Any, str], Any] | None = None,
    persist: bool = True,
) -> BindingWalkResult:
    """Walk the binding store and heal drift classes A + C (see module docstring).

    Args:
        binding_store: the live :class:`BindingStore` (all_bindings / get_baseline /
            note_absent / clear_absent / confirmed_count).
        curr_snapshot: ``{jira_key: fields}`` — this pass's fetch window.
        active_local_ids: local ids present in the active snapshot; their bindings
            are ALREADY reconciled by the field differ and its bound-but-absent
            probe, so the walk skips them (no double-processing).
        client: the Jira client for bounded direct GETs (None ⇒ cannot probe, so an
            off-window key stays ``ABSENT_IN_WINDOW`` and is deferred).
        local_reader: ``local_id → local ticket dict | None`` — a targeted read of
            an off-snapshot (archived/deleted/gone) ticket.
        max_acting_fraction: circuit-breaker cap on the acting fraction.
        classify_mod: the ``classify`` module (injected — the caller loads it via the
            reconciler loader so this stays free of the test-package import shadow).
        mutation_mod: the ``mutation`` module (typed Mutation vocabulary).
        outbound_differ_mod: the ``outbound_differ`` module (its ``_safe_get_issue``
            probe + ``_DELETED`` / ``_TRANSPORT_ERROR`` sentinels are reused UNCHANGED).
        grace: consecutive-404 retire threshold (defaults to the env/3 resolution).
        probe_get: injectable direct-GET (defaults to ``outbound_differ._safe_get_issue``).
        persist: when False (dry-run / reconcile-check), the walk COMPUTES the plan
            (terminal-transition mutations + a predicted census) but performs NO
            binding-store side effects — no counter advance, no retirement. The
            terminal transitions ride the plan and are applied only by a persisting
            pass. Live grace counting therefore only happens in write mode.

    Returns:
        A :class:`BindingWalkResult`.
    """
    mut_mod = mutation_mod
    ob_mod = outbound_differ_mod

    if grace is None:
        grace = _resolve_grace()

    def _probe(cli: Any, key: str) -> Any:
        # Resolve the direct-GET lazily so a pass with nothing to probe never
        # touches ``outbound_differ._safe_get_issue`` (keeps degenerate/mock
        # environments and no-op passes cheap).
        fn = probe_get if probe_get is not None else ob_mod._safe_get_issue
        return fn(cli, key)

    ObservedJira = classify_mod.ObservedJira
    JiraObservation = classify_mod.JiraObservation
    DecisionKind = classify_mod.DecisionKind

    # ---- phase 1: READ-ONLY classification of every off-snapshot binding -----
    plans: list[tuple[str, str, Any, Any]] = []  # (local_id, jira_key, obs, decision)
    for local_id, entry in binding_store.all_bindings().items():
        # A pending binding is owned by the write-ahead recovery path.
        if entry.get("state") == "pending":
            continue
        jira_key = entry.get("jira_key")
        if not jira_key:
            continue
        # Active-snapshot pairs are handled by the field differ (+ its own
        # bound-but-absent probe); the walk only owns the off-snapshot ones.
        if local_id in active_local_ids:
            continue

        local = local_reader(local_id)

        if jira_key in curr_snapshot:
            obs = JiraObservation(
                state=ObservedJira.PRESENT, key=jira_key, fields=curr_snapshot[jira_key]
            )
        elif client is None:
            # No client ⇒ cannot resolve absence; treat as absent-in-window and
            # defer (never a deletion — ADR 0028 §1).
            obs = JiraObservation(state=ObservedJira.ABSENT_IN_WINDOW, key=jira_key)
        else:
            got = _probe(client, jira_key)
            if got is ob_mod._DELETED:
                obs = JiraObservation(state=ObservedJira.CONFIRMED_404, key=jira_key)
            elif got is ob_mod._TRANSPORT_ERROR:
                obs = JiraObservation(state=ObservedJira.TRANSPORT_ERROR, key=jira_key)
            else:
                # HTTP 200 — alive but out of the fetch window.
                obs = JiraObservation(state=ObservedJira.PRESENT, key=jira_key, fields=got)

        baseline = binding_store.get_baseline(local_id)
        decision = classify_mod.classify(local, obs, entry, baseline, grace=grace)
        plans.append((local_id, jira_key, obs, decision))

    # Nothing off-snapshot to act on — skip the breaker (and its confirmed_count
    # read) entirely so a no-op pass is truly cheap and side-effect-free.
    if not plans:
        return BindingWalkResult(census=classify_mod.census([]))

    # ---- phase 2: circuit breaker over the WHOLE pass, BEFORE any mutation ----
    decisions = [p[3] for p in plans]
    total_bindings = binding_store.confirmed_count()
    breaker = classify_mod.check_blast_radius(decisions, total_bindings, max_acting_fraction)
    census = classify_mod.census(
        decisions, total_bindings=total_bindings, max_acting_fraction=max_acting_fraction
    )
    result = BindingWalkResult(census=census, breaker=breaker)
    if not breaker.allowed:
        # Refuse the entire acting phase — no transitions, no counter increments,
        # no retirements (mass-retire / mass-transition guard).
        result.refused = True
        return result

    # ---- phase 3: dispatch the allowed decisions into state mutations ---------
    for local_id, jira_key, obs, decision in plans:
        kind = decision.kind
        # An out-of-window key that GET-resolved ALIVE has its absence counter
        # reset (a transient window miss must never accrue to grace); this covers
        # both a terminal-transition target and any alive probe result.
        if persist and obs.state is ObservedJira.PRESENT and jira_key not in curr_snapshot:
            binding_store.clear_absent(jira_key)

        if kind is DecisionKind.TERMINAL_TRANSITION:
            result.mutations.append(
                mut_mod.Mutation(
                    direction=mut_mod.MutationDirection.outbound,
                    action=mut_mod.MutationAction.update,
                    target=jira_key,
                    payload={
                        "changed_fields": {"status": _TERMINAL_JIRA_STATUS},
                        "comments": [],
                        "labels": [],
                        "links": [],
                    },
                    provenance={
                        "source": "binding_walk",
                        "drift_class": "A",
                        "local_id": local_id,
                        "jira_key": jira_key,
                    },
                )
            )
        elif kind in (DecisionKind.PROBE_GET, DecisionKind.RETIRE_AFTER_GRACE):
            # The GET already happened in phase 1. Advance/retire ONLY on a
            # CONFIRMED 404 (ADR 0028 §2 — never on ABSENT_IN_WINDOW).
            if obs.state is ObservedJira.CONFIRMED_404:
                result.probed.append(jira_key)
                if kind is DecisionKind.RETIRE_AFTER_GRACE:
                    result.retired.append(jira_key)
                if persist:
                    # note_absent increments the counter and retires it (moves to
                    # bindings-retired.json + alert) when it reaches grace.
                    binding_store.note_absent(jira_key)
        elif kind is DecisionKind.ALERT:
            # bound + local vanished while Jira live — an anomaly; surface, do not act.
            result.alerted.append(jira_key)
        # SKIP_RETIRED / NOOP / SYNC_FIELDS (the last is unreachable off-snapshot):
        # nothing to do.

    return result
