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
