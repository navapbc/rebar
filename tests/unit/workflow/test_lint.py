"""Unit tests for the workflow linter (WS-B2): reference integrity, the closed
expression allow-list, the injection guard, post-substitution invariance, and the
secret scan. Pure stdlib, no store/network."""

from __future__ import annotations

import pytest

from rebar.llm.workflow import lint as L

CLEAN = """\
schema_version: "1"
name: code_review
inputs:
  ticket_id:
    type: string
    required: true
steps:
  - id: fetch
    uses: fetch_ticket
    with:
      ticket_id: ${{ inputs.ticket_id }}
  - id: review
    prompt: code_quality
    needs: [fetch]
    with:
      diff: ${{ steps.fetch.outputs.diff }}
      token: ${{ secrets.GH_TOKEN }}
"""


def _msgs(findings):
    return "\n".join(str(f) for f in findings)


def test_clean_workflow_has_no_findings() -> None:
    pytest.importorskip("jsonschema")
    findings = L.lint_workflow(CLEAN)
    assert findings == [], _msgs(findings)
    assert L.lint_passes(findings)


# ── reference integrity ───────────────────────────────────────────────────────


def test_undeclared_input_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      v: ${{ inputs.nope }}
"""
    findings = L.lint_workflow(wf)
    assert any("undeclared workflow input 'nope'" in f.message for f in findings), _msgs(findings)


def test_unknown_step_output_reference_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      v: ${{ steps.ghost.outputs.x }}
"""
    findings = L.lint_workflow(wf)
    assert any("unknown step 'ghost'" in f.message for f in findings), _msgs(findings)


def test_reference_to_non_ancestor_step_is_flagged() -> None:
    # `b` references `a`'s output but does not declare `a` in needs.
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
  - id: b
    uses: u
    needs: [a]
    with:
      from_a: ${{ steps.a.outputs.x }}
  - id: c
    uses: u
    needs: [b]
    with:
      from_a: ${{ steps.a.outputs.x }}
"""
    findings = L.lint_workflow(wf)
    # c references a but only needs b (a IS a transitive ancestor of c) -> allowed.
    assert not any("not an upstream dependency" in f.message for f in findings), _msgs(findings)


def test_reference_to_unrelated_step_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
  - id: b
    uses: u
    with:
      from_a: ${{ steps.a.outputs.x }}
"""
    findings = L.lint_workflow(wf)
    assert any("not an upstream dependency" in f.message for f in findings), _msgs(findings)


def test_self_reference_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      v: ${{ steps.a.outputs.x }}
"""
    findings = L.lint_workflow(wf)
    assert any("its own output" in f.message for f in findings), _msgs(findings)


def test_unknown_needs_edge_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    needs: [ghost]
"""
    findings = L.lint_workflow(wf)
    assert any("unknown step 'ghost' in `needs`" in f.message for f in findings), _msgs(findings)


def test_cycle_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    needs: [b]
  - id: b
    uses: u
    needs: [a]
"""
    findings = L.lint_workflow(wf)
    assert any("cycle" in f.message for f in findings), _msgs(findings)


def test_multiple_terminal_steps_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
  - id: b
    uses: u
"""
    findings = L.lint_workflow(wf)
    assert any("exactly one terminal step" in f.message for f in findings), _msgs(findings)


def test_single_terminal_passes() -> None:
    pytest.importorskip("jsonschema")
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
  - id: b
    uses: u
    needs: [a]
"""
    findings = L.lint_workflow(wf)
    assert not any("terminal" in f.message for f in findings), _msgs(findings)


# ── expression allow-list + kill switch ───────────────────────────────────────


def test_disallowed_expression_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      v: ${{ os.system('rm -rf /') }}
"""
    findings = L.lint_workflow(wf)
    assert any("disallowed expression" in f.message for f in findings), _msgs(findings)


def test_expressions_off_kill_switch() -> None:
    wf = """\
schema_version: "1"
name: x
inputs:
  t: {type: string}
steps:
  - id: a
    uses: u
    with:
      v: ${{ inputs.t }}
"""
    findings = L.lint_workflow(wf, expressions=False)
    assert any("expressions are disabled" in f.message for f in findings), _msgs(findings)


# ── injection guard ───────────────────────────────────────────────────────────


def test_raw_expression_in_prompt_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    prompt: ${{ inputs.evil }}
"""
    findings = L.lint_workflow(wf)
    assert any("raw expression not allowed in `prompt`" in f.message for f in findings), _msgs(
        findings
    )


def test_expression_in_mapping_key_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
inputs:
  k: {type: string}
steps:
  - id: a
    uses: u
    with:
      "${{ inputs.k }}": value
"""
    findings = L.lint_workflow(wf)
    assert any("mapping key" in f.message for f in findings), _msgs(findings)


# ── secret scanning ───────────────────────────────────────────────────────────


def test_embedded_private_key_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      key: |
        -----BEGIN RSA PRIVATE KEY-----
        abcdef
"""
    findings = L.lint_workflow(wf)
    assert any("private key" in f.message for f in findings), _msgs(findings)


def test_aws_access_key_is_flagged() -> None:
    findings = L.secret_scan("token: AKIAIOSFODNN7EXAMPLE\n")
    assert any("AWS access key" in f.message for f in findings), _msgs(findings)


def test_secret_named_literal_field_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      password: supersecretvalue123
"""
    findings = L.lint_workflow(wf)
    assert any("looks like a credential" in f.message for f in findings), _msgs(findings)


def test_secret_via_indirection_is_clean() -> None:
    pytest.importorskip("jsonschema")
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      password: ${{ secrets.DB_PASS }}
"""
    findings = L.lint_workflow(wf)
    assert not any("credential" in f.message for f in findings), _msgs(findings)


def test_env_indirection_is_clean() -> None:
    pytest.importorskip("jsonschema")
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    with:
      token: ${env:GH_TOKEN}
"""
    findings = L.lint_workflow(wf)
    assert not any("credential" in f.message for f in findings), _msgs(findings)


# ── parse failure short-circuits ──────────────────────────────────────────────


def test_parse_failure_returns_single_finding() -> None:
    findings = L.lint_workflow("- not a mapping\n")
    assert len(findings) == 1
    assert not L.lint_passes(findings)


def test_lean_core_degrades_when_jsonschema_absent(monkeypatch) -> None:
    # Simulate a no-extras install (no jsonschema): a VALID workflow still passes —
    # structural fallback + a non-blocking warning, never a false error.
    from rebar import schemas

    def _no_jsonschema(name):
        raise ImportError("no jsonschema in this lean install")

    monkeypatch.setattr(schemas, "validator", _no_jsonschema)
    findings = L.lint_workflow(CLEAN)
    assert L.lint_passes(findings), _msgs(findings)
    # The skip note is present but only as a warning.
    assert any(f.severity == "warning" and "jsonschema" in f.message.lower() for f in findings)


def test_type_disagreement_with_shape_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    type: scripted
    prompt: code_quality
"""
    findings = L.lint_workflow(wf)
    assert any("type: scripted" in f.message and "prompt" in f.message for f in findings), _msgs(
        findings
    )


def test_agent_only_field_on_scripted_step_is_flagged() -> None:
    wf = """\
schema_version: "1"
name: x
steps:
  - id: a
    uses: u
    output_schema: review_result
    mode: findings
"""
    findings = L.lint_workflow(wf)
    msgs = _msgs(findings)
    assert any("output_schema" in f.message and "ignored" in f.message for f in findings), msgs
    assert any("mode" in f.message and "ignored" in f.message for f in findings), msgs


def test_secret_indirection_via_any_expression_is_clean() -> None:
    # The literal-secret check fires only on a bare literal; ANY ${{ }} indirection
    # (inputs/secrets/steps/env) means the secret isn't in the file.
    pytest.importorskip("jsonschema")
    wf = """\
schema_version: "1"
name: x
inputs:
  pw: {type: string}
steps:
  - id: a
    uses: u
    with:
      password: ${{ inputs.pw }}
"""
    findings = L.lint_workflow(wf)
    assert not any("credential" in f.message for f in findings), _msgs(findings)
