"""Walking skeleton (5e78): the contract-bearing step model wired end-to-end through
ONE scripted op (`fetch_ticket`) — the schema registry resolving an INPUT contract by
name, the step DECLARING input+output+description via `@register_step`, the editor
inspector SURFACING that contract read-only, and the linter CONSUMING the output
contract for name-existence of a `${{ steps.<id>.outputs.<name> }}` reference.

Pure: no store/network (the linter, the registry, and the inspector data layer are all
offline). Asserts the four layers agree on one op so a design flaw surfaces early."""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.llm.workflow import lint as L
from rebar.llm.workflow import steps  # noqa: F401 - registers the built-in contracts
from rebar.llm.workflow.editor import resolve_contracts, step_contract_view
from rebar.llm.workflow.executor import contract_for
from rebar.llm.workflow.lint_refs import ENGINE_INJECTED_INPUTS

# fetch_ticket's declared output fields (the OUTPUT contract's properties).
FETCH_OUTPUTS = {"ticket", "ticket_id", "title", "description", "status", "ticket_type", "tags"}


def _msgs(findings):
    return "\n".join(str(f) for f in findings)


# A workflow that wires a `with:` ref to a GOOD fetch_ticket output (`description`).
_GOOD = """\
schema_version: "1"
name: skeleton
inputs:
  ticket_id:
    type: string
    required: true
steps:
  - id: fetch
    uses: fetch_ticket
    with:
      ticket_id: ${{ inputs.ticket_id }}
  - id: use
    uses: render_context
    needs: [fetch]
    with:
      context: ${{ steps.fetch.outputs.description }}
"""

# Same, but referencing a field fetch_ticket does NOT produce (`diff`).
_BAD = _GOOD.replace("outputs.description", "outputs.diff")


# ── AC #1 + #8: the contract is declared and resolves from the registry ─────────


def test_fetch_ticket_declares_contract() -> None:
    contract = contract_for("fetch_ticket")
    assert contract is not None
    assert contract.input_schema == "fetch_ticket_input"
    assert contract.output_schema == "fetch_ticket_output"
    assert contract.description  # a non-empty description


def test_contract_schema_names_resolve_from_the_registry() -> None:
    # AC #8: the input schema lives at a defined registry path and resolves BY NAME.
    pytest.importorskip("jsonschema")
    schemas.validator("fetch_ticket_input")  # builds without error → name resolves
    schemas.validator("fetch_ticket_output")
    in_schema = schemas.load("fetch_ticket_input")
    assert in_schema["$id"].endswith("/fetch_ticket_input.schema.json")
    assert set(schemas.load("fetch_ticket_output")["properties"]) == FETCH_OUTPUTS


# ── AC #2 + #7: the editor inspector surfaces consumes/produces/description ──────


def test_inspector_view_shows_consumes_produces_description() -> None:
    view = step_contract_view("fetch_ticket")
    assert view["has_contract"] is True
    assert view["description"]
    assert {f["name"] for f in view["consumes"]} == {"ticket_id"}
    assert {f["name"] for f in view["produces"]} == FETCH_OUTPUTS
    # field metadata is carried through for the inspector to render
    produced = {f["name"]: f for f in view["produces"]}
    assert produced["ticket"]["required"] is True
    assert produced["title"]["required"] is False


def test_inspector_empty_state_for_unknown_or_none() -> None:
    # AC #7: a node with no declared contract (or nothing selected → None) yields a
    # defined empty state, never a crash.
    for arg in (None, "", "not_a_registered_op"):
        view = step_contract_view(arg)
        assert view["has_contract"] is False
        assert view["consumes"] == [] and view["produces"] == []
        assert view["description"] == ""


def test_resolve_contracts_keys_by_op_name() -> None:
    from rebar.llm.workflow.schema import parse_workflow

    doc = parse_workflow(_GOOD)
    contracts = resolve_contracts(doc)
    assert "fetch_ticket" in contracts
    assert contracts["fetch_ticket"]["has_contract"] is True
    # render_context has no declared contract in this slice → empty state, not absent.
    assert contracts["render_context"]["has_contract"] is False


# ── AC #3 + #5: the linter consumes the output contract ─────────────────────────


def test_lint_passes_a_good_output_reference() -> None:
    findings = [f for f in L.lint_workflow(_GOOD) if f.severity != "warning"]
    assert findings == [], _msgs(findings)


def test_lint_flags_a_bad_output_field_reference() -> None:
    findings = L.lint_workflow(_BAD)
    assert any("output 'diff' not produced by step 'fetch'" in f.message for f in findings), _msgs(
        findings
    )


def test_lint_skips_unknown_producer_never_a_false_error() -> None:
    # AC #5: an upstream step with NO declared output contract is UNKNOWN — a ref to
    # any of its fields must NOT be flagged (only fetch_ticket is annotated here).
    wf = """\
schema_version: "1"
name: unknown_producer
steps:
  - id: a
    uses: render_context
  - id: b
    uses: render_context
    needs: [a]
    with:
      v: ${{ steps.a.outputs.anything_at_all }}
"""
    findings = [f for f in L.lint_workflow(wf) if f.severity != "warning"]
    assert not any("not produced by" in f.message for f in findings), _msgs(findings)


# ── AC #6: the engine-injected `${{ inputs.* }}` namespace is not flagged ────────


def test_engine_injected_inputs_are_not_flagged() -> None:
    # ticket_id/ticket_context/repo_path are valid inputs even when not declared.
    assert ENGINE_INJECTED_INPUTS == {"ticket_id", "ticket_context", "repo_path"}
    wf = """\
schema_version: "1"
name: injected
steps:
  - id: a
    uses: render_context
    with:
      tid: ${{ inputs.ticket_id }}
      ctx: ${{ inputs.ticket_context }}
      root: ${{ inputs.repo_path }}
"""
    findings = [f for f in L.lint_workflow(wf) if f.severity != "warning"]
    assert not any("undeclared workflow input" in f.message for f in findings), _msgs(findings)


def test_undeclared_non_injected_input_is_still_flagged() -> None:
    wf = """\
schema_version: "1"
name: bad_input
steps:
  - id: a
    uses: render_context
    with:
      v: ${{ inputs.not_injected_and_not_declared }}
"""
    findings = L.lint_workflow(wf)
    assert any("undeclared workflow input" in f.message for f in findings), _msgs(findings)
