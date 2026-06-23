"""Validation depth (story c768): the 3-state SHALLOW static structural check, the
RUNTIME validation of a step's resolved inputs against the CONSUMER's input contract
(the real safety net), and the DISTINCT validator-failure signal (fail-loud, never
false-pass). Plus the editor's "unchecked" badge flag for an opaque source.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm.workflow.lint_refs import shallow_contract_check

pytest.importorskip("jsonschema")


@pytest.fixture
def rebar_repo(tmp_path, monkeypatch):
    """A self-contained initialized rebar repo (this unit dir has no shared fixture)."""
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


# ── 1. The 3-state SHALLOW structural check (port of spike E2) ─────────────────


def _obj(props, required=None):
    s = {"type": "object", "properties": props}
    if required is not None:
        s["required"] = required
    return s


def test_shallow_error_on_missing_required() -> None:
    source = _obj({"a": {"type": "string"}})
    target = _obj({"a": {"type": "string"}, "b": {"type": "string"}}, required=["b"])
    assert shallow_contract_check(source, target) == "ERROR"


def test_shallow_error_on_kind_mismatch() -> None:
    source = _obj({"x": {"type": "string"}})
    target = _obj({"x": {"type": "integer"}}, required=["x"])
    assert shallow_contract_check(source, target) == "ERROR"


def test_shallow_unknown_on_top_level_oneof() -> None:
    source = {"oneOf": [_obj({"a": {"type": "string"}})]}
    target = _obj({"a": {"type": "string"}}, required=["a"])
    assert shallow_contract_check(source, target) == "UNKNOWN"
    # The same abstention when the TARGET is the combinator-typed one.
    assert shallow_contract_check(target, {"anyOf": [target]}) == "UNKNOWN"


def test_shallow_unknown_on_ref_field() -> None:
    # A field that is a $ref (or a combinator) is UNKNOWN for that field — never an
    # ERROR even if the other side declares an incompatible primitive kind.
    source = _obj({"x": {"$ref": "common.schema.json#/$defs/comment"}})
    target = _obj({"x": {"type": "integer"}}, required=["x"])
    assert shallow_contract_check(source, target) == "OK"


def test_shallow_unknown_on_bare_ref_top_level() -> None:
    assert shallow_contract_check({"$ref": "x"}, _obj({"a": {"type": "string"}})) == "UNKNOWN"


def test_shallow_unknown_when_no_properties() -> None:
    # Not a plain object-with-properties schema → abstain.
    assert shallow_contract_check({"type": "string"}, _obj({})) == "UNKNOWN"


def test_shallow_ok_on_matching_shape() -> None:
    source = _obj({"x": {"type": "string"}, "y": {"type": "integer"}, "extra": {"type": "boolean"}})
    target = _obj({"x": {"type": "string"}, "y": {"type": "integer"}}, required=["x", "y"])
    assert shallow_contract_check(source, target) == "OK"


def test_shallow_ok_on_intersecting_type_lists() -> None:
    source = _obj({"x": {"type": ["string", "null"]}})
    target = _obj({"x": {"type": ["string", "integer"]}}, required=["x"])
    assert shallow_contract_check(source, target) == "OK"


def test_shallow_ok_when_field_missing_type() -> None:
    # A property present in both but with no declared type is UNKNOWN for that field,
    # not an ERROR — so the overall verdict is OK.
    source = _obj({"x": {"description": "no type"}})
    target = _obj({"x": {"type": "integer"}}, required=["x"])
    assert shallow_contract_check(source, target) == "OK"


# ── 2. RUNTIME validation against the CONSUMER's input contract ────────────────


def _run(doc, inputs, tid, repo):
    import rebar

    return rebar.run_workflow(doc, inputs, ticket_id=tid, repo_root=str(repo))


def test_runtime_input_violation_fails_step(rebar_repo) -> None:
    # The `tag` step's input contract (tag_input) requires `tag: string`. A resolved
    # `with` that supplies an integer VIOLATES the contract — the step must FAIL loud.
    import rebar

    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    doc = {
        "schema_version": "2",
        "name": "bad_input",
        "inputs": {"ticket_id": {"type": "string"}},
        "steps": [
            {
                "id": "t",
                "uses": "tag",
                "with": {"tag": 123, "ticket_id": "${{ inputs.ticket_id }}"},
            },
        ],
    }
    res = _run(doc, {"ticket_id": tid}, tid, rebar_repo)
    assert res["status"] == "failed"
    assert "input contract violation (tag_input)" in res["error"]
    # The store must NOT have been mutated — the step failed before its effect.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "123" not in state["tags"]


def test_runtime_missing_required_input_fails_step(rebar_repo) -> None:
    import rebar

    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    doc = {
        "schema_version": "2",
        "name": "missing_required",
        "inputs": {"ticket_id": {"type": "string"}},
        # `tag` is required by tag_input; omit it -> violation.
        "steps": [{"id": "t", "uses": "tag", "with": {"ticket_id": "${{ inputs.ticket_id }}"}}],
    }
    res = _run(doc, {"ticket_id": tid}, tid, rebar_repo)
    assert res["status"] == "failed"
    assert "input contract violation (tag_input)" in res["error"]


def test_runtime_contractless_consumer_does_not_fail(rebar_repo) -> None:
    # A scripted step with NO declared input contract must run unimpeded (UNKNOWN =
    # skip), so contract-less workflows keep working.
    from rebar.llm.workflow import executor as _ex

    def _noop(ctx):
        return _ex.StepResult(outputs={"ok": True})

    doc = {
        "schema_version": "2",
        "name": "contractless",
        "steps": [{"id": "n", "uses": "noop_test_step", "with": {"anything": [1, 2, 3]}}],
    }
    res = _ex.run_workflow(doc, {}, scripted_registry={"noop_test_step": _noop})
    assert res.status == "succeeded"
    assert res.outputs["n"]["ok"] is True


# ── 2b. The DISTINCT validator-FAILURE signal (fail-loud, never false-pass) ────


def test_runtime_validator_failure_is_distinct(rebar_repo, monkeypatch) -> None:
    # If the validator ITSELF errors (here: a non-ValidationError raised while building
    # it), the step must fail with a DISTINCT "UNAVAILABLE/errored" message — never
    # silently pass the value through.
    from rebar import schemas

    real_validator = schemas.validator

    def _boom(name):
        # Break ONLY the consumer-input contract validator (not document validation,
        # which legitimately builds the workflow.v2 validator at run start).
        if name == "tag_input":
            raise RuntimeError(f"cannot build validator for {name}")
        return real_validator(name)

    monkeypatch.setattr(schemas, "validator", _boom)

    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    doc = {
        "schema_version": "2",
        "name": "validator_boom",
        "inputs": {"ticket_id": {"type": "string"}},
        "steps": [
            {
                "id": "t",
                "uses": "tag",
                "with": {"tag": "ok", "ticket_id": "${{ inputs.ticket_id }}"},
            },
        ],
    }
    res = _run(doc, {"ticket_id": tid}, tid, rebar_repo)
    assert res["status"] == "failed"
    assert "input validation UNAVAILABLE/errored (tag_input)" in res["error"]
    # Distinct from a plain violation message.
    assert "input contract violation" not in res["error"]
    # And the value was NOT applied to the store (never false-pass).
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "ok" not in state["tags"]


# ── 3. Editor "unchecked" badge flag ───────────────────────────────────────────


def test_contract_view_exposes_checked_flag() -> None:
    from rebar.llm.workflow import (
        editor,
        steps,  # noqa: F401 - registers built-in contracts
    )

    # A contract-less / unknown op → unchecked (opaque source).
    unchecked = editor.step_contract_view(None)
    assert unchecked["has_contract"] is False
    assert unchecked["checked"] is False
    unknown = editor.step_contract_view("definitely_not_a_registered_op")
    assert unknown["checked"] is False

    # A registered op WITH a contract → checked.
    checked = editor.step_contract_view("tag")
    assert checked["has_contract"] is True
    assert checked["checked"] is True
