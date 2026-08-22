"""RP-06 S4 — plan-review Pass-1 discovery over the shared typed kernel.

These tests pin the observable behaviour of cutting plan-review's Pass-1 checkpointing
and outcome-classification over to :mod:`rebar.llm.review_kernel` while preserving the
existing finder mechanics (facet packing, size ladder, shed-to-budget, cache warming,
bounded concurrency). They assert ONLY observable behaviour: the checkpoint identity's
sensitivity to every material/code/policy/prompt/contract/model/mode/topology/context/
dependency/namespace input, success-only reuse, the false-success fix (a failed local
unit is never serialized as a clean checkpoint), the reducer-ignored discovery journal,
and the narrow ``review-plan --status`` contract.

OFFLINE: a fake ``rebar.llm.Runner`` drives the finder; no model, no network.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import registry, sidecar, sizing
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.pass1 import run_pass1
from rebar.llm.review_kernel import (
    DISCOVERY_NAMESPACE_VERSION,
    CheckpointEnvelope,
    Usage,
)
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Acceptance Criteria\n- [ ] X is observably true\n- [ ] another check\n"
)

_IDS_RE = re.compile(r"\(ids: ([^)]*)\)")


def _ctx(description: str = _GOOD_AC, *, repo_root=None, **kw) -> PlanContext:
    return PlanContext(
        ticket_id="abcd-0000-0000-0001",
        ticket_type="task",
        title="Build X",
        description=description,
        repo_root=repo_root,
        **kw,
    )


def _cfg() -> LLMConfig:
    return dataclasses.replace(LLMConfig.from_env(repo_root=None), model="claude-opus-4-8")


def _chunk_ids(req) -> list[str]:
    m = _IDS_RE.search(req.instructions)
    assert m, f"no criterion-id header in: {req.instructions!r}"
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


class _FailCriteriaRunner:
    """A fake ``Runner`` that RAISES a plain (non-context, non-systemic) error for any
    call whose chunk touches one of ``fail_ids``, and otherwise returns one finding per
    covered criterion. Used to drive the false-success / partial-failure paths."""

    name = "fail-criteria"

    def __init__(self, fail_ids: set[str]) -> None:
        self._fail = set(fail_ids)

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req) -> dict:
        ids = _chunk_ids(req)
        if self._fail.intersection(ids):
            raise RuntimeError("the model call failed for an unrelated reason")
        return {"findings": [{"finding": f"f-{c}", "criteria": [c]} for c in ids]}


def _healthy() -> FakeRunner:
    return FakeRunner(structured={"analysis": "", "findings": []})


# ── AC4 (core): the checkpoint identity is stable and input-sensitive ──────────
def _identity(ctx, chunk, **kw) -> str:
    base = dict(material="MAT", model="m", agentic=False)
    base.update(kw)
    return sizing.checkpoint_identity(ctx, chunk=chunk, **base)


def test_checkpoint_identity_stable_and_material_sensitive():
    ctx = _ctx()
    chunk = [{"id": "E2"}]
    a = _identity(ctx, chunk)
    assert a == _identity(ctx, chunk), "identity must be deterministic for identical inputs"
    assert a != _identity(ctx, chunk, material="OTHER"), "material must change the identity"
    assert a != _identity(ctx, [{"id": "E1"}]), "the criterion id set must change the identity"


# ── AC5 (core): only a content-identical SUCCESS envelope seeds reuse ──────────
def test_success_envelope_roundtrips_and_only_success_reuses(tmp_path):
    ctx = _ctx(repo_root=str(tmp_path))
    chunk = [{"id": "E2"}]
    digest = _identity(ctx, chunk)
    assert sizing.load_checkpoint(ctx, digest) is None, "cold cache must miss"

    success = CheckpointEnvelope(
        unit_id="single:E2",
        kind="success",
        digest=digest,
        namespace_version=DISCOVERY_NAMESPACE_VERSION,
        content=[{"finding": "x", "criteria": ["E2"]}],
        usage=Usage(input_tokens=1, output_tokens=1, requests=1),
    )
    assert sizing.save_checkpoint(ctx, success) is True
    got = sizing.load_checkpoint(ctx, digest)
    assert got is not None and got.content[0]["finding"] == "x", "a success must resume"

    # A non-reusable (failed) envelope stored at its digest must NOT seed reuse.
    failed_digest = _identity(ctx, [{"id": "E4"}], agentic=True)
    failed = dataclasses.replace(success, unit_id="agent:E4", kind="failed", digest=failed_digest)
    sizing.save_checkpoint(ctx, failed)
    assert sizing.load_checkpoint(ctx, failed_digest) is None, "a failed envelope is never reused"


# ── AC3 (core): a failed local unit is NEVER serialized as a clean checkpoint ──
def test_failed_chunk_is_not_checkpointed_and_reruns(tmp_path):
    ctx = _ctx(repo_root=str(tmp_path))
    cfg = _cfg()
    agent = [registry.by_id()["E4"]]  # a single-criterion AGENT unit
    runner = _FailCriteriaRunner({"E4"})

    cov1: dict = {}
    run_pass1(ctx, cfg, runner, [], list(agent), cov1)
    assert cov1["checkpoint"]["chunks_resumed"] == 0
    # The failed unit left NO checkpoint, so a second run re-invokes it (no resume).
    cov2: dict = {}
    run_pass1(ctx, cfg, runner, [], list(agent), cov2)
    assert cov2["checkpoint"]["chunks_resumed"] == 0, "a failed unit must not be resumable"
    # The discovery journal records the failure as a failed outcome (safe trace only).
    trace = {t["unit_id"]: t for t in cov2["discovery_trace"]}
    assert any(t["kind"] == "failed" for t in trace.values()), cov2["discovery_trace"]


# ── AC6 (core): the reducer-ignored review journal carries the safe trace ──────
def test_discovery_journal_persisted_in_sidecar_payload():
    trace = [
        {
            "unit_id": "single:E2",
            "kind": "success",
            "namespace_version": DISCOVERY_NAMESPACE_VERSION,
            "usage": {"input_tokens": 1, "output_tokens": 1, "requests": 1},
            "reason": None,
            "lineage": {"dependencies": [], "digest": "d" * 64},
        }
    ]
    verdict = {
        "verdict": "PASS",
        "ticket_id": "abcd-0000-0000-0001",
        "ticket_type": "task",
        "blocking": [],
        "advisory": [],
        "overflow": [],
        "indeterminate": [],
        "dropped": [],
        "coaching": [],
        "coverage": {"discovery_trace": trace},
    }
    payload = sidecar.build_payload(verdict, material="abc")
    journal = payload["discovery_journal"]
    assert journal["version"] == sidecar.DISCOVERY_JOURNAL_VERSION
    assert journal["namespace_version"] == DISCOVERY_NAMESPACE_VERSION
    assert [u["unit_id"] for u in journal["units"]] == ["single:E2"]
    # The safe trace never carries content/prompt/provider bodies.
    assert "content" not in journal["units"][0]


# ── restored held-out edge + E2E oracle ──


def test_corrupt_and_legacy_checkpoints_zero_reuse(tmp_path):
    ctx = _ctx(repo_root=str(tmp_path))
    chunk = [{"id": "E2"}]
    digest = _identity(ctx, chunk)
    cache_dir = sizing._checkpoint_dir(ctx)
    assert cache_dir is not None
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Corrupt JSON at the digest path → miss (recompute), never a crash.
    (cache_dir / f"{digest}.json").write_text("{not json", encoding="utf-8")
    assert sizing.load_checkpoint(ctx, digest) is None

    # A legacy namespace_version envelope → miss (ignored, not translated).
    legacy = {
        "unit_id": "single:E2",
        "kind": "success",
        "digest": digest,
        "namespace_version": DISCOVERY_NAMESPACE_VERSION + 999,
        "content": [{"finding": "stale"}],
        "usage": {"input_tokens": 0, "output_tokens": 0, "requests": 0},
    }
    import json as _json

    (cache_dir / f"{digest}.json").write_text(_json.dumps(legacy), encoding="utf-8")
    assert sizing.load_checkpoint(ctx, digest) is None


def test_digest_changes_on_every_identity_axis():
    ctx = _ctx()
    chunk = [{"id": "E2"}]
    base = _identity(ctx, chunk)
    variants = {
        "material": _identity(ctx, chunk, material="MAT2"),
        "model": _identity(ctx, chunk, model="other-model"),
        "mode": _identity(ctx, chunk, agentic=True),
        "criterion": _identity(ctx, [{"id": "E1"}]),
        "policy": _identity(ctx, chunk, policy_digest="policy-2"),
        "code_ref": _identity(ctx, chunk, code_ref="deadbeef"),
        "topology": _identity(ctx, chunk, topology_digest="topo-2"),
        "context": _identity(ctx, chunk, extra_context="G5 decomposition block"),
    }
    for axis, digest in variants.items():
        assert digest != base, f"{axis} must change the checkpoint identity"
    # Distinct axes must not collide with one another.
    assert len(set(variants.values())) == len(variants), "identity axes must be independent"


def test_partial_failure_preserves_successes_and_reruns_failed(tmp_path):
    ctx = _ctx(repo_root=str(tmp_path))
    cfg = _cfg()
    # Two independent single-criterion AGENT units; E4 fails, E5-ish (use another AGENT id).
    agent_ids = [
        c["id"] for c in registry.load_criteria(repo_root=None) if registry.exec_tier(c) == "AGENT"
    ]
    assert len(agent_ids) >= 2, agent_ids
    ok_id, fail_id = agent_ids[0], agent_ids[1]
    agent = [registry.by_id()[ok_id], registry.by_id()[fail_id]]
    runner = _FailCriteriaRunner({fail_id})

    cov1: dict = {}
    findings1 = run_pass1(ctx, cfg, runner, [], list(agent), cov1)
    # The successful criterion produced its finding; the failed one contributed none.
    got_ids = {c for f in findings1 for c in f.get("criteria", [])}
    assert ok_id in got_ids and fail_id not in got_ids

    # Second run: the success RESUMES from checkpoint; the failure re-invokes.
    cov2: dict = {}
    run_pass1(ctx, cfg, runner, [], list(agent), cov2)
    assert cov2["checkpoint"]["chunks_resumed"] >= 1
    kinds = {t["unit_id"]: t["kind"] for t in cov2["discovery_trace"]}
    assert any(k in ("resumed",) for k in kinds.values())
    assert any(k == "failed" for k in kinds.values())


def test_shadow_parity_findings_and_batch_plan_unchanged(tmp_path):
    """The migration is behaviour-preserving for a healthy run: the SAME observed calls
    produce the SAME findings and the SAME established coverage keys."""
    ctx = _ctx(repo_root=str(tmp_path))
    cfg = _cfg()
    from rebar.llm.plan_review.orchestrator import route_criteria

    single, agent = route_criteria(ctx)
    routed = [c["id"] for c in single + agent]
    fake = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in routed],
        }
    )
    cov: dict = {}
    findings = run_pass1(ctx, cfg, fake, single, agent, cov)
    assert findings, "healthy run must produce findings"
    # Established coverage keys are all still present (nothing dropped by the migration).
    for key in ("chunks", "batch_plan", "budget", "checkpoint", "usage"):
        assert key in cov, key
    # The new journal sits alongside, never replacing the established keys.
    assert "discovery_trace" in cov
    # Every routed criterion's finding survived the healthy pass.
    got = {c for f in findings for c in f.get("criteria", [])}
    assert set(routed).issubset(got)


def test_review_plan_status_stays_narrow(tmp_path):
    from rebar.llm.plan_review import attest_gate

    status = attest_gate.plan_review_status("abcd-0000-0000-0001", repo_root=str(tmp_path))
    assert set(status.keys()) == {
        "ok",
        "verdict",
        "reason",
        "verified_at_sha",
        "signed_at",
        "currency_basis",
    }


def test_shed_and_cancel_units_are_not_reusable(tmp_path):
    """Only success/resumed reuse; a shed or cancelled unit leaves no reusable envelope."""
    ctx = _ctx(repo_root=str(tmp_path))
    digest = _identity(ctx, [{"id": "E4"}], agentic=True)
    for kind in ("shed", "cancelled", "skipped"):
        env = CheckpointEnvelope(
            unit_id="agent:E4",
            kind=kind,
            digest=digest,
            namespace_version=DISCOVERY_NAMESPACE_VERSION,
            content=[{"finding": "x"}],
            usage=Usage(),
        )
        sizing.save_checkpoint(ctx, env)
        assert sizing.load_checkpoint(ctx, digest) is None, f"{kind} must not be reused"
