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
    desc = desc or ("A task with criteria.\n\n## Acceptance Criteria\n- [ ] the thing exists\n")
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
        findings.validate_structured(
            {"verdict": "PASS", "findings": [{"detail": 5}]}, "completion_verdict"
        )
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
                {
                    "criterion": "AC1",
                    "detail": "nope",
                    "severity": "high",
                    "dimension": "completion",
                }
            ],
        },
    )
    assert r["verdict"] == "FAIL"  # a listed failure must block, regardless of the agent's verdict


def test_op_normalizes_verdict_casing(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    assert _verify(rebar_repo, tid, {"verdict": "pass", "findings": []})["verdict"] == "PASS"
    assert _verify(rebar_repo, tid, {"verdict": "garbage", "findings": []})["verdict"] == "FAIL"


def test_fail_verdict_carries_remediation_guidance(rebar_repo: Path) -> None:
    """Every FAIL verdict carries remediation guidance that points at the ticket-comments
    evidence channel — generic, and without steering the caller toward bypassing the gate."""
    from rebar.llm.completion import COMPLETION_REMEDIATION_GUIDANCE

    r = _verify(
        rebar_repo,
        _seed(rebar_repo),
        {
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "AC1",
                    "detail": "not met",
                    "severity": "high",
                    "dimension": "completion",
                }
            ],
        },
    )
    guidance = r.get("remediation")
    assert guidance, "a FAIL verdict must carry remediation guidance"
    assert guidance == COMPLETION_REMEDIATION_GUIDANCE
    lowered = guidance.lower()
    # Names the intended channel: documenting evidence as a comment on the ticket.
    assert "comment" in lowered
    assert "evidence" in lowered
    # Steers to the evidence channel only — does not advertise a way to bypass the gate.
    assert "force" not in lowered
    # Generic, not over-fitted to any one incident.
    for token in ("epic", "dependabot", "contributing", "child ticket", "governance"):
        assert token not in lowered


def test_pass_verdict_has_no_remediation(rebar_repo: Path) -> None:
    """A PASS has nothing to remediate, so it never carries the guidance field."""
    r = _verify(rebar_repo, _seed(rebar_repo), {"verdict": "PASS", "findings": []})
    assert r["verdict"] == "PASS"
    assert "remediation" not in r


def test_deterministic_child_failure_carries_remediation() -> None:
    """The deterministic child-closure FAIL (no LLM) also carries the guidance — the single
    reconcile_verdict chokepoint means the same coaching rides both failure paths."""
    from types import SimpleNamespace

    from rebar.llm.completion import COMPLETION_REMEDIATION_GUIDANCE, deterministic_child_failure

    child_findings = [
        {
            "criterion": "direct child X is closed",
            "detail": "child X is 'open', not closed.",
            "severity": "high",
            "dimension": "completion",
        }
    ]
    verdict = deterministic_child_failure(
        "parent-id", child_findings, SimpleNamespace(repo_path=None)
    )
    assert verdict["verdict"] == "FAIL"
    assert verdict.get("remediation") == COMPLETION_REMEDIATION_GUIDANCE


def test_child_closure_trust(rebar_repo: Path) -> None:
    """Epic-level verdict trust: a parent is FAIL unless every DIRECT child is closed WITH a
    certified signature — without recursing or re-verifying child criteria. The LLM verdict on
    the parent's OWN criteria is PASS here (FakeRunner), so the verdict is driven purely by the
    deterministic child-closure check."""
    import subprocess

    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(rebar_repo),
        check=True,
        capture_output=True,
    )
    parent = rebar.create_ticket(
        "epic",
        "parent",
        description=(
            "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n"
            "## Success Criteria\n- [ ] y\n\n## Context\nc\n"
        ),
        repo_root=str(rebar_repo),
    )
    child = rebar.create_ticket(
        "task",
        "child",
        parent=parent,
        description="A child.\n\n## Acceptance Criteria\n- [ ] done\n",
        repo_root=str(rebar_repo),
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

    # 2. child CLOSED but UNSIGNED (force-closed) -> parent may CLOSE (own criteria PASS via the
    #    FakeRunner) but is NOT certifiable: an uncertified descendant withholds the parent's
    #    signature (certification propagates). Closure is not blocked; certification is.
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    r = verdict()
    assert r["verdict"] == "PASS"  # the parent's own criteria pass; uncertified child != block
    assert r.get("certifiable") is False, "an uncertified descendant withholds certification"

    # 3. child closed AND signed -> child-check clears; parent PASS AND certifiable (can certify)
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    assert rebar.verify_signature(child, repo_root=str(rebar_repo))["verdict"] == "certified"
    r = verdict()
    assert r["verdict"] == "PASS", r["findings"]
    assert r.get("certifiable") is not False, "all children certified -> parent is certifiable"


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

    class _CountingRunner(FakeRunner):
        """A canned runner that records whether the model run was reached (call count)."""

        name = "counting"

        def __init__(self) -> None:
            super().__init__(structured={"verdict": "PASS", "findings": []})
            self.calls = 0

        def run(self, req):  # type: ignore[override]
            self.calls += 1
            return super().run(req)

    parent = rebar.create_ticket(
        "epic",
        "parent",
        description=(
            "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n"
            "## Success Criteria\n- [ ] y\n\n## Context\nc\n"
        ),
        repo_root=str(rebar_repo),
    )
    child = rebar.create_ticket(
        "task",
        "child",
        parent=parent,
        description="A child.\n\n## Acceptance Criteria\n- [ ] done\n",
        repo_root=str(rebar_repo),
    )

    # child OPEN -> deterministic FAIL; the runner is NEVER reached (no AssertionError raised).
    # (_BoomRunner.run raising is the path-independent proof the model was not called: the
    # precheck short-circuits before the agentic verify step.)
    r = rebar.llm.verify_completion(parent, repo_root=str(rebar_repo), runner=_BoomRunner())
    assert r["verdict"] == "FAIL"
    assert r["runner"] == "deterministic"  # proves no model ran
    assert any("is closed" in f["criterion"] for f in r["findings"])

    # close + sign the child -> the gate clears -> the LLM IS reached. (Asserted via a counting
    # runner rather than a raise: on the workflow gate a runner-raised exception is wrapped by
    # the interpreter into a failed run, so we observe the call count, not the exception type.)
    rebar.transition(child, "open", "closed", repo_root=str(rebar_repo))
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    counting = _CountingRunner()
    r2 = rebar.llm.verify_completion(parent, repo_root=str(rebar_repo), runner=counting)
    assert counting.calls == 1, "the LLM verify step must be reached once the child gate clears"
    assert r2["runner"] != "deterministic"  # a real model verdict, not the short-circuit


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
        tid,
        repo_root=str(rebar_repo),
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
        rebar_repo,
        tid,
        {
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "AC1",
                    "detail": "not done",
                    "severity": "high",
                    "dimension": "completion",
                },
                {
                    "criterion": "AC2",
                    "detail": "missing",
                    "severity": "medium",
                    "dimension": "completion",
                },
            ],
        },
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
    orig = operations.assemble_context

    def spy(ticket_id, *, graph, repo_root):
        seen[ticket_id] = graph
        return orig(ticket_id, graph=graph, repo_root=repo_root)

    monkeypatch.setattr(operations, "assemble_context", spy)
    PASS = {"verdict": "PASS", "findings": []}
    epic = rebar.create_ticket(
        "epic",
        "E",
        description=(
            "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n"
            "## Success Criteria\n- [ ] y\n\n## Context\nc\n"
        ),
        repo_root=str(rebar_repo),
    )
    task = _seed(rebar_repo)
    rebar.llm.verify_completion(
        epic, repo_root=str(rebar_repo), runner=FakeRunner(structured=dict(PASS))
    )
    rebar.llm.verify_completion(
        task, repo_root=str(rebar_repo), runner=FakeRunner(structured=dict(PASS))
    )
    assert seen[epic] is True  # epic ⇒ descendants considered
    assert seen[task] is False  # non-epic ⇒ self only


# ── child-closure-trust: grandchildren NOT recursed, child criteria NOT re-verified ─
def test_child_closure_does_not_recurse_grandchildren(rebar_repo: Path) -> None:
    """The deterministic child-closure check inspects ONLY the parent's DIRECT children: a
    direct child that is closed+certified clears the check even if ITS OWN child (the epic's
    grandchild) is still open — the grandchild is the child's responsibility, not the epic's.
    Proves no recursion AND that a child's own criteria are not re-verified (the child's
    certified signature is the trusted attestation)."""
    import subprocess

    from rebar import config as _config
    from rebar._commands import transition as _t
    from rebar._engine_support.resolver import resolve_ticket_id

    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(rebar_repo),
        check=True,
        capture_output=True,
    )

    def rid(t: str) -> str:
        return resolve_ticket_id(t, str(_config.tracker_dir(str(rebar_repo))))

    D = (
        "Body.\n\n## Acceptance Criteria\n- [ ] x\n\n"
        "## Success Criteria\n- [ ] y\n\n## Context\nc\n"
    )
    epic = rebar.create_ticket("epic", "E", description=D, repo_root=str(rebar_repo))
    child = rebar.create_ticket("story", "C", parent=epic, description=D, repo_root=str(rebar_repo))
    grandchild = rebar.create_ticket(
        "task",
        "GC",
        parent=child,
        description="Body.\n\n## Acceptance Criteria\n- [ ] z\n",
        repo_root=str(rebar_repo),
    )
    # Reach the "closed+certified direct child with an OPEN grandchild" state legitimately:
    # the open-children guard cannot be bypassed (not even with --force, warty-karma-matte) and
    # a closed parent can't gain new children — so close the grandchild, then the child (each
    # passes the guard with NO force), then REOPEN the grandchild. (A grandchild reopened after
    # its ancestors closed is exactly the case the verifier must not recurse into.)
    # Starting work on the grandchild cascades its ancestors (child, epic) to in_progress too
    # (the parent-first cascade), so the child is already in_progress when we close it.
    rebar.transition(grandchild, "open", "in_progress", repo_root=str(rebar_repo))
    _t.transition_compute(rid(grandchild), "in_progress", "closed", repo_root=str(rebar_repo))
    _t.transition_compute(rid(child), "in_progress", "closed", repo_root=str(rebar_repo))
    rebar.sign_manifest(child, ["completion-verifier: PASS"], repo_root=str(rebar_repo))
    assert rebar.verify_signature(child, repo_root=str(rebar_repo))["verdict"] == "certified"
    _t.transition_compute(rid(grandchild), "closed", "open", repo_root=str(rebar_repo))  # reopen
    assert rebar.show_ticket(grandchild, repo_root=str(rebar_repo))["status"] == "open"

    r = rebar.llm.verify_completion(
        epic,
        repo_root=str(rebar_repo),
        runner=FakeRunner(structured={"verdict": "PASS", "findings": []}),
    )
    # Epic PASSes: its only DIRECT child is closed+certified; the open grandchild is not recursed.
    assert r["verdict"] == "PASS", r["findings"]


# ── runner: free-text → validated structured verdict (offline) ──────────────────
def test_free_text_conclusion_parses_into_structured_verdict() -> None:
    """The structured-output stack turns a model's free-text JSON conclusion into a
    validated verdict via the deterministic tolerant parse (no second LLM, no live
    call), and rejects an empty conclusion (so finalize raises rather than inventing a
    verdict). This is the successor to the removed extract-step contract."""
    from rebar.llm import contracts, structured
    from rebar.llm.errors import StructuredOutputError

    model_cls = contracts.completion_verdict_response_model()

    out = structured.parse_structured(
        '{"verdict": "PASS", "findings": [], "summary": "all criteria met"}', model_cls
    )
    assert out.verdict == "PASS" and out.summary == "all criteria met"
    # An empty / non-JSON conclusion is a hard error, never an invented verdict.
    with pytest.raises(StructuredOutputError):
        structured.parse_structured("   ", model_cls)
