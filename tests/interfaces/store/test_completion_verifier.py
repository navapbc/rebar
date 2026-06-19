"""Offline coverage for the completion-verification framework + operation (epic c7c5).

All tests run with NO model/network: the contract registry + schema pin are pure, and the
``verify_completion`` operation is exercised through ``FakeRunner(structured=…)`` (the seam
that returns a canned structured payload), so the deterministic op layer — normalize →
resolve_citations → reconcile (verdict normalization + FAIL⇔findings) → validate — is fully
covered without the agents extra doing a real run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import schemas
from rebar.llm import contracts, findings
from rebar.llm.runner import FakeRunner


def _seed(repo: Path, ttype: str = "task", desc: str | None = None) -> str:
    desc = desc or (
        "A task with criteria.\n\n## Acceptance Criteria\n- [ ] the thing exists\n"
    )
    return rebar.create_ticket(ttype, f"verify {ttype}", description=desc, repo_root=str(repo))


def _verify(repo: Path, tid: str, structured: dict) -> dict:
    return rebar.llm.verify_completion(
        tid, graph=False, repo_root=str(repo), runner=FakeRunner(structured=structured)
    )


# ── contract registry ─────────────────────────────────────────────────────────
def test_response_model_for_selects_by_output_schema() -> None:
    findings_model = contracts.response_model_for(None)
    assert "findings" in findings_model.model_fields
    assert "verdict" not in findings_model.model_fields  # the findings default, not a verdict
    # explicit findings name → same default shape
    assert "verdict" not in contracts.response_model_for("review_result").model_fields
    # completion contract → the verdict shape
    cv = contracts.response_model_for("completion_verdict")
    assert set(cv.model_fields) == {"verdict", "findings", "summary"}
    # unknown name → falls back to the findings default (never raises)
    assert "verdict" not in contracts.response_model_for("nonexistent").model_fields


def test_completion_verdict_pins_to_schema() -> None:
    """CompletionVerdict's top-level fields match the schema's properties it owns, and the
    per-finding ``criterion`` is an ADDITIONAL property (rides on finding.additionalProperties),
    NOT a finding-$def property."""
    cv = contracts.completion_verdict_response_model()
    schema = schemas.load(schemas.COMPLETION_VERDICT)
    # the model owns verdict/findings/summary; the schema lists those (+ provenance the op adds)
    assert {"verdict", "findings", "summary"} <= set(schema["properties"])
    assert set(cv.model_fields) == {"verdict", "findings", "summary"}
    # criterion is NOT in the shared finding $def, but a finding carrying it still validates
    finding_props = schemas.load(schemas.COMMON)["$defs"]["finding"]["properties"]
    assert "criterion" not in finding_props
    schemas.validator(schemas.COMPLETION_VERDICT).validate(
        {
            "verdict": "FAIL",
            "findings": [
                {"severity": "high", "dimension": "completion", "detail": "x", "criterion": "AC1"}
            ],
        }
    )


# ── BI-1: optional Nones must not leak as schema-invalid nulls (real model_dump) ─
def test_bi1_finalize_structured_excludes_none() -> None:
    """A clean PASS leaves summary/title unset; finalize_outcome must NOT serialize them as
    `null` (the shape-only schema types them `string`). FakeRunner can't catch this (it never
    calls model_dump), so build the REAL model + run finalize_outcome directly."""
    cv = contracts.completion_verdict_response_model()
    inst = cv(verdict="PASS", findings=[], summary=None)  # summary omitted
    res = findings.finalize_outcome(
        {"structured_response": inst},
        mode="structured",
        output_schema="completion_verdict",
        runner="fake",
    )
    assert "summary" not in res or res["summary"] is not None  # no null leaked
    schemas.validator(schemas.COMPLETION_VERDICT).validate(res)


def test_validate_structured_is_public_and_graceful() -> None:
    # rejects a real shape violation
    with pytest.raises(findings.FindingsError):
        findings.validate_structured({"verdict": "PASS", "findings": [{"detail": 5}]}, "completion_verdict")
    # no-ops on an unknown schema name (graceful degradation, mirrors validate_result)
    assert findings.validate_structured({"anything": 1}, "no_such_schema") == {"anything": 1}


# ── the operation's deterministic reconcile + citation layer (FakeRunner) ───────
def test_op_pass_clean(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    r = _verify(rebar_repo, tid, {"verdict": "PASS", "findings": [], "summary": "all met"})
    assert r["verdict"] == "PASS" and r["findings"] == []
    assert r["reviewers"] == ["completion-verifier"]
    assert r["target"]["ticket_ids"][0] == tid


def test_op_fail_without_findings_is_repaired(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    r = _verify(rebar_repo, tid, {"verdict": "FAIL", "findings": []})
    assert r["verdict"] == "FAIL"
    assert len(r["findings"]) == 1 and r["findings"][0]["criterion"] == "(unspecified)"


def test_op_pass_with_failure_finding_flips_to_fail(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    r = _verify(
        rebar_repo,
        tid,
        {
            "verdict": "PASS",
            "findings": [
                {"criterion": "AC1", "detail": "nope", "severity": "high", "dimension": "completion"}
            ],
        },
    )
    assert r["verdict"] == "FAIL"  # a listed failure must block, regardless of the agent's verdict


def test_op_normalizes_verdict_casing(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _verify(rebar_repo, tid, {"verdict": "pass", "findings": []})["verdict"] == "PASS"
    assert _verify(rebar_repo, tid, {"verdict": "garbage", "findings": []})["verdict"] == "FAIL"


def test_child_closure_trust(rebar_repo: Path) -> None:
    """Epic-level verdict trust: a parent is FAIL unless every DIRECT child is closed WITH a
    certified signature — without recursing or re-verifying child criteria. The LLM verdict on
    the parent's OWN criteria is PASS here (FakeRunner), so the verdict is driven purely by the
    deterministic child-closure check."""
    import subprocess

    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"], cwd=str(rebar_repo),
        check=True, capture_output=True,
    )
    parent = rebar.create_ticket(
        "epic", "parent",
        description="Body.\n\n## Acceptance Criteria\n- [ ] x\n\n## Success Criteria\n- [ ] y\n\n## Context\nc\n",
        repo_root=str(rebar_repo),
    )
    child = rebar.create_ticket(
        "task", "child", parent=parent,
        description="A child.\n\n## Acceptance Criteria\n- [ ] done\n", repo_root=str(rebar_repo),
    )
    PASS = {"verdict": "PASS", "findings": []}

    def verdict():
        return rebar.llm.verify_completion(
            parent, repo_root=str(rebar_repo), runner=FakeRunner(structured=dict(PASS))
        )

    # 1. child OPEN -> parent FAIL (child not closed)
    r = verdict()
    assert r["verdict"] == "FAIL"
    assert any("is closed" in f["criterion"] for f in r["findings"])

    # 2. child CLOSED but UNSIGNED -> parent still FAIL (closure not certified)
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    r = verdict()
    assert r["verdict"] == "FAIL"
    assert any("signed/validated closure" in f["criterion"] for f in r["findings"])

    # 3. child closed AND signed -> child-check clears; parent PASS (own criteria PASS)
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    assert rebar.verify_signature(child, repo_root=str(rebar_repo))["verdict"] == "certified"
    r = verdict()
    assert r["verdict"] == "PASS", r["findings"]


def test_child_closure_gate_short_circuits_before_llm(rebar_repo: Path) -> None:
    """The child-closure gate is DETERMINISTIC and runs BEFORE the LLM (bug a254): when a
    direct child is not closed+signed, verify_completion returns a FAIL verdict WITHOUT ever
    invoking the runner (so the cost is independent of child count, and the evaluator never has
    to reason about child closure). Once children clear, the gate passes through to the LLM."""

    class _BoomRunner:
        """A runner that fails loudly if its model run is ever reached."""

        name = "boom"

        def preflight(self) -> None:
            pass

        def run(self, req):
            raise AssertionError("LLM evaluator was called despite a failing child-closure gate")

    parent = rebar.create_ticket(
        "epic", "parent",
        description=(
            "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n"
            "## Success Criteria\n- [ ] y\n\n## Context\nc\n"
        ),
        repo_root=str(rebar_repo),
    )
    child = rebar.create_ticket(
        "task", "child", parent=parent,
        description="A child.\n\n## Acceptance Criteria\n- [ ] done\n",
        repo_root=str(rebar_repo),
    )

    # child OPEN -> deterministic FAIL; the runner is NEVER reached (no AssertionError raised).
    r = rebar.llm.verify_completion(parent, repo_root=str(rebar_repo), runner=_BoomRunner())
    assert r["verdict"] == "FAIL"
    assert r["runner"] == "deterministic"  # proves no model ran
    assert any("is closed" in f["criterion"] for f in r["findings"])

    # close + sign the child -> the gate clears -> the LLM IS reached (BoomRunner now raises).
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    with pytest.raises(AssertionError, match="LLM evaluator was called"):
        rebar.llm.verify_completion(parent, repo_root=str(rebar_repo), runner=_BoomRunner())


def test_op_downgrades_hallucinated_file_citation(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    r = _verify(
        rebar_repo,
        tid,
        {
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "AC2",
                    "detail": "x",
                    "severity": "high",
                    "dimension": "completion",
                    "citations": [{"kind": "file", "path": "no/such/file.py", "line_start": 5}],
                }
            ],
        },
    )
    cit = r["findings"][0]["citations"][0]
    assert cit["kind"] == "source"  # unresolved file citation downgraded
    assert r["findings"][0]["criterion"] == "AC2"  # criterion preserved through normalize


# ── CONTRACT: the returned verdict is a schema-valid completion_verdict for every type ─
@pytest.mark.parametrize("ttype", ["task", "bug", "story", "epic"])
def test_op_result_validates_against_schema(rebar_repo: Path, ttype: str) -> None:
    """Whatever the op returns (PASS or repaired-FAIL), it MUST validate against the
    canonical ``completion_verdict`` schema for every ticket type — the data contract the
    CLI/MCP/gate consumers rely on. (epic graph default ⇒ children considered; a childless
    epic still PASSes here, isolating the schema-shape guarantee from the child-closure rule.)"""
    desc = (
        "Body long enough for the gates.\n\n## Acceptance Criteria\n- [ ] done\n"
        "\n## Success Criteria\n- [ ] shipped\n\n## Context\nc\n## Reproduction Steps\n- run\n"
    )
    tid = rebar.create_ticket(ttype, f"v {ttype}", description=desc, repo_root=str(rebar_repo))
    # graph=None exercises the auto-default (True for epic, False otherwise).
    r = rebar.llm.verify_completion(
        tid, repo_root=str(rebar_repo),
        runner=FakeRunner(structured={"verdict": "PASS", "findings": [], "summary": "met"}),
    )
    schemas.validator(schemas.COMPLETION_VERDICT).validate(r)
    # provenance the op stamps on regardless of runner
    assert r["target"]["ticket_ids"] == [tid]
    assert r["reviewers"] == ["completion-verifier"]


def test_op_fail_findings_each_have_criterion_and_detail(rebar_repo: Path) -> None:
    """CONTRACT: a FAIL verdict yields a non-empty findings list, and each finding carries
    both a ``criterion`` (the specific requirement) and a ``detail`` — the per-criterion
    shape the gate/CLI render. Asserted on the SHAPE, not on any specific wording."""
    tid = _seed(rebar_repo)
    r = _verify(
        rebar_repo, tid,
        {"verdict": "FAIL", "findings": [
            {"criterion": "AC1", "detail": "not done", "severity": "high", "dimension": "completion"},
            {"criterion": "AC2", "detail": "missing", "severity": "medium", "dimension": "completion"},
        ]},
    )
    assert r["verdict"] == "FAIL"
    assert len(r["findings"]) >= 1
    for f in r["findings"]:
        assert f.get("criterion"), f
        assert f.get("detail"), f


# ── graph auto-default: epic ⇒ children considered; non-epic ⇒ not (BEHAVIORAL) ─
def test_graph_auto_default_depends_on_ticket_type(rebar_repo: Path, monkeypatch) -> None:
    """When ``graph`` is left as the default (None), the op resolves it from the ticket type:
    True for an epic (success criteria span children), False otherwise. Asserted by capturing
    the resolved ``graph`` at the deterministic context-assembly seam — no model needed."""
    from rebar.llm import operations

    seen: dict[str, bool] = {}
    orig = operations._assemble_context

    def spy(ticket_id, *, graph, repo_root):
        seen[ticket_id] = graph
        return orig(ticket_id, graph=graph, repo_root=repo_root)

    monkeypatch.setattr(operations, "_assemble_context", spy)
    PASS = {"verdict": "PASS", "findings": []}
    epic = rebar.create_ticket(
        "epic", "E",
        description="Body.\n\n## Acceptance Criteria\n- [ ] x\n\n## Success Criteria\n- [ ] y\n\n## Context\nc\n",
        repo_root=str(rebar_repo),
    )
    task = _seed(rebar_repo)
    rebar.llm.verify_completion(epic, repo_root=str(rebar_repo), runner=FakeRunner(structured=dict(PASS)))
    rebar.llm.verify_completion(task, repo_root=str(rebar_repo), runner=FakeRunner(structured=dict(PASS)))
    assert seen[epic] is True   # epic ⇒ descendants considered
    assert seen[task] is False  # non-epic ⇒ self only


# ── child-closure-trust: grandchildren NOT recursed, child criteria NOT re-verified ─
def test_child_closure_does_not_recurse_grandchildren(rebar_repo: Path) -> None:
    """The deterministic child-closure check inspects ONLY the parent's DIRECT children: a
    direct child that is closed+certified clears the check even if ITS OWN child (the epic's
    grandchild) is still open — the grandchild is the child's responsibility, not the epic's.
    Proves no recursion AND that a child's own criteria are not re-verified (the child's
    certified signature is the trusted attestation)."""
    import subprocess

    from rebar._commands import transition as _t
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar import config as _config

    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"], cwd=str(rebar_repo),
        check=True, capture_output=True,
    )

    def rid(t: str) -> str:
        return resolve_ticket_id(t, str(_config.tracker_dir(str(rebar_repo))))

    D = "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n## Success Criteria\n- [ ] y\n\n## Context\nc\n"
    epic = rebar.create_ticket("epic", "E", description=D, repo_root=str(rebar_repo))
    child = rebar.create_ticket("story", "C", parent=epic, description=D, repo_root=str(rebar_repo))
    grandchild = rebar.create_ticket(
        "task", "GC", parent=child,
        description="Body.\n\n## Acceptance Criteria\n- [ ] z\n", repo_root=str(rebar_repo),
    )
    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    # Force past the open-grandchild guard so the child closes while the grandchild stays open.
    _t.transition_compute(rid(child), "in_progress", "closed", force=True, repo_root=str(rebar_repo))
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    assert rebar.verify_signature(child, repo_root=str(rebar_repo))["verdict"] == "certified"
    assert rebar.show_ticket(grandchild, repo_root=str(rebar_repo))["status"] == "open"

    r = rebar.llm.verify_completion(
        epic, repo_root=str(rebar_repo),
        runner=FakeRunner(structured={"verdict": "PASS", "findings": []}),
    )
    # Epic PASSes: its only DIRECT child is closed+certified; the open grandchild is not recursed.
    assert r["verdict"] == "PASS", r["findings"]


# ── runner: output_strategy="extract" tool-less structured extraction (offline) ─
def test_extract_structured_uses_with_structured_output(rebar_repo: Path) -> None:
    """The ``output_strategy="extract"`` path's second step (``_extract_structured``) turns the
    agent's free-text conclusion into the structured verdict via ``model.with_structured_output``
    — NO tools, NO live call. Stub the model so this is fully offline; assert it returns the
    typed verdict (contract) and returns None for an empty conclusion (so finalize raises rather
    than inventing a verdict)."""
    from rebar.llm import contracts
    from rebar.llm import runner as _runner

    model_cls = contracts.completion_verdict_response_model()

    class _Structured:
        def __init__(self, cls):
            self._cls = cls

        def invoke(self, messages):
            # Faithfully transcribe a PASS conclusion (no re-judging) into the schema.
            assert any("Extract" in str(m.get("content", "")) for m in messages)
            return self._cls(verdict="PASS", findings=[], summary="all criteria met")

    class StubModel:
        def with_structured_output(self, cls):
            return _Structured(cls)

    out = _runner._extract_structured(StubModel(), model_cls, "Conclusion: every criterion met. PASS.")
    assert out.verdict == "PASS" and out.summary == "all criteria met"
    # Empty conclusion ⇒ None (StructuredOutputError downstream, never an invented verdict).
    assert _runner._extract_structured(StubModel(), model_cls, "   ") is None
