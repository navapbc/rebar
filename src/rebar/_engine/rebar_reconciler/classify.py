"""Pure level-triggered reconciliation classifier (epic 3006-e198).

This is the CONVERGENCE FOUNDATION: one pure decision function over the full
``(local × observed-jira × binding)`` state matrix, plus the circuit-breaker
and decision-census helpers that make a level-triggered controller safe and
observable. It is a **leaf** — no I/O, no imports from the differ / outbound /
inbound modules — so both consumers (the live pass, which ACTS on Decisions,
and the offline/online audit, which REPORTS them) share exactly one classifier
(healing the report-only/healing fork; drift class D).

The invariants encoded here are specified verbatim by ADRs 0026–0029:

* **ADR 0026** — a three-way-merge ``baseline`` (the per-pair last-synced field
  values) is the ONLY source of direction arbitration. ``local == baseline`` ⇒
  a Jira-side edit ⇒ mirror inbound; ``local != baseline`` ⇒ a local edit ⇒
  local-wins. An absent baseline degrades to local-wins (safe/lossy); a corrupt
  baseline fails the pass closed (enforced by the store, not here).
* **ADR 0027** — binding lifecycle ``pending → confirmed → retired``; adoption of
  an unbound Jira issue is GATED (retired-skip, identity audit, baseline seed,
  external-id idempotency). This module only decides ADOPT vs SKIP_RETIRED; the
  handler runs the gates.
* **ADR 0028** — snapshot-absence is NOT deletion. A bound key absent from the
  fetch window routes to ``PROBE_GET`` (a bounded direct GET), NEVER to a "gone"
  verdict. Only a CONFIRMED 404, counted to grace, yields ``RETIRE_AFTER_GRACE``.
  ``ObservedJira`` is a FOUR-way state, never a boolean.
* **ADR 0029** — the reverse status map is canonical; ``archived → Done`` outbound
  requires a non-oscillating inbound counterpart (handled in the differ, not
  here). The classifier computes outbound + inbound intent from the same observed
  snapshot so single-pass echo suppression stays expressible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

# Twin of binding_store._DEFAULT_ABSENT_RETIRE_GRACE (kept in lock-step; the
# classifier is pure so the configured grace is passed in, defaulting here).
_DEFAULT_ABSENT_RETIRE_GRACE = 3

# Local statuses that mean the ticket has left the active working set.
_TERMINAL_LOCAL_STATUSES = frozenset({"archived", "deleted"})

# Jira workflow statuses that are already terminal — a TERMINAL local ticket
# whose Jira issue is ALREADY Done needs no transition (idempotent steady state).
_JIRA_TERMINAL_STATUSES = frozenset({"Done", "Cancelled"})


class ObservedJira(Enum):
    """The four-way observation state of a Jira key (ADR 0028 §Decision).

    NEVER collapse to a boolean: ``ABSENT_IN_WINDOW`` (out of the fetch window,
    e.g. a Done issue older than ``_DONE_RECENT_CAP``) must never be treated as
    deleted — that would mass-retire the whole Done backlog.
    """

    PRESENT = "present"  # in the fetch snapshot; ``fields`` carries its values
    CONFIRMED_404 = "confirmed_404"  # a bounded direct GET returned 404 this pass
    ABSENT_IN_WINDOW = "absent_in_window"  # not in snapshot; liveness UNKNOWN
    TRANSPORT_ERROR = "transport_error"  # GET failed for a non-404 reason; defer


class LocalState(Enum):
    """The lifecycle state of the local side of a pair."""

    ACTIVE = "active"  # a live local ticket
    TERMINAL = "terminal"  # archived or status in {archived, deleted}
    ABSENT = "absent"  # no local ticket exists (None)


class DecisionKind(Enum):
    """The CLOSED set of reconciliation decisions. No open-ended fallthrough."""

    SYNC_FIELDS = "sync_fields"  # bound + active + present → run the field differ
    ADOPT = "adopt"  # unbound Jira-native issue → create local + bind (gated)
    TERMINAL_TRANSITION = "terminal_transition"  # local terminal → drive Jira → Done
    PROBE_GET = "probe_get"  # bound + absent-in-window → bounded direct GET
    RETIRE_AFTER_GRACE = "retire_after_grace"  # confirmed-404 at grace → retire binding
    SKIP_RETIRED = "skip_retired"  # unbound but retired key → do not re-adopt
    ALERT = "alert"  # anomaly (bound + local vanished) → surface, do not act
    NOOP = "noop"  # nothing to do / defer


# The acting decisions — the ones the circuit breaker counts and gates. A
# SYNC_FIELDS may or may not actually mutate (empty diff = no-op); it is NOT
# acting for blast-radius purposes. PROBE_GET is a bounded read, not acting.
ACTING_KINDS = frozenset(
    {DecisionKind.TERMINAL_TRANSITION, DecisionKind.RETIRE_AFTER_GRACE, DecisionKind.ADOPT}
)


@dataclass(frozen=True)
class JiraObservation:
    """A Jira key tagged with its four-way observation state (ADR 0028).

    ``fields`` is the snapshot value (present only when ``state`` is PRESENT).
    ``retired`` reflects whether the key is soft-deleted in the binding store —
    it gates ADOPT vs SKIP_RETIRED for an unbound present key (ADR 0027 §4a).
    """

    state: ObservedJira
    key: str = ""
    fields: Mapping | None = None
    retired: bool = False


@dataclass(frozen=True)
class Decision:
    """A single reconciliation decision. ``kind`` is a closed enum; ``payload``
    carries handler-specific detail (e.g. the target Jira status, the grace
    count) without widening the enum."""

    kind: DecisionKind
    reason: str
    payload: dict = field(default_factory=dict)

    @property
    def is_acting(self) -> bool:
        return self.kind in ACTING_KINDS


def local_state(local: Mapping | None) -> LocalState:
    """Derive the local lifecycle state from a local ticket dict (or None)."""
    if local is None:
        return LocalState.ABSENT
    if local.get("archived") or local.get("status") in _TERMINAL_LOCAL_STATUSES:
        return LocalState.TERMINAL
    return LocalState.ACTIVE


def classify(
    local: Mapping | None,
    jira: JiraObservation,
    binding: Mapping | None,
    baseline: Mapping | None,
    *,
    grace: int = _DEFAULT_ABSENT_RETIRE_GRACE,
) -> Decision:
    """Classify one ``(local, jira, binding, baseline)`` cell into a Decision.

    The FULL matrix is enumerated below; an unmatched cell RAISES (no silent
    default). ``grace`` is the configured consecutive-404 retire threshold
    (ADR 0027 L14); it is a keyword-only arg to preserve the documented
    ``classify(local, jira, binding, baseline)`` signature while keeping the
    function pure.

    ``baseline`` is not consumed for the lifecycle routing here (it drives
    field-level direction arbitration inside the SYNC_FIELDS handler, ADR 0026);
    it is part of the signature so both consumers pass the same tuple.
    """
    bound = binding is not None
    lstate = local_state(local)
    obs = jira.state

    # ---- unbound cells: adoption of a Jira-native issue (drift class B) ------
    if not bound:
        if obs is ObservedJira.PRESENT:
            if jira.retired:
                # ADR 0027 §4a — a just-retired key must not be resurrected into
                # a delete/re-adopt loop with the class-C GC.
                return Decision(
                    DecisionKind.SKIP_RETIRED,
                    reason="unbound present key is retired; skip to avoid re-adopt loop",
                    payload={"jira_key": jira.key},
                )
            return Decision(
                DecisionKind.ADOPT,
                reason="unbound Jira-native issue present in window; adopt (gated)",
                payload={"jira_key": jira.key},
            )
        # Unbound + not-present: nothing exists to adopt or act on. A confirmed
        # 404 / absent / transport-error on an unbound key is a no-op (there is
        # no binding to retire and no local ticket to reconcile).
        return Decision(
            DecisionKind.NOOP,
            reason=f"unbound key not present ({obs.value}); nothing to do",
            payload={"jira_key": jira.key},
        )

    # ---- bound cells: the binding-store walk (drift classes A + C) -----------
    # A pending binding is owned by the write-ahead recovery path, not this walk.
    if binding is not None and binding.get("state") == "pending":
        return Decision(
            DecisionKind.NOOP,
            reason="binding is pending; owned by write-ahead recovery",
            payload={"jira_key": jira.key},
        )

    if obs is ObservedJira.TRANSPORT_ERROR:
        # ADR 0028 §2 — a transport error is not evidence of anything; defer.
        return Decision(
            DecisionKind.NOOP,
            reason="bound key GET failed (transport error); defer to next pass",
            payload={"jira_key": jira.key},
        )

    if obs is ObservedJira.ABSENT_IN_WINDOW:
        # ADR 0028 §1 — absence is NOT deletion. Route to a bounded direct GET;
        # only a CONFIRMED 404 (next pass) can lead to retirement.
        return Decision(
            DecisionKind.PROBE_GET,
            reason="bound key absent from fetch window; probe liveness (not gone)",
            payload={"jira_key": jira.key},
        )

    if obs is ObservedJira.CONFIRMED_404:
        # ADR 0028 §2 / ADR 0027 L14 — deletion is proven only by a confirmed
        # 404 counted to grace. Predict whether THIS miss crosses the threshold
        # (note_absent enacts the increment); below grace we keep probing.
        prior = int((binding or {}).get("absent_404_count", 0))
        if prior + 1 >= grace:
            return Decision(
                DecisionKind.RETIRE_AFTER_GRACE,
                reason="bound key confirmed 404 at grace; retire binding (reversible)",
                payload={"jira_key": jira.key, "absent_404_count": prior + 1, "grace": grace},
            )
        return Decision(
            DecisionKind.PROBE_GET,
            reason="bound key confirmed 404 below grace; keep counting",
            payload={"jira_key": jira.key, "absent_404_count": prior + 1, "grace": grace},
        )

    # obs is PRESENT from here.
    if lstate is LocalState.ABSENT:
        # The Jira issue is live and bound, but the local ticket vanished from
        # the store — an anomaly (never expected; surface it, do not act).
        return Decision(
            DecisionKind.ALERT,
            reason="bound + local ticket absent while Jira live; anomaly",
            payload={"jira_key": jira.key},
        )

    if lstate is LocalState.TERMINAL:
        # Drift class A — a locally archived/deleted ticket must drive its Jira
        # issue to a terminal status. If Jira is ALREADY terminal, steady state.
        jira_status = _jira_status(jira.fields)
        if jira_status in _JIRA_TERMINAL_STATUSES:
            return Decision(
                DecisionKind.NOOP,
                reason="bound + local terminal + Jira already terminal; steady state",
                payload={"jira_key": jira.key, "jira_status": jira_status},
            )
        return Decision(
            DecisionKind.TERMINAL_TRANSITION,
            reason="bound + local terminal + Jira live; drive Jira to Done",
            payload={"jira_key": jira.key, "jira_status": jira_status},
        )

    # lstate is ACTIVE, obs is PRESENT — the normal bidirectional field sync.
    return Decision(
        DecisionKind.SYNC_FIELDS,
        reason="bound + local active + Jira present; run field differ",
        payload={"jira_key": jira.key},
    )


def direction_is_inbound(local_val: object, baseline_val: object) -> bool:
    """ADR 0026 direction arbitration for a single field.

    ``local == baseline`` ⇒ local is UNCHANGED since the last sync ⇒ any
    divergence from *current* Jira is a Jira-side (teammate) edit ⇒ **mirror it
    inbound / suppress the outbound push** (return True). ``local != baseline`` ⇒
    a genuine local edit ⇒ local-wins outbound (return False).

    This is the pure kernel of the three-way merge. The live consumer
    (``outbound_fields._local_matches_prev``, swapped from prev_snapshot to
    ``BindingStore.get_baseline`` by the rollout task) adds the shape-tolerant
    assignee/text comparisons on top; the direction RULE itself is exactly this.
    An absent baseline (None) is the "no ancestor" case: not equal to a real
    local value, so it degrades to local-wins (ADR 0026 §2) — safe/lossy.
    """
    return local_val == baseline_val


def _jira_status(fields: Mapping | None) -> str:
    """Best-effort extraction of the Jira workflow status name from a snapshot.

    Tolerates both the flat ``{"status": "To Do"}`` and the nested
    ``{"status": {"name": "To Do"}}`` shapes the snapshot may carry.
    """
    if not fields:
        return ""
    status = fields.get("status")
    if isinstance(status, Mapping):
        return str(status.get("name", "") or "")
    return str(status or "")


# ── circuit breaker (pure; live wiring is the rollout task) ──────────────────


@dataclass(frozen=True)
class BreakerVerdict:
    """The blast-radius verdict for a set of decisions."""

    allowed: bool
    acting_count: int
    total_bindings: int
    acting_fraction: float
    max_acting_fraction: float
    reason: str


def check_blast_radius(
    decisions: Iterable[Decision],
    total_bindings: int,
    max_acting_fraction: float,
) -> BreakerVerdict:
    """Refuse a pass whose ACTING decisions exceed ``max_acting_fraction`` of the
    binding population (prior art: Argo ``allowEmpty``, Entra deletion threshold).

    The 2026-07-03 census measured 1.14% acting — an 8.8× headroom under the
    default 0.10. A fetch/JQL regression that would mass-retire or mass-transition
    trips this BEFORE any mutation is applied (the rollout wires it into the apply
    path). ``total_bindings == 0`` allows (nothing to gate) rather than dividing
    by zero.
    """
    acting = sum(1 for d in decisions if d.is_acting)
    if total_bindings <= 0:
        fraction = 0.0
    else:
        fraction = acting / total_bindings
    allowed = fraction <= max_acting_fraction
    reason = (
        f"{acting}/{total_bindings} acting ({fraction:.4f}) "
        f"{'within' if allowed else 'EXCEEDS'} cap {max_acting_fraction:.4f}"
    )
    return BreakerVerdict(
        allowed=allowed,
        acting_count=acting,
        total_bindings=total_bindings,
        acting_fraction=fraction,
        max_acting_fraction=max_acting_fraction,
        reason=reason,
    )


# ── decision census (telemetry; emitted via SyncLogger by the first consumer) ─


def census(
    decisions: Iterable[Decision],
    *,
    total_bindings: int | None = None,
    max_acting_fraction: float | None = None,
) -> dict:
    """Summarise a pass's decisions into a structured census record.

    Returns ``{counts, acting_count, acting_pct, breaker}``. Emitted every pass in
    every mode via ``SyncLogger.log("decision_census", **census(...))`` — this is
    what makes the shadow phase and divergence alerting possible. The emit call
    lands with the first consumer; the record SHAPE + its unit test land here.
    """
    decisions = list(decisions)
    counts = {kind.value: 0 for kind in DecisionKind}
    for d in decisions:
        counts[d.kind.value] += 1
    acting_count = sum(counts[k.value] for k in ACTING_KINDS)
    total = total_bindings if total_bindings is not None else len(decisions)
    acting_pct = (acting_count / total * 100.0) if total else 0.0
    record: dict = {
        "counts": counts,
        "acting_count": acting_count,
        "acting_pct": round(acting_pct, 4),
        "total": total,
    }
    if max_acting_fraction is not None and total_bindings is not None:
        verdict = check_blast_radius(decisions, total_bindings, max_acting_fraction)
        record["breaker"] = {
            "allowed": verdict.allowed,
            "acting_fraction": round(verdict.acting_fraction, 6),
            "max_acting_fraction": verdict.max_acting_fraction,
            "reason": verdict.reason,
        }
    return record
