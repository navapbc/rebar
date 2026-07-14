"""B2: the plan-review gate as a v3 engine WORKFLOW (running, offline-testable).

Proves `src/rebar/llm/workflow/gates/plan-review.yaml` + its `uses` ops:
 * it validates + lints clean (v3, prompt-refs resolve);
 * `plan_review_assemble_criteria` routes the criteria (proportionate scrutiny + overlay
   triggering) and emits the per-criterion `include_<ID>` inclusion the batch's `when` reads
   — an overlay criterion is included/excluded per its trigger (the E5 advisory);
 * an end-to-end OFFLINE run (inject the B1 ProductionBatchRunner + a FakeRunner for the
   finder, and a canned AgentStepRunner for verify/coach) produces a plan_review_verdict-shaped
   result — NO live calls;
 * the DET-block short-circuit skips the LLM entirely (no finder/verify/coach calls) and yields
   the blocking verdict, mirroring the B3 completion-gate short-circuit;
 * decide/coach produce the expected verdict shape.

This proves the workflow RUNS and produces the right verdict shape (the workflow is now the
sole plan-review gate; the bespoke path it once mirrored was retired in story B-RETIRE).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import lint as _lint
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import AgentStepRunner, StepResult

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")
_TARGET = "T-1"

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)
# A plan that fires the T5a (performance) deterministic overlay trigger.
_PERF_AC = _GOOD_AC + "\nWe must cut the p99 latency on the hot path and add a cache.\n"


def _doc() -> dict:
    return _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))


def _state(*, ttype: str = "story", description: str = _GOOD_AC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Build X",
        "description": description,
        "deps": [],
    }


def _patch_reads(monkeypatch, state: dict) -> None:

    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])


class _CountingFinder(FakeRunner):
    """A FakeRunner (the Pass-1 finder seam ProductionBatchRunner drives) that counts calls
    so a test can assert the LLM was (or was NOT) reached."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def run(self, req):
        self.calls += 1
        return super().run(req)


class _CannedAgent(AgentStepRunner):
    """A no-token agent runner for the verify + coach PROMPT steps. Branches on the step's
    prompt id: the Pass-2 verifier returns one verification per finding (mid validity → a
    surviving ADVISORY); the Pass-4 coach returns one move pick. Counts calls."""

    def __init__(self):
        self.calls = 0
        # Which verifier prompt id the dynamic Pass-2 branch selected (B5): the agentic
        # variant when any finding is code-grounded, else the single-turn one.
        self.verifier_prompts: list[str] = []
        # Every prompt id this runner was asked to run (WS4: assert the coach prompt is
        # NOT invoked on a 0-surviving PASS).
        self.prompts_seen: list[str] = []

    def run(self, ctx) -> StepResult:
        self.calls += 1
        prompt = ctx.step.get("prompt")
        self.prompts_seen.append(prompt)
        if prompt in ("plan-review-verifier", "plan-review-verifier-agentic"):
            self.verifier_prompts.append(prompt)
            findings = ctx.inputs.get("findings") or []
            verifs = [
                {
                    "index": i,
                    "severity_attributes": {
                        "prod_impact": "medium",
                        "debt_impact": "medium",
                        "blast_radius": "module",
                        "likelihood": "medium",
                        "reversibility": "moderate",
                    },
                    "binary": {
                        "cited_reference_accurate": "na",
                        "is_verifiable": "yes",
                        "evidence_entails_finding": "yes",
                        "path_reachable": "yes",
                        "impact_follows_necessarily": "yes",
                        "no_viable_alternative_explanation": "yes",
                        "no_existing_mitigation": "yes",
                        "severity_claim_justified": "yes",
                    },
                }
                for i in range(len(findings))
            ]
            return StepResult(outputs={"verifications": verifs}, status="succeeded")
        if prompt == "plan-review-coach":
            return StepResult(
                outputs={
                    "notes": [{"move_id": "1", "subject": "the X design", "finding_refs": []}]
                },
                status="succeeded",
            )
        return StepResult(outputs={"_fake": True}, status="succeeded")


class _Rec(_ex.RunRecorder):
    """Captures every frame's recorded outputs (keyed by frame_key) for assertion."""

    def __init__(self):
        self.store: dict = {}

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _terminal_verdict(rec) -> dict | None:
    """The plan_review_verdict the workflow produced (the coach or passthrough arm output) —
    distinguished from intermediate frames by carrying both `verdict` (str) and `coverage`."""
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "coverage" in out and "ticket_id" in out:
            return out
    return None


def _run(monkeypatch, state, *, finder, agent, probe_criteria=None):
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner

    rec = _Rec()
    res = _ex.run_workflow(
        _doc(),
        {"ticket_id": _TARGET, "probe_criteria": list(probe_criteria or [])},
        recorder=rec,
        target_ticket=_TARGET,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=agent,
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    return rec, res


# ── lint + schema ─────────────────────────────────────────────────────────────
def test_workflow_validates_and_lints():
    doc = _doc()
    assert doc["schema_version"] == "3"
    assert _schema.validate_document(doc) == []
    findings = [
        str(f)
        for f in _lint.lint_workflow(_WF.read_text(), check_prompts=True)
        if f.severity != "warning"
    ]
    assert findings == [], findings


def _all_steps(steps):
    """Flatten every step in a workflow doc, recursing into branch/loop/map bodies."""
    for s in steps:
        if not isinstance(s, dict):
            continue
        yield s
        for block, *keys in (("branch", "then", "else"), ("loop", "body"), ("map", "body")):
            blk = s.get(block)
            if isinstance(blk, dict):
                for k in keys:
                    if isinstance(blk.get(k), list):
                        yield from _all_steps(blk[k])


def test_verify_step_carries_no_static_model_so_operator_override_is_honored():
    """WS2 (gawky-koi-grain): the verify steps must NOT pin a static `model:` in the YAML —
    the Sonnet downgrade is applied on cfg (`plan_review._verifier_cfg`) so an operator's
    explicit model wins. A static step `model:` would always beat cfg (resolve_model: step >
    workflow > cfg), silently breaking the override. This guards that invariant."""
    verify_steps = [
        s
        for s in _all_steps(_doc()["steps"])
        if str(s.get("prompt", "")).startswith("plan-review-verifier")
    ]
    assert verify_steps, "expected at least one plan-review-verifier step"
    offenders = [s["id"] for s in verify_steps if "model" in s]
    assert offenders == [], f"verify steps must not pin a static model: {offenders}"


# ── assemble_criteria routing + overlay inclusion (the E5 advisory) ───────────
def test_assemble_criteria_overlay_inclusion(monkeypatch):
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    op = STEP_REGISTRY["plan_review_assemble_criteria"]

    def _assemble(state) -> dict:
        _patch_reads(monkeypatch, state)
        ctx = StepContext(
            run_id="r",
            step_id="assemble",
            kind="scripted",
            step={},
            inputs={"ticket_id": _TARGET},
            workflow={},
            target_ticket=_TARGET,
            repo_root=None,
        )
        return op(ctx)

    # The T5a performance overlay applies at TASK level + fires on a deterministic perf signal.
    # No performance signal → EXCLUDED.
    out_clean = _assemble(_state(ttype="task", description=_GOOD_AC))
    assert out_clean["include_T5a"] is False
    assert "T5a" not in out_clean["routing"]["single_turn"] + out_clean["routing"]["agent_tier"]

    # A performance signal in the plan → the T5a overlay is INCLUDED (deterministic trigger).
    out_perf = _assemble(_state(ttype="task", description=_PERF_AC))
    assert out_perf["include_T5a"] is True
    routed = out_perf["routing"]["single_turn"] + out_perf["routing"]["agent_tier"]
    assert "T5a" in routed
    # A non-overlay mandatory criterion (E1) is always routed; ISF is never a batch criterion.
    assert out_perf["include_E1"] is True
    assert "include_ISF" not in out_perf


def test_assemble_criteria_probe_mode_restricts_to_allowlist(monkeypatch):
    """WS1 (odd-cocoa-chase): PROBE MODE — when `probe_criteria` is set, assemble FORCES
    exactly that allowlist (the cheap E4+G1G2 drift probe) and excludes everything else, so
    the finder batch runs only those criteria. Mirrors the retired bespoke drift probe."""
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    op = STEP_REGISTRY["plan_review_assemble_criteria"]
    state = _state(ttype="task", description=_PERF_AC)  # would normally route many criteria
    _patch_reads(monkeypatch, state)
    ctx = StepContext(
        run_id="r",
        step_id="assemble",
        kind="scripted",
        step={},
        inputs={"ticket_id": _TARGET, "probe_criteria": ["E4", "G1G2"]},
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    out = op(ctx)
    included = {cid for cid, on in out.items() if cid.startswith("include_") and on}
    assert included == {"include_E4", "include_G1G2"}, included
    # Even the otherwise-mandatory E1 and the fired T5a overlay are EXCLUDED in probe mode.
    assert out["include_E1"] is False and out["include_T5a"] is False
    assert out["routing"]["probe_criteria"] == ["E4", "G1G2"]


# ── end-to-end OFFLINE run → a plan_review_verdict-shaped PASS ────────────────
def test_e2e_offline_produces_verdict(monkeypatch):
    from rebar import schemas
    from rebar.llm.plan_review.orchestrator import assemble_context, route_criteria

    state = _state()
    _patch_reads(monkeypatch, state)
    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    routed_ids = [c["id"] for c in single + agent]
    assert routed_ids, "the routed criteria should be non-empty for a well-formed story"

    finder = _CountingFinder(
        structured={
            "analysis": "",
            "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in routed_ids],
        }
    )
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)

    assert res.status == "succeeded", res.error
    assert finder.calls > 0, "the Pass-1 finder must run on the precheck-passed arm"
    assert canned.calls >= 2, "the verify + coach prompt steps must both run"

    verdict = _terminal_verdict(rec)
    assert verdict is not None
    # Shape: validate against the canonical plan_review_verdict schema.
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    schemas.validator(schemas.PLAN_REVIEW_VERDICT).validate(verdict)
    assert verdict["verdict"] == "PASS"
    assert verdict["ticket_id"] == _TARGET
    assert verdict["blocking"] == []
    assert verdict["advisory"], "advisory findings should survive to the verdict"
    # Pass-4 coaching was rendered from the canned move pick (deterministic template).
    assert verdict["coaching"], "coaching should be rendered from the coach move pick"
    assert verdict["coaching"][0]["move_id"] == "1"
    assert "the X design" in verdict["coaching"][0]["coaching"]
    assert verdict["coverage"]["counts"]["blocking"] == 0
    # The then-arm ran: the coach prompt WAS invoked (there were surviving advisories).
    assert "plan-review-coach" in canned.prompts_seen


# A clean, well-formed plan that triggers NO DET advisory (P6 ac-quality passes: no
# compound-`and` criteria, no vague lexicon, mentions a test) → with no LLM findings the
# surfaced set is empty → a true 0-surviving PASS.
_CLEAN_DESC = (
    "## Why\nThe get endpoint returns the wrong status code for a missing record.\n\n"
    "## What\nReturn HTTP 404 from `src/api/get.py` when the record id is absent.\n\n"
    "## Scope\nThe single get handler.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] A request for a missing id returns HTTP 404\n"
    "- [ ] A request for an existing id returns HTTP 200\n"
    "- [ ] A unit test covers the missing-id path\n\n"
    "## Verification\nRun the api test module.\n"
)


def test_clean_pass_makes_no_coach_llm_call(monkeypatch):
    """WS4 (crimp-polar-jag): a PASS with 0 SURVIVING advisories takes the coach_gate else
    arm — coach renders with notes:[] and the `plan-review-coach` prompt step is NEVER run
    (no wasted coach LLM call), matching the bespoke pass4_coach early-return."""
    state = _state(description=_CLEAN_DESC)
    state["file_impact"] = [{"path": "src/api/get.py", "reason": "return 404 on missing id"}]
    # The finder surfaces NOTHING → 0 LLM findings; the clean plan raises no DET advisory
    # either → 0 surviving → clean PASS.
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)

    assert res.status == "succeeded", res.error
    verdict = _terminal_verdict(rec)
    assert verdict is not None and verdict["verdict"] == "PASS"
    assert verdict["advisory"] == [], verdict["advisory"]
    assert verdict["coaching"] == []
    # The coach prompt step made NO LLM call (the gate took the else arm).
    assert canned.prompts_seen.count("plan-review-coach") == 0


# ── producer/consumer: a real review run → the real sidecar emit → lossless v2 event (4e19) ─
def test_review_run_emits_lossless_v2_sidecar_via_real_emit_path(monkeypatch, tmp_path):
    """test-design §3 producer/consumer: a full OFFLINE plan review (stub LLM finder + canned
    agent) produces a verdict, which the REAL git-backed sidecar emit path persists as a
    ``REVIEW_RESULT`` event whose ``schema`` is ``plan_review_result_v2`` and whose every
    persisted finding carries ``evidence``, ``scenarios``, ``block_threshold``, and
    ``blocking_enabled`` (story 4e19). Unlike the direct ``build_payload`` unit tests, the
    findings here are the OUTPUT of the real four-pass pipeline, threaded through the real emit."""
    import subprocess

    import rebar
    from rebar.llm.plan_review import sidecar

    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(a, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("story", "Build X", description=_GOOD_AC, repo_root=str(repo))

    state = _state()
    state["ticket_id"] = tid
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.orchestrator import assemble_context, route_criteria

    ctx = assemble_context(tid, repo_root=None)
    single, agent = route_criteria(ctx)
    routed_ids = [c["id"] for c in single + agent]
    # The Pass-1 finder emits evidence + scenarios per finding (the prose v2 must persist).
    finder = _CountingFinder(
        structured={
            "analysis": "",
            "findings": [
                {
                    "finding": f"f-{cid}",
                    "criteria": [cid],
                    "evidence": [f"grounding quote for {cid}"],
                    "scenarios": [f"failure scenario for {cid}"],
                }
                for cid in routed_ids
            ],
        }
    )
    rec, res = _run(monkeypatch, state, finder=finder, agent=_CannedAgent())
    assert res.status == "succeeded", res.error
    verdict = _terminal_verdict(rec)
    assert verdict is not None and verdict["advisory"], "expected surviving advisory findings"
    verdict["ticket_id"] = tid

    # REAL emit path → read the persisted event's payload back.
    assert sidecar.emit(verdict, material="fp-xyz", repo_root=str(repo))
    got = sidecar.latest_review_result(tid, repo_root=str(repo))
    assert got is not None
    assert got["schema"] == "plan_review_result_v2"
    assert got["findings"], "the emitted sidecar must persist the review's findings"
    # Every persisted finding carries the v2 keys (present even when empty/None — e.g. a DET-tier
    # finding has no scenarios and is not threshold-decided).
    for sf in got["findings"]:
        for key in ("evidence", "scenarios", "block_threshold", "blocking_enabled"):
            assert key in sf, f"finding {sf.get('id')} is missing the v2 key {key!r}"
    # The LLM-tier findings (the four-pass output) carry the populated prose AND the resolved
    # decision boundary the Pass-3 decision applied — the enrichment that was previously dropped.
    llm = [sf for sf in got["findings"] if sf.get("tier") == "LLM"]
    assert llm, "expected at least one LLM-tier finding in the emitted sidecar"
    for sf in llm:
        assert sf.get("evidence"), f"LLM finding {sf.get('id')} lost its evidence prose"
        assert sf.get("scenarios"), f"LLM finding {sf.get('id')} lost its scenarios prose"
        assert sf.get("block_threshold") is not None, "LLM finding lost its block_threshold"
        assert sf.get("blocking_enabled") is not None, "LLM finding lost its blocking_enabled"


# ── P1/P5 DET block does NOT short-circuit: LLM runs, DET block merged → BLOCK ─
def test_p1_det_block_still_runs_llm_and_blocks(monkeypatch):
    # Reconciled with bespoke run_review (story B5): a P1 DET block (here, NO
    # `## Acceptance Criteria`) does NOT short-circuit the LLM — bespoke run_review only
    # stops before the LLM on a P8-too-big plan. So the four-pass review RUNS and the DET
    # block is merged at decide-time → a BLOCK verdict carrying the P1 block (+ any LLM
    # advisories), NOT a no-LLM deterministic short-circuit.
    state = _state(description="Just a body, no acceptance criteria at all here.")
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)

    assert res.status == "succeeded", res.error
    assert finder.calls > 0, "a P1 block does NOT short-circuit — the LLM still runs (parity)"
    verdict = _terminal_verdict(rec)
    assert verdict is not None
    assert verdict["verdict"] == "BLOCK"
    assert verdict["blocking"], "a DET block must itemize the failing check"
    assert any("P1" in (f.get("criteria") or []) for f in verdict["blocking"])


# ── exempt type short-circuits to PASS (no LLM) ──────────────────────────────
def test_bug_is_exempt_short_circuit(monkeypatch):
    state = _state(ttype="bug", description="A bug, gate-exempt.")
    finder = _CountingFinder(structured={"analysis": "", "findings": []})
    canned = _CannedAgent()
    rec, res = _run(monkeypatch, state, finder=finder, agent=canned)
    assert res.status == "succeeded", res.error
    assert finder.calls == 0 and canned.calls == 0
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"
    assert verdict["runner"] == "exempt"


# ── decide op shape (deterministic Pass-3 over batch findings + verifications) ─
def test_decide_op_partitions_findings(monkeypatch):
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    _patch_reads(monkeypatch, _state())
    op = STEP_REGISTRY["plan_review_decide"]
    # Two findings against an always-advisory criterion (E1): high-validity verifications →
    # both survive as ADVISORY (no blocking).
    findings = [
        {"finding": "f0", "criteria": ["E1"]},
        {"finding": "f1", "criteria": ["E1"]},
    ]
    verifs = [
        {
            "index": i,
            "severity_attributes": {
                "prod_impact": "medium",
                "blast_radius": "module",
                "likelihood": "medium",
                "reversibility": "moderate",
            },
            "binary": {
                "cited_reference_accurate": "na",
                "is_verifiable": "yes",
                "evidence_entails_finding": "yes",
                "path_reachable": "yes",
                "impact_follows_necessarily": "yes",
                "no_viable_alternative_explanation": "yes",
                "no_existing_mitigation": "yes",
                "severity_claim_justified": "yes",
            },
        }
        for i in range(2)
    ]
    ctx = StepContext(
        run_id="r",
        step_id="decide",
        kind="scripted",
        step={},
        inputs={
            "findings": findings,
            "verifications": verifs,
            "det_blocking": [],
            "det_advisory": [],
        },
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    out = op(ctx)
    assert set(out) >= {"blocking", "surfaced", "overflow", "indeterminate", "dropped"}
    assert out["blocking"] == []
    assert len(out["surfaced"]) == 2
    assert all(f["decision"] == "advisory" for f in out["surfaced"])
    assert all("id" in f for f in out["surfaced"])


def _high_validity_verif(i: int) -> dict:
    return {
        "index": i,
        "severity_attributes": {
            "prod_impact": "medium",
            "blast_radius": "module",
            "likelihood": "medium",
            "reversibility": "moderate",
        },
        "binary": {
            "cited_reference_accurate": "na",
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
    }


def test_contract_violation_sink_is_run_scoped():
    """The run-scoped sink (epic drag-gripe-brake): record is a no-op outside a scope (no crash,
    no leak), drains-and-clears inside one, and never leaks across scopes."""
    from rebar.llm.plan_review import orchestrator

    # Outside any scope: record is a no-op, drain is empty.
    orchestrator.record_contract_violation({"duplicates": [0]})
    assert orchestrator.drain_contract_violations() == []
    with orchestrator.collect_contract_violations():
        orchestrator.record_contract_violation({"duplicates": [1]})
        assert orchestrator.drain_contract_violations() == [{"duplicates": [1]}]
        assert orchestrator.drain_contract_violations() == []  # drain cleared it
    assert orchestrator.drain_contract_violations() == []  # scope exited → empty again


def test_decide_op_records_contract_violation_without_changing_outcome(monkeypatch):
    """plan_review_decide routes the raw verifications through the SHARED reshape seam: a
    duplicate / out-of-range index is RECORDED as a contract violation in the run-scoped sink
    (→ verdict coverage) but the partition is UNCHANGED (expand-contract: observability only,
    the conforming findings still decide normally)."""
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    _patch_reads(monkeypatch, _state())
    op = STEP_REGISTRY["plan_review_decide"]
    findings = [{"finding": "f0", "criteria": ["E1"]}, {"finding": "f1", "criteria": ["E1"]}]
    # index 0 duplicated, index 5 out of range (valid is {0, 1}), index 1 conforming.
    verifs = [
        _high_validity_verif(0),
        _high_validity_verif(0),
        _high_validity_verif(5),
        _high_validity_verif(1),
    ]
    ctx = StepContext(
        run_id="r",
        step_id="decide",
        kind="scripted",
        step={},
        inputs={
            "findings": findings,
            "verifications": verifs,
            "det_blocking": [],
            "det_advisory": [],
        },
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    with orchestrator.collect_contract_violations():
        out = op(ctx)
        recorded = orchestrator.drain_contract_violations()
    # Outcome UNCHANGED: both findings had a conforming verification (idx 0 + 1) → both advisory.
    assert len(out["surfaced"]) == 2
    assert out["blocking"] == []
    # The violation was recorded DISTINCTLY (a duplicate + an out-of-range index).
    assert recorded == [{"duplicates": [0], "unexpected": [5]}]


def test_explicit_config_reflected_in_verdict_model_runner():
    """586c: a caller's explicit config (non-default model/runner), resolved ONCE at the run
    boundary and published via gate_config, is reflected in the verdict's model/runner FIELDS —
    the divergence this ticket removes. plan_review_coach reads it via resolve_gate_config, so
    under an active scope the verdict reports the caller's identity, not the env's."""
    import dataclasses

    from rebar.llm.config import LLMConfig, gate_config
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    custom = dataclasses.replace(
        LLMConfig.from_env(), model="caller-model-xyz", runner="caller-runner"
    )
    ctx = StepContext(
        run_id="r",
        step_id="coach",
        kind="scripted",
        step={},
        inputs={
            "canonical_id": _TARGET,
            "ticket_type": "task",
            "blocking": [],
            "surfaced": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "notes": [],
            "det_coverage": {},
            "routing": {},
        },
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )
    op = STEP_REGISTRY["plan_review_coach"]
    with gate_config(custom):
        out = op(ctx)
    assert out["model"] == "caller-model-xyz"
    assert out["runner"] == "caller-runner"
    # Sanity: WITHOUT the scope it falls back to the env config (NOT the caller's identity).
    out_env = op(ctx)
    assert out_env["model"] != "caller-model-xyz"
