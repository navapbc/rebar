"""Deterministic (offline) lifecycle tests for the R4 lightweight BUG REVIEW TIER (epic 6982).

Before R4, the plan-review gate short-circuited EVERY bug to a bare exempt-PASS
(`workflow_ops.plan_review_precheck` → `orchestrator._exempt_verdict`), so a bug got no
substantive review. The bug tier instead runs the DET floor + the advisory `necessity` probe
(`registry.BUG_TIER_CRITERIA`). P1/P10 readiness-floor failures still block; non-readiness
findings stay advisory for ordinary bugs. These tests pin, with NO live LLM:

* precheck: an ordinarily sized, well-formed bug emits ``run_llm=True`` + ``det_blocking==[]`` +
  ``coverage.bug_tier`` (and is NOT the bare exempt short-circuit), while
  session_log/code_review/identity STAY exempt;
* routing: a bug's included LLM criteria are restricted to ``BUG_TIER_CRITERIA`` (= necessity),
  and every bug-tier criterion is advisory (so the tier is structurally non-blocking);
* an end-to-end OFFLINE run on a well-formed bug produces a PASS verdict with
  ``runner != "exempt"``, no blocking findings, and the necessity finding surfaced as advisory.
* the reported AC-less / Testing-less bug description keeps P1/P10 as blocking findings and
  short-circuits before any LLM pass.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.plan_review import registry
from rebar.llm.plan_review.det_floor import PlanContext, det_blocking_findings, run_det_floor
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review `uses` ops
from rebar.llm.workflow.executor import STEP_REGISTRY, AgentStepRunner, StepContext, StepResult

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/plan-review.yaml")
_TARGET = "BUG-1"
_BUG_DESC = (
    "## Reproduction Steps\n1. do X.\n2. observe Y.\n\n"
    "**Expected:** Z.\n**Actual:** not Z.\n\n"
    "## What\nFix the handler in `src/rebar/x.py`.\n\n"
    "## Acceptance Criteria\n- [ ] Y no longer happens, covered by a test.\n\n"
    "## Testing\nRun `pytest tests/unit/test_x.py -q`.\n"
)

_REPORTED_NO_AC_FLOOR_CASES = (
    (
        "task",
        "Generate KNOWN_EVENT_TYPES from the reducer's _EVENT_HANDLERS (mirror F1)",
        "Mirror inventory finding F1 (reports/stability/mirror-inventory.md), HIGH.\n\n"
        "Two masters for one fact — the set of event types the reducer dispatches:\n"
        "- src/rebar/reducer/_replay.py:93 `_EVENT_HANDLERS` (19 keys)\n"
        "- src/rebar/reducer/_version.py:86 `KNOWN_EVENT_TYPES` (19 values)\n\n"
        "Symmetric difference is empty today, so this is preventive. The hazard is severe: "
        "_replay.py:195\n"
        "gates on KNOWN_EVENT_TYPES before handler lookup, and _commands/compact_plan.py:131 "
        "makes known\n"
        "types eligible for SNAPSHOT squash and file retirement. A type in KNOWN_EVENT_TYPES "
        "with no handler\n"
        "is folded into nothing and then deleted — silent permanent data loss.\n\n"
        "The claimed gate does not exist: "
        "tests/interfaces/contracts/test_event_schema_forward_compat.py:16\n"
        "says it pins the parity; its body (:90-97) asserts 3 memberships, and grep "
        '"EVENT_HANDLERS" tests/\n'
        "returns nothing.\n\n"
        "Proposed fix: generate-from-canonical — `KNOWN_EVENT_TYPES = "
        "frozenset(_EVENT_HANDLERS)`.\n"
        "In-tree precedent: _capabilities.py:203.",
    ),
    (
        "task",
        "Validate code-review and completion-verification gate step ids (mirror F13)",
        "Mirror inventory finding F13 (reports/stability/mirror-inventory.md), HIGH.\n\n"
        "- src/rebar/llm/workflow/gates/code-review.yaml:174 (- id: verify), :206 "
        "(- id: decide)\n"
        "- src/rebar/llm/code_review/finalize.py:28-29 _STEP_VERIFY/_STEP_DECIDE, used at "
        ":205,211,217,300\n\n"
        'finalize.py:15,27 states the coupling: "a rename of those there must be mirrored '
        'here."\n\n'
        "The validator already exists — `_validate_gate_step_ids` — with exactly ONE call site:\n"
        'gate_dispatch.py:154, gate_name="plan-review". plan_review_recovery.py:31-36,84-87 '
        "records why it\n"
        'was written: a step-id rename "would otherwise make the recovery lookups silently '
        "return None and\n"
        'degrade a recoverable run to INDETERMINATE." Renaming verify/decide reproduces '
        "exactly that,\n"
        "unguarded. completion-verification.yaml is equally unvalidated.\n\n"
        "Proposed fix: add-parity-check — call the existing helper at the code-review (and "
        "completion-\n"
        "verification) dispatch sites. One line each. This is also the portable "
        "runtime-parity pattern\n"
        "(no CI provider required) that F1, F3 and F11 should follow.",
    ),
    (
        "bug",
        "docs/exit-codes.md has drifted from ROUTES and advertises a gate it lacks (mirror F12)",
        "Mirror inventory finding F12 (reports/stability/mirror-inventory.md), HIGH, "
        "ALREADY DRIFTED.\n\n"
        "- src/rebar/_cli/_registry.py `ROUTES` — 78 live routes\n"
        "- docs/exit-codes.md — 78 hand-maintained rows\n\n"
        "Equal counts, different sets (one addition and one stale row cancelling out):\n"
        "- live route with no doc row: `bridge status`\n"
        "- doc row for a non-route: `review` (docs/exit-codes.md:184) — not retired=True, "
        "absent from ROUTES\n\n"
        'Aggravating factor: docs/exit-codes.md:5-7 claims the file is "the single source of '
        "truth ... pinned\n"
        'by tests/interfaces/lifecycle/test_exit_codes.py, which fails if the codes drift." '
        "The entire\n"
        'assertion touching the file (:236-243) is `assert "\\`11\\`" in text and '
        '"block-but-retryable" in\n'
        "text.lower()`. A reader has positive reason to trust a document nothing checks, and "
        "exit codes are\n"
        "load-bearing for agents driving the CLI.\n\n"
        "Proposed fix: add-census-assertion-to-existing-test — set-difference the table rows "
        "against live\n"
        "ROUTES inside test_exit_codes.py. Note a count comparison would NOT have caught this.",
    ),
)


def _state(*, ttype: str, description: str = _BUG_DESC, title: str = "Some ticket") -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": title,
        "description": description,
        "deps": [],
    }


def _patch_reads(monkeypatch, state: dict) -> None:
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])


def _ctx(state: dict, *, step_id: str) -> StepContext:
    return StepContext(
        run_id="r",
        step_id=step_id,
        kind="scripted",
        step={},
        inputs={"ticket_id": _TARGET},
        workflow={},
        target_ticket=_TARGET,
        repo_root=None,
    )


# ── registry: the bug tier is a restricted, advisory set ─────────────────────────────────────
def test_bug_tier_criteria_are_necessity_and_advisory():
    assert registry.BUG_TIER_CRITERIA == ("necessity",)
    by_id = registry.by_id(None)
    for cid in registry.BUG_TIER_CRITERIA:
        # Structural "never blocks a bug": every bug-tier criterion ships advisory posture.
        assert by_id[cid]["default_posture"] == "advisory", cid


# ── precheck: a bug gets the light tier, not the bare exempt short-circuit ───────────────────
def test_precheck_clean_bug_runs_light_tier_without_blocking(monkeypatch):
    op = STEP_REGISTRY["plan_review_precheck"]
    _patch_reads(monkeypatch, _state(ttype="bug"))
    out = op(_ctx(_state(ttype="bug"), step_id="precheck"))
    # The LLM tier runs (not the exempt short-circuit) ...
    assert out["run_llm"] is True
    assert out["verdict"] is None
    # ... and this ordinarily sized, well-formed bug has no deterministic block.
    assert out["det_blocking"] == []
    assert out["det_coverage"].get("bug_tier") is True


def _blocking_ids(out: dict) -> set[str]:
    return {c for finding in out["det_blocking"] for c in finding.get("criteria", [])}


@pytest.mark.parametrize(
    "cases", [_REPORTED_NO_AC_FLOOR_CASES, tuple(reversed(_REPORTED_NO_AC_FLOOR_CASES))]
)
def test_plan_review_bug_floor_blocks_reported_pre_rewrite_descriptions(monkeypatch, cases):
    """The ticket's three original descriptions all trip P1/P10 and all short-circuit BLOCK.

    The third fixture is the exact pre-rewrite `classical-grey-ayeaye` description from the
    CREATE event; it used to be admitted because the ordinary bug tier downgraded P1/P10.
    """
    op = STEP_REGISTRY["plan_review_precheck"]
    for ttype, title, description in cases:
        ctx = PlanContext(
            ticket_id=_TARGET,
            ticket_type=ttype,
            title=title,
            description=description,
        )
        direct_blocks = det_blocking_findings(run_det_floor(ctx))
        direct_ids = {c for finding in direct_blocks for c in finding.get("criteria", [])}
        assert {"P1", "P10"} <= direct_ids

        state = _state(ttype=ttype, title=title, description=description)
        _patch_reads(monkeypatch, state)
        out = op(_ctx(state, step_id="precheck"))

        assert out["run_llm"] is False
        assert out["verdict"] is not None
        assert out["verdict"]["verdict"] == "BLOCK"
        assert {"P1", "P10"} <= _blocking_ids(out)
        assert out["verdict"]["coverage"]["llm_ran"] is False


@pytest.mark.parametrize("ttype", ["session_log", "code_review", "identity"])
def test_precheck_other_exempt_types_stay_exempt(monkeypatch, ttype):
    op = STEP_REGISTRY["plan_review_precheck"]
    _patch_reads(monkeypatch, _state(ttype=ttype))
    out = op(_ctx(_state(ttype=ttype), step_id="precheck"))
    assert out["run_llm"] is False
    assert out["verdict"]["runner"] == "exempt"
    assert out["verdict"]["verdict"] == "PASS"


# ── assemble: a bug's included LLM criteria are restricted to the bug tier ────────────────────
def test_assemble_bug_restricts_included_to_bug_tier(monkeypatch):
    op = STEP_REGISTRY["plan_review_assemble_criteria"]
    _patch_reads(monkeypatch, _state(ttype="bug"))
    out = op(_ctx(_state(ttype="bug"), step_id="assemble"))
    included = {cid for cid, on in out.items() if cid.startswith("include_") and on}
    assert included == {"include_necessity"}, included
    routed = out["routing"]["single_turn"] + out["routing"]["agent_tier"]
    assert routed == ["necessity"], routed


# ── end-to-end OFFLINE run on a bug → PASS, non-blocking, runner != exempt ────────────────────
class _CannedAgent(AgentStepRunner):
    def __init__(self):
        self.prompts_seen: list[str] = []

    def run(self, ctx) -> StepResult:
        prompt = ctx.step.get("prompt")
        self.prompts_seen.append(prompt)
        if prompt and prompt.startswith("plan-review-verifier"):
            findings = ctx.inputs.get("findings") or []
            verifs = [
                {
                    "index": i,
                    "severity_attributes": {
                        "prod_impact": "low",
                        "debt_impact": "low",
                        "blast_radius": "local",
                        "likelihood": "low",
                        "reversibility": "easy",
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
            return StepResult(outputs={"notes": []}, status="succeeded")
        return StepResult(outputs={"_fake": True}, status="succeeded")


class _Rec(_ex.RunRecorder):
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
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "coverage" in out and "ticket_id" in out:
            return out
    return None


def test_e2e_offline_bug_gets_advisory_review_not_exempt(monkeypatch):
    """The whole point of R4 piece (b): a bug now gets a substantive advisory review
    (runner != 'exempt') instead of the bare exempt-PASS, and has no blocking finding."""
    state = _state(ttype="bug")
    _patch_reads(monkeypatch, state)
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner

    # The finder fires the necessity probe (the only included bug-tier criterion).
    finder = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": "necessity nit", "criteria": ["necessity"]}],
        }
    )
    doc = _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))
    rec = _Rec()
    res = _ex.run_workflow(
        doc,
        {"ticket_id": _TARGET, "probe_criteria": []},
        recorder=rec,
        target_ticket=_TARGET,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=_CannedAgent(),
        batch_runner=ProductionBatchRunner(runner=finder),
    )
    assert res.status == "succeeded", res.error
    verdict = _terminal_verdict(rec)
    assert verdict is not None
    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == []
    # NOT the bare exempt short-circuit — a real (advisory) review ran.
    assert verdict["runner"] != "exempt"
