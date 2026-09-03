"""Chunk-atomic checkpointing for Pass-1 finder units (extracted from :mod:`.sizing`).

A self-contained cluster over the shared :mod:`rebar.llm.review_kernel` discovery kernel
so an interrupted/restarted review RESUMES completed Pass-1 chunks instead of re-paying
for them: :func:`checkpoint_identity` builds the identity digest (via
:func:`_discovery_unit_plan` / :func:`_unit_id`), and :func:`load_checkpoint` /
:func:`save_checkpoint` read and atomically write the envelope under
:func:`_checkpoint_dir`. The digest binds the ticket MATERIAL fingerprint, the chunk's
criterion set (prompt id), the model/mode, the injected extra-context, and the
policy/code/topology refs, so any of those moving invalidates the cache; only a reusable
SUCCESS envelope seeds reuse.

Nothing here calls back into :mod:`.sizing`; the names are re-exported there so the
historical ``sizing.<name>`` call sites are unchanged.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from rebar.llm.review_kernel import (
    CheckpointEnvelope,
    DiscoveryStagePlan,
    DiscoveryUnitPlan,
)

from .det_floor import PlanContext


def _checkpoint_dir(ctx: PlanContext) -> Path | None:
    """The git-ignored per-ticket checkpoint cache dir (``.rebar/cache/plan-review/``),
    or None when there is no repo root to anchor it."""
    if not ctx.repo_root:
        return None
    return Path(ctx.repo_root) / ".rebar" / "cache" / "plan-review" / ctx.ticket_id


# The single registered discovery contract for a Pass-1 finder unit.
_CHECKPOINT_CONTRACT_ID = "plan_review_findings"


def _unit_id(chunk: list[dict], agentic: bool) -> str:
    """The stable unit id for a chunk: tier prefix + sorted criterion ids."""
    return ("agent:" if agentic else "single:") + ",".join(sorted(c["id"] for c in chunk))


def _discovery_unit_plan(
    *,
    chunk: list[dict],
    model: str | None,
    agentic: bool,
    extra_context: str = "",
    policy_digest: str = "",
) -> DiscoveryUnitPlan:
    """Build the frozen typed unit plan for one Pass-1 finder chunk. The ``prompt_id``
    is derived from the criterion ids so the identity changes with the criterion set;
    the ``context_digest`` binds the injected store-derived extra context."""
    ids = sorted(c["id"] for c in chunk)
    context_digest = (
        hashlib.sha256(extra_context.encode("utf-8")).hexdigest() if extra_context else ""
    )
    return DiscoveryUnitPlan(
        unit_id=_unit_id(chunk, agentic),
        prompt_id="plan-review:" + ",".join(ids),
        contract_id=_CHECKPOINT_CONTRACT_ID,
        model=str(model),
        mode="agent" if agentic else "single",
        context_digest=context_digest,
        policy_digest=policy_digest,
    )


def checkpoint_identity(
    *,
    material: str,
    chunk: list[dict],
    model: str | None,
    agentic: bool,
    extra_context: str = "",
    policy_digest: str = "",
    code_ref: str = "",
    topology_digest: str = "",
) -> str:
    """The deterministic identity digest for a Pass-1 chunk's checkpoint, over the
    shared kernel's :meth:`CheckpointEnvelope.identity_digest`. Any of the material,
    criterion set, model/mode, injected extra-context, or policy/code/topology refs
    moving changes the digest (so the stored checkpoint is invalidated)."""
    unit = _discovery_unit_plan(
        chunk=chunk,
        model=model,
        agentic=agentic,
        extra_context=extra_context,
        policy_digest=policy_digest,
    )
    stage = DiscoveryStagePlan(
        units=(unit,),
        material=material,
        code_ref=code_ref,
        topology_digest=topology_digest,
    )
    return CheckpointEnvelope.identity_digest(unit_plan=unit, stage_plan=stage)


def load_checkpoint(ctx: PlanContext, digest: str) -> CheckpointEnvelope | None:
    """Return the reusable-SUCCESS checkpoint envelope stored at ``digest`` (resume),
    else None. Best-effort: any read/parse error, corrupt JSON, legacy namespace, a
    digest mismatch, or a non-reusable kind ⇒ None (re-run the chunk)."""
    d = _checkpoint_dir(ctx)
    if d is None:
        return None
    try:
        path = d / f"{digest}.json"
        if path.is_file():
            env = CheckpointEnvelope.from_json(path.read_text(encoding="utf-8"))
            if env is not None and env.digest == digest and env.is_reusable_success():
                return env
    except Exception:  # noqa: BLE001 — checkpoint read is a best-effort resume optimization; any failure ⇒ no cached result (recompute)
        return None
    return None


def save_checkpoint(ctx: PlanContext, envelope: CheckpointEnvelope) -> bool:
    """Persist an envelope ATOMICALLY (tmp + rename) at its own digest so a restarted
    review resumes it. Best-effort: any write error → False (the review still
    proceeds). Callers only ever save successes; ``load_checkpoint`` is what refuses a
    non-reusable envelope on the way back out."""
    d = _checkpoint_dir(ctx)
    if d is None:
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=f".tmp-{envelope.digest}-", suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as handle:
                handle.write(envelope.to_json())
            Path(tmp).replace(d / f"{envelope.digest}.json")
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True
    except Exception:  # noqa: BLE001 — checkpoint write is a best-effort resume optimization; any failure ⇒ not cached (the review still proceeds)
        return False
