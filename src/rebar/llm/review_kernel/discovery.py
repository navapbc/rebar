"""RP-06 S2 — the shared discovery-execution kernel.

A typed, dependency-aware kernel that executes a *stage* of independent-but-dependency-
ordered *discovery units* (each unit = one LLM operation against a locked rubric) and
produces trustworthy execution FACTS: every unit carries exactly one of six precise
outcome kinds, usage is accounted exactly, and success is checkpointable and resumable.

Both review gates (plan-review and the future code-review) consume this ONE kernel so the
execution facts — which units succeeded, resumed, were skipped, shed, failed, or cancelled
— cannot fork. A later failure never erases an earlier success; an empty result never
masquerades as success.

**Thin on purpose (the ADR 0065 "Burr tripwire").** No new concurrency/retry machinery:
no ``asyncio``/``threading``/``concurrent.futures``/``multiprocessing`` and no third-party
retry/workflow library. Ordering uses the stdlib :mod:`graphlib` exactly like
``rebar.llm.workflow.executor``; execution is a plain synchronous loop; cancellation is
cooperative via a caller-supplied ``cancelled`` callable, never threads.
"""

from __future__ import annotations

import graphlib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# Bumped when the checkpoint schema changes. Every envelope stamps it; a stored envelope
# with a different value is treated as a cache miss (legacy) rather than reused.
DISCOVERY_NAMESPACE_VERSION: int = 1

# The enumerable outcome vocabulary. Exactly six kinds, no more.
OUTCOME_KINDS: tuple[str, ...] = (
    "success",
    "resumed",
    "skipped",
    "shed",
    "failed",
    "cancelled",
)

_REUSABLE_KINDS: frozenset[str] = frozenset({"success", "resumed"})

# Symbolic, non-sensitive reason codes recorded on outcomes (never raw exception text).
REASON_LOCAL_EXHAUSTED = "local_operation_exhausted"
REASON_SYSTEMIC = "systemic_discovery_error"
REASON_DEP_UNSATISFIED = "dependency_unsatisfied"
REASON_BUDGET_SHED = "budget_shed"
REASON_CANCELLED = "cancelled"

_KNOWN_REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_LOCAL_EXHAUSTED,
        REASON_SYSTEMIC,
        REASON_DEP_UNSATISFIED,
        REASON_BUDGET_SHED,
        REASON_CANCELLED,
    }
)


# ── exceptions (the model-call boundary signals) ──────────────────────────────
class LocalOperationExhausted(Exception):
    """A LOCAL, unit-scoped failure: retries/budget for that one unit are exhausted.

    The executor records that unit ``failed``, preserves every prior success, keeps
    running other independent units, and marks that unit's dependents ``skipped``.
    """


class SystemicDiscoveryError(Exception):
    """A SYSTEMIC failure (e.g. provider init) that aborts the whole remaining stage.

    The executor stops dispatching further units and sets ``systemic_abort=True``.
    """


# ── value types ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Usage:
    """Exact, fieldwise usage accounting. ``Usage()`` is the zero/identity element."""

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def __add__(self, other: Any) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            requests=self.requests + other.requests,
        )

    def __radd__(self, other: Any) -> Usage:
        # Enables ``sum(usages)`` (which seeds with the int ``0``).
        if other == 0:
            return self
        return self.__add__(other)

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "requests": self.requests,
        }


@dataclass(frozen=True, kw_only=True)
class DiscoveryUnitPlan:
    """A frozen typed plan for one discovery unit (one LLM operation)."""

    unit_id: str
    dependencies: tuple[str, ...] = ()
    prompt_id: str
    contract_id: str
    model: str
    mode: str
    context_digest: str
    policy_digest: str
    blocking: bool = False
    budget_estimate: float = 0.0


@dataclass(frozen=True, kw_only=True)
class DiscoveryStagePlan:
    """A frozen typed plan for a stage of discovery units."""

    units: tuple[DiscoveryUnitPlan, ...]
    budget: float | None = None
    material: str = ""
    code_ref: str = ""
    topology_digest: str = ""

    def __post_init__(self) -> None:
        # 0.0 and negatives are likely-mistake sentinels; None means uncapped.
        if self.budget is not None and self.budget <= 0:
            raise ValueError(f"budget must be a positive float or None, got {self.budget!r}")


@dataclass(frozen=True)
class CheckpointEnvelope:
    """A frozen, digest-complete checkpoint record for one unit's committed outcome."""

    unit_id: str
    kind: str
    digest: str
    namespace_version: int
    content: Any
    usage: Usage

    @classmethod
    def identity_digest(
        cls, *, unit_plan: DiscoveryUnitPlan, stage_plan: DiscoveryStagePlan
    ) -> str:
        """Deterministic sha256 hex over the unit's identity tuple (stable across runs)."""
        payload = {
            "material": stage_plan.material,
            "code_ref": stage_plan.code_ref,
            "policy_digest": unit_plan.policy_digest,
            "prompt_id": unit_plan.prompt_id,
            "contract_id": unit_plan.contract_id,
            "model": unit_plan.model,
            "mode": unit_plan.mode,
            "context_digest": unit_plan.context_digest,
            "dependencies": sorted(unit_plan.dependencies),
            "topology_digest": stage_plan.topology_digest,
            "namespace_version": DISCOVERY_NAMESPACE_VERSION,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        payload = {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "digest": self.digest,
            "namespace_version": self.namespace_version,
            "content": self.content,
            "usage": self.usage.as_dict(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> CheckpointEnvelope | None:
        """Parse; return None on corrupt input OR a legacy ``namespace_version``."""
        try:
            data = json.loads(s)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("namespace_version") != DISCOVERY_NAMESPACE_VERSION:
            return None
        try:
            usage_raw = data["usage"]
            usage = Usage(
                input_tokens=int(usage_raw["input_tokens"]),
                output_tokens=int(usage_raw["output_tokens"]),
                requests=int(usage_raw["requests"]),
            )
            return cls(
                unit_id=str(data["unit_id"]),
                kind=str(data["kind"]),
                digest=str(data["digest"]),
                namespace_version=int(data["namespace_version"]),
                content=data["content"],
                usage=usage,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_reusable_success(self) -> bool:
        return self.kind in _REUSABLE_KINDS


@dataclass(frozen=True)
class UnitOutcome:
    """A frozen per-unit outcome. Carries NO gate-specific verdict."""

    unit_id: str
    kind: str
    usage: Usage
    reason: str | None = None
    envelope: CheckpointEnvelope | None = None


@dataclass(frozen=True)
class DiscoveryStageResult:
    """A frozen typed stage result. Carries NO gate-specific verdict."""

    outcomes: tuple[UnitOutcome, ...]
    usage: Usage
    systemic_abort: bool = False


# ── checkpoint store ──────────────────────────────────────────────────────────
class CheckpointStore(Protocol):
    """Structural type for a checkpoint store the executor can read/write."""

    def load_raw(self, digest: str) -> str | None: ...

    def put_raw(self, digest: str, raw: str) -> None: ...

    def save(self, envelope: CheckpointEnvelope) -> None: ...


@dataclass
class MemoryCheckpointStore:
    """An in-memory checkpoint store keyed by identity digest."""

    _data: dict[str, str] = field(default_factory=dict)

    def load_raw(self, digest: str) -> str | None:
        return self._data.get(digest)

    def put_raw(self, digest: str, raw: str) -> None:
        self._data[digest] = raw

    def save(self, envelope: CheckpointEnvelope) -> None:
        self._data[envelope.digest] = envelope.to_json()


RunUnit = Callable[[DiscoveryUnitPlan], "tuple[Any, Usage]"]
Cancelled = Callable[[], bool]


# ── ordering + shedding helpers ───────────────────────────────────────────────
def _static_order(units: tuple[DiscoveryUnitPlan, ...]) -> list[str]:
    """Dependency-respecting order, tie-broken deterministically by ``unit_id``."""
    ids = {u.unit_id for u in units}
    graph: dict[str, set[str]] = {u.unit_id: {d for d in u.dependencies if d in ids} for u in units}
    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter(graph)
    sorter.prepare()
    order: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        for node in ready:
            order.append(node)
            sorter.done(node)
    return order


def _dependency_rank(order: list[str], by_id: dict[str, DiscoveryUnitPlan]) -> dict[str, int]:
    """Longest-path depth from a root; higher rank = later/leafier unit."""
    rank: dict[str, int] = {}
    for uid in order:
        deps = [d for d in by_id[uid].dependencies if d in by_id]
        rank[uid] = 0 if not deps else 1 + max(rank[d] for d in deps)
    return rank


def _cascade_skipped(
    order: list[str], by_id: dict[str, DiscoveryUnitPlan], unavailable: set[str]
) -> set[str]:
    """Units transitively depending on an unavailable (shed/skipped) unit."""
    skipped: set[str] = set()
    for uid in order:
        if uid in unavailable:
            continue
        deps = by_id[uid].dependencies
        if any(d in unavailable or d in skipped for d in deps):
            skipped.add(uid)
    return skipped


def _retained_estimate(
    order: list[str], by_id: dict[str, DiscoveryUnitPlan], shed: set[str]
) -> float:
    skipped = _cascade_skipped(order, by_id, shed)
    return sum(
        by_id[uid].budget_estimate for uid in order if uid not in shed and uid not in skipped
    )


def _select_shed(
    plan: DiscoveryStagePlan,
    order: list[str],
    by_id: dict[str, DiscoveryUnitPlan],
    rank: dict[str, int],
) -> set[str]:
    """Choose the units to shed so the retained estimate fits a positive budget."""
    budget = plan.budget
    if budget is None:
        return set()
    if _retained_estimate(order, by_id, set()) <= budget:
        return set()

    def priority(uid: str) -> tuple[int, str]:
        return (rank[uid], uid)

    non_blocking = sorted(
        (u.unit_id for u in plan.units if not u.blocking), key=priority, reverse=True
    )
    blocking = sorted((u.unit_id for u in plan.units if u.blocking), key=priority, reverse=True)
    shed: set[str] = set()
    for uid in non_blocking + blocking:
        if _retained_estimate(order, by_id, shed) <= budget:
            break
        shed.add(uid)
    return shed


# ── execution ─────────────────────────────────────────────────────────────────
def _broken_dep(unit: DiscoveryUnitPlan, kinds: dict[str, str]) -> str | None:
    """The first (deterministically ordered) dependency that is not an available success."""
    broken = [dep for dep in unit.dependencies if kinds.get(dep) not in _REUSABLE_KINDS]
    return sorted(broken)[0] if broken else None


def _load_reusable(store: CheckpointStore | None, digest: str) -> CheckpointEnvelope | None:
    if store is None:
        return None
    raw = store.load_raw(digest)
    if raw is None:
        return None
    env = CheckpointEnvelope.from_json(raw)
    if env is not None and env.is_reusable_success():
        return env
    return None


@dataclass
class _ExecState:
    shed: set[str]
    kinds: dict[str, str]
    cancel_latched: bool = False
    systemic_latched: bool = False


def _predispatch_kind(unit: DiscoveryUnitPlan, state: _ExecState) -> tuple[str, str | None] | None:
    """Resolve a (skip/shed/cancel kind, reason) BEFORE any dispatch, or None to proceed."""
    if unit.unit_id in state.shed:
        return "shed", REASON_BUDGET_SHED
    if state.cancel_latched:
        return "cancelled", REASON_CANCELLED
    if state.systemic_latched:
        return "skipped", REASON_SYSTEMIC
    broken = _broken_dep(unit, state.kinds)
    if broken is not None:
        return "skipped", f"{REASON_DEP_UNSATISFIED}:{broken}"
    return None


def execute_stage(
    plan: DiscoveryStagePlan,
    run_unit: RunUnit,
    *,
    store: CheckpointStore | None = None,
    cancelled: Cancelled | None = None,
) -> DiscoveryStageResult:
    """Execute a stage's units in dependency order, producing typed per-unit facts."""
    by_id = {u.unit_id: u for u in plan.units}
    order = _static_order(plan.units)
    rank = _dependency_rank(order, by_id)
    state = _ExecState(shed=_select_shed(plan, order, by_id, rank), kinds={})

    outcomes: dict[str, UnitOutcome] = {}
    total_usage = Usage()

    for uid in order:
        unit = by_id[uid]
        pre = _predispatch_kind(unit, state)
        if pre is not None:
            pre_kind, pre_reason = pre
            state.kinds[uid] = pre_kind
            outcomes[uid] = UnitOutcome(
                unit_id=uid, kind=pre_kind, usage=Usage(), reason=pre_reason
            )
            continue
        if cancelled is not None and cancelled():
            state.cancel_latched = True
            state.kinds[uid] = "cancelled"
            outcomes[uid] = UnitOutcome(
                unit_id=uid, kind="cancelled", usage=Usage(), reason=REASON_CANCELLED
            )
            continue

        digest = CheckpointEnvelope.identity_digest(unit_plan=unit, stage_plan=plan)
        reused = _load_reusable(store, digest)
        if reused is not None:
            state.kinds[uid] = "resumed"
            outcomes[uid] = UnitOutcome(unit_id=uid, kind="resumed", usage=Usage(), envelope=reused)
            continue

        outcome = _dispatch_unit(unit, run_unit, digest, store, state)
        if outcome.kind == "success":
            total_usage = total_usage + outcome.usage
        outcomes[uid] = outcome

    ordered = tuple(outcomes[u.unit_id] for u in plan.units)
    return DiscoveryStageResult(
        outcomes=ordered, usage=total_usage, systemic_abort=state.systemic_latched
    )


def _dispatch_unit(
    unit: DiscoveryUnitPlan,
    run_unit: RunUnit,
    digest: str,
    store: CheckpointStore | None,
    state: _ExecState,
) -> UnitOutcome:
    """Dispatch one unit and commit its true outcome (in-flight is non-interruptible)."""
    uid = unit.unit_id
    try:
        content, usage = run_unit(unit)
    except SystemicDiscoveryError:
        state.systemic_latched = True
        state.kinds[uid] = "failed"
        return UnitOutcome(unit_id=uid, kind="failed", usage=Usage(), reason=REASON_SYSTEMIC)
    except LocalOperationExhausted:
        state.kinds[uid] = "failed"
        return UnitOutcome(unit_id=uid, kind="failed", usage=Usage(), reason=REASON_LOCAL_EXHAUSTED)

    envelope = CheckpointEnvelope(
        unit_id=uid,
        kind="success",
        digest=digest,
        namespace_version=DISCOVERY_NAMESPACE_VERSION,
        content=content,
        usage=usage,
    )
    if store is not None:
        store.save(envelope)
    state.kinds[uid] = "success"
    return UnitOutcome(unit_id=uid, kind="success", usage=usage, envelope=envelope)


# ── safe diagnostics ──────────────────────────────────────────────────────────
def _normalize_reason(reason: str | None, kind: str) -> str | None:
    if reason is None:
        return None
    return reason if reason in _KNOWN_REASON_CODES else kind


def unit_trace(outcome: UnitOutcome, *, unit_plan: DiscoveryUnitPlan) -> dict[str, Any]:
    """A SAFE, JSON-serializable diagnostic record — normalized data only.

    Never carries the envelope's ``content`` payload, prompt/ticket/context bodies, any
    provider payload, or a raw exception message — only a normalized reason CODE.
    """
    env = outcome.envelope
    return {
        "unit_id": outcome.unit_id,
        "kind": outcome.kind,
        "namespace_version": DISCOVERY_NAMESPACE_VERSION,
        "usage": outcome.usage.as_dict(),
        "reason": _normalize_reason(outcome.reason, outcome.kind),
        "lineage": {
            "dependencies": sorted(unit_plan.dependencies),
            "digest": env.digest if env is not None else None,
        },
    }
