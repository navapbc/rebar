"""B3: the completion-verification gate as an engine workflow.

Proves the `src/rebar/llm/workflow/gates/completion-verification.yaml` walking skeleton:
 * it validates + lints clean (v3, prompt-refs resolve);
 * the child-closure precheck SHORT-CIRCUITS — a parent with an unclosed/uncertified child
   FAILS deterministically and the agentic verify is NEVER called (no billable LLM call), the
   behaviour completion.py:223-225 guarantees;
 * on a passing precheck the agentic verify runs and its raw verdict is reconciled into a
   completion_verdict using the SAME normalize → resolve_citations → reconcile → validate
   pipeline as completion.verify_completion (parity is structural, pinned here);
 * the reconcile invariants hold (FAIL⇔findings; PASS-with-findings flips to FAIL).

Offline only — a canned AgentStepRunner stands in for the LLM (no tokens, no network), and the
rebar reads the precheck performs are monkeypatched, so the whole gate shape is exercised cheaply.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import lint as _lint
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the gate_ops `uses` steps
from rebar.llm.workflow.executor import AgentStepRunner, StepResult

pytestmark = pytest.mark.unit

_WF = pathlib.Path("src/rebar/llm/workflow/gates/completion-verification.yaml")


def _doc() -> dict:
    return _migrate.migrate_to_current(yaml.safe_load(_WF.read_text()))


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


class _CannedRunner(AgentStepRunner):
    """A no-token agent runner returning a completion_verdict-shaped output, counting calls so a
    test can assert the LLM was (or was NOT) reached. It MIRRORS the real structured runner
    (findings.finalize_outcome): the payload (verdict/findings/summary) is emitted with
    exclude_none semantics — an omitted `summary` is ABSENT, not None — while runner/model/
    trace_id are added unconditionally afterwards (so a workflow referencing them never raises)."""

    def __init__(
        self,
        *,
        verdict="PASS",
        findings=None,
        summary=None,
        runner="canned",
        model="fake",
        trace_id=None,
    ):
        payload: dict = {"verdict": verdict, "findings": findings or []}
        if summary is not None:
            payload["summary"] = summary
        self.outputs = {**payload, "runner": runner, "model": model, "trace_id": trace_id}
        self.calls = 0

    def run(self, ctx) -> StepResult:
        self.calls += 1
        return StepResult(outputs=dict(self.outputs), status="succeeded")


def _patch_rebar(monkeypatch, *, ticket_type="story", children=None, child_sig="certified"):
    import rebar

    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda tid, repo_root=None: {"ticket_id": "T-1", "ticket_type": ticket_type},
    )
    monkeypatch.setattr(
        "rebar._reads.list_tickets", lambda parent=None, repo_root=None: list(children or [])
    )
    # Match the REAL call site (completion.child_closure_findings):
    # verify_signature(cid, kind="completion-verifier", repo_root=…). A fake missing `kind`
    # raises TypeError, which the child-closure path swallows into `uncertified` — so the
    # certified-child branch would be untestable (every call would fail via the exception arm).
    monkeypatch.setattr(
        rebar,
        "verify_signature",
        lambda cid, kind=None, repo_root=None: {"verdict": child_sig},
    )


def _run(runner, monkeypatch, **patch):
    _patch_rebar(monkeypatch, **patch)
    rec = _Rec()
    res = _ex.run_workflow(
        _doc(),
        {"ticket_id": "T-1"},
        recorder=rec,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=runner,
    )
    return rec, res


def _terminal_verdict(rec) -> dict | None:
    """The completion_verdict the workflow produced — the reconcile/passthrough arm's output.
    Distinguished from the `verify` step's RAW agent output (which also has verdict+findings)
    by the reconciled `target`/`reviewers` fields the gate ops add."""
    for v in rec.store.values():
        out = v.get("outputs") or {}
        if isinstance(out.get("verdict"), str) and "target" in out and "reviewers" in out:
            return out
    return None


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


def test_happy_path_runs_verify_and_reconciles(monkeypatch):
    # The verifier omits `summary` (the common case: the structured runner drops it via
    # exclude_none) — the run must NOT raise on the missing output, and the reconciled verdict
    # must be schema-valid with no `summary` key (mirroring completion.py).
    runner = _CannedRunner(verdict="PASS", findings=[])  # no summary
    rec, res = _run(runner, monkeypatch, children=[])  # childless → precheck passes
    assert res.status == "succeeded"
    assert runner.calls == 1, "the agentic verify must run when the precheck passes"
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"
    assert "summary" not in verdict, "an absent agent summary must stay absent (no None)"
    assert verdict["target"] == {"kind": "ticket", "ticket_ids": ["T-1"]}
    assert verdict["reviewers"] == ["completion-verifier"]


def test_precheck_short_circuits_without_calling_the_llm(monkeypatch):
    # A parent whose direct child is NOT closed → deterministic FAIL, and the agentic verify is
    # NEVER reached (the central behavioural guarantee: no billable call on a precheck failure).
    runner = _CannedRunner(verdict="PASS")
    unclosed_child = [{"ticket_id": "C-1", "title": "child", "status": "open"}]
    rec, res = _run(runner, monkeypatch, ticket_type="epic", children=unclosed_child)
    assert res.status == "succeeded"
    assert runner.calls == 0, "the LLM must NOT be called when the child-closure precheck fails"
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "FAIL"
    assert verdict["runner"] == "deterministic"  # no model ran
    assert verdict["findings"], "a deterministic FAIL must itemize the failing child"


def test_uncertified_child_does_not_block_but_withholds_certification(monkeypatch):
    # A closed-but-UNCERTIFIED (force-closed) direct child does NOT short-circuit: the LLM still
    # runs on the parent's OWN criteria, and the verdict is marked certifiable=False — the parent
    # may CLOSE but not CERTIFY (certification propagates; an unattested descendant withholds it).
    runner = _CannedRunner(verdict="PASS")
    closed_but_unsigned = [{"ticket_id": "C-2", "title": "child", "status": "closed"}]
    rec, _ = _run(
        runner, monkeypatch, ticket_type="epic", children=closed_but_unsigned, child_sig="absent"
    )
    assert runner.calls == 1, "the LLM runs on the parent's own criteria (uncertified != block)"
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"  # parent's own criteria passed
    assert verdict["certifiable"] is False, "an uncertified descendant withholds certification"


def test_certified_child_is_certifiable(monkeypatch):
    # The REAL certified-child branch: a closed direct child whose completion-verifier signature
    # verifies as `certified` (and computes valid) does NOT block and does NOT withhold — the
    # parent's own criteria still run (LLM once) and the verdict stays certifiable=True. This
    # branch is only reachable because the fake's signature accepts `kind` (a fake missing it
    # would raise TypeError and land in the uncertified-via-exception arm, masking this path).
    runner = _CannedRunner(verdict="PASS")
    certified_child = [{"ticket_id": "C-3", "title": "child", "status": "closed"}]
    rec, _ = _run(
        runner, monkeypatch, ticket_type="epic", children=certified_child, child_sig="certified"
    )
    assert runner.calls == 1, "the LLM runs on the parent's own criteria (a certified child)"
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"
    assert verdict["certifiable"] is True, (
        "a certified direct child must NOT withhold certification — the certified-child branch "
        "(compute_validity → valid), not the uncertified-via-TypeError exception arm"
    )


def test_child_enumeration_read_error_withholds_certification(monkeypatch):
    # Regression (ffb3-730f-bd48-47f1): a TRANSIENT store error enumerating a parent's children
    # must NOT LAUNDER certification. The old `except: return [], []` made `uncertified` empty →
    # gate_ops' `certifiable = not uncertified` → True → the parent closed SIGNED as if it were
    # childless, even though a direct child might be force-closed/uncertified. The correct
    # behaviour (mirroring attest._attested_delivered's fail-closed-on-certification): the parent
    # may still close on its OWN criteria (a read glitch shouldn't block a legitimate close), but
    # the verdict must be certifiable=False (closes UNSIGNED).
    from rebar.llm.completion import child_closure_findings

    def _boom(parent=None, repo_root=None):
        raise RuntimeError("transient store read error")

    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda tid, repo_root=None: {"ticket_id": "T-1", "ticket_type": "epic"},
    )
    monkeypatch.setattr("rebar._reads.list_tickets", _boom)

    # Direct contract (the fixed function): blocking EMPTY (don't fabricate a block on a read
    # error — the close may proceed), uncertified NON-EMPTY (so `certifiable = not uncertified`
    # is False). This is the exact empty/empty return the bug produced, now withheld.
    blocking, uncertified = child_closure_findings("T-1", None)
    assert blocking == [], "a read error must NOT fabricate a blocking child (close may proceed)"
    assert uncertified, "a read error must mark the parent uncertified (withhold, not forge)"

    # End-to-end through the gate: the LLM still runs on the parent's own criteria (not blocked),
    # the verdict passes, but certification is WITHHELD — not the certification-forging PASS+signed
    # the empty/empty return would have produced.
    runner = _CannedRunner(verdict="PASS")
    rec = _Rec()
    res = _ex.run_workflow(
        _doc(),
        {"ticket_id": "T-1"},
        recorder=rec,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=runner,
    )
    assert res.status == "succeeded"
    assert runner.calls == 1, "a read error must NOT block the close (LLM judges own criteria)"
    verdict = _terminal_verdict(rec)
    assert verdict and verdict["verdict"] == "PASS"  # own criteria pass; a read error != block
    assert verdict["certifiable"] is False, (
        "a child-enumeration read error must WITHHOLD certification, not forge it (regression: "
        "the old empty/empty return laundered the signature)"
    )


def test_reconcile_fail_without_findings_synthesizes_one(monkeypatch):
    runner = _CannedRunner(verdict="FAIL", findings=[])
    rec, _ = _run(runner, monkeypatch, children=[])
    verdict = _terminal_verdict(rec)
    assert verdict["verdict"] == "FAIL"
    assert len(verdict["findings"]) == 1  # FAIL⇔findings invariant synthesized a placeholder


def test_reconcile_pass_with_findings_flips_to_fail(monkeypatch):
    finding = {"criterion": "AC1", "severity": "high", "dimension": "completion", "detail": "x"}
    runner = _CannedRunner(verdict="PASS", findings=[finding])
    rec, _ = _run(runner, monkeypatch, children=[])
    verdict = _terminal_verdict(rec)
    assert verdict["verdict"] == "FAIL", "a listed failure finding must block (PASS→FAIL)"


def test_unsupported_file_citation_is_downgraded(monkeypatch):
    # AC4(c): a finding citing a nonexistent file must be downgraded (kind file → source, the
    # path/line fields dropped) by the reconcile op's resolve_citations pass — the same
    # hallucinated-citation guardrail completion.py applies, exercised through the workflow.
    finding = {
        "criterion": "AC1",
        "severity": "high",
        "dimension": "completion",
        "detail": "claims a file that does not exist",
        "citations": [{"kind": "file", "path": "no/such/hallucinated_file.py", "line_start": 3}],
    }
    runner = _CannedRunner(verdict="FAIL", findings=[finding])
    rec, _ = _run(runner, monkeypatch, children=[])
    verdict = _terminal_verdict(rec)
    cits = verdict["findings"][0]["citations"]
    assert cits and cits[0]["kind"] == "source", "an unresolved file citation must be downgraded"
    assert "path" not in cits[0], "the hallucinated path must be dropped on downgrade"


def test_reconcile_matches_completion_py_tail(monkeypatch):
    # Parity: the workflow's reconcile produces the SAME completion_verdict as completion.py's
    # own normalize → resolve_citations → reconcile → validate tail on the same raw agent output.
    from rebar.llm import findings as _findings
    from rebar.llm.completion import _REVIEWER_ID, reconcile_verdict
    from rebar.llm.config import LLMConfig

    raw_findings = [
        {"criterion": "AC1", "severity": "high", "dimension": "completion", "detail": "d"}
    ]
    # No summary (the faithful common case) → the workflow and completion.py produce IDENTICAL
    # verdicts. (Carrying an agent-supplied summary is the documented B4 parity gap.)
    runner = _CannedRunner(verdict="FAIL", findings=raw_findings, runner="r", model="m")
    rec, _ = _run(runner, monkeypatch, children=[])
    got = _terminal_verdict(rec)

    cfg = LLMConfig.from_env(repo_root=None)
    expected = {
        "verdict": "FAIL",
        "findings": [
            _findings.normalize_finding(f, reviewer_id=_REVIEWER_ID) for f in raw_findings
        ],
        "target": {"kind": "ticket", "ticket_ids": ["T-1"]},
        "reviewers": [_REVIEWER_ID],
        "runner": "r",
        "model": "m",
        "trace_id": None,
    }
    _findings.resolve_citations(expected, cfg.repo_path)
    reconcile_verdict(expected)
    # The workflow reconcile also carries the precheck's certification decision onto the verdict
    # (default true — this run is childless, so certifiable). completion.py's shared tail helper
    # (reconcile_verdict) does the FAIL<->findings normalization; certifiable is added by the
    # reconcile op.
    expected["certifiable"] = True
    # The reconcile op also carries the POSITIVE per-criterion `criteria[]` through the workflow
    # (story e7e0); on the no-summary common case here the agent emitted none, so it is [].
    expected["criteria"] = []
    expected = _findings.validate_structured(expected, "completion_verdict")
    assert got == expected, "workflow reconcile diverged from completion.py's deterministic tail"
