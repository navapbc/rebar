"""Unit tests for the v2 declarative IR: schema (branch/loop/map), frame-scoped
linting, and the binding grammar (`${{ loop.<var> }}` / `${{ map.<as> }}`).

The v1 contract (flat `needs` DAG, `if:` skip-guard) is covered by test_schema.py /
test_lint.py; these pin the v2 ADDITIONS: declarative control flow as data, the
mandatory loop `max_iterations`, frame-scoped reference integrity (a `needs` edge or
output reference may not cross a frame boundary), and the map `as`-binding scope.
"""

from __future__ import annotations

from rebar.llm.workflow import lint as L
from rebar.llm.workflow import schema as S


def _schema_errors(doc: dict) -> list[str]:
    """Schema (structural) errors only — drop the jsonschema-absent informational note."""
    return [e for e in S.validate_document(doc) if not e.startswith("note:")]


def _lint_errors(doc: dict) -> list[str]:
    """Error-severity semantic lint findings (location: message)."""
    return [f"{f.location}: {f.message}" for f in L.lint_document(doc) if f.severity == "error"]


# A fully-featured, VALID v2 workflow exercising branch + loop + map + bindings, with
# a single top-level terminal (`fanout`). Reused as the clean baseline.
def _valid_v2() -> dict:
    return {
        "schema_version": "2",
        "name": "demo",
        "inputs": {"items": {"type": "array", "required": True}},
        "steps": [
            {"id": "start", "uses": "noop"},
            {
                "id": "refine",
                "needs": ["start"],
                "loop": {
                    "max_iterations": 3,
                    "until": "${{ steps.attempt.outputs.done }}",
                    "var": "i",
                    "body": [
                        {"id": "attempt", "prompt": "refine", "with": {"n": "${{ loop.i }}"}},
                    ],
                },
            },
            {
                "id": "gate",
                "needs": ["refine"],
                "branch": {
                    "when": "${{ steps.start.outputs.ok }}",
                    "then": [{"id": "approve", "uses": "emit"}],
                    "else": [{"id": "reject", "uses": "emit"}],
                },
            },
            {
                "id": "fanout",
                "needs": ["gate"],
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "index_var": "ix",
                    "max_concurrency": 4,
                    "body": [
                        {"id": "process", "prompt": "proc", "with": {"x": "${{ map.item }}"}},
                    ],
                },
            },
        ],
    }


# ── Schema: the v2 control constructs validate, and their constraints bind ──────


def test_valid_v2_passes_schema() -> None:
    assert _schema_errors(_valid_v2()) == []


def test_loop_requires_max_iterations() -> None:
    doc = _valid_v2()
    del doc["steps"][1]["loop"]["max_iterations"]
    errs = _schema_errors(doc)
    assert any("max_iterations" in e for e in errs), errs


def test_loop_while_and_until_are_mutually_exclusive() -> None:
    doc = _valid_v2()
    doc["steps"][1]["loop"]["while"] = "${{ steps.attempt.outputs.go }}"
    # `until` is already set -> both present -> the loop's `not:{required:[while,until]}` fails.
    assert _schema_errors(doc) != []


def test_map_requires_as_and_over() -> None:
    doc = _valid_v2()
    del doc["steps"][3]["map"]["as"]
    assert any("as" in e for e in _schema_errors(doc))


def test_branch_requires_then() -> None:
    doc = _valid_v2()
    del doc["steps"][2]["branch"]["then"]
    assert any("then" in e for e in _schema_errors(doc))


def test_control_objects_reject_unknown_keys() -> None:
    doc = _valid_v2()
    doc["steps"][1]["loop"]["bogus"] = True
    assert _schema_errors(doc) != []


def test_step_cannot_mix_leaf_and_control() -> None:
    doc = _valid_v2()
    doc["steps"][1]["uses"] = "noop"  # a loop step ALSO carrying `uses` violates the oneOf
    assert _schema_errors(doc) != []


def test_control_step_rejects_with_block() -> None:
    doc = _valid_v2()
    doc["steps"][3]["with"] = {"x": 1}  # map step may not carry `with`
    assert _schema_errors(doc) != []


def test_nested_control_validates() -> None:
    # A map whose body contains a loop — recursion through $ref/#step holds.
    doc = {
        "schema_version": "2",
        "name": "nested",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {
                "id": "outer",
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "it",
                    "body": [
                        {
                            "id": "inner",
                            "loop": {
                                "max_iterations": 2,
                                "body": [{"id": "leaf", "uses": "noop"}],
                            },
                        }
                    ],
                },
            }
        ],
    }
    assert _schema_errors(doc) == []


# ── Lint: frame-scoped reference integrity + the binding grammar ────────────────


def test_valid_v2_lints_clean() -> None:
    assert _lint_errors(_valid_v2()) == []


def test_loop_var_binding_in_scope() -> None:
    doc = _valid_v2()
    assert not any("loop.i" in e for e in _lint_errors(doc))


def test_unknown_loop_var_is_flagged() -> None:
    doc = _valid_v2()
    doc["steps"][1]["loop"]["body"][0]["with"]["n"] = "${{ loop.j }}"  # no loop declares var j
    assert any("loop variable 'j'" in e and "not in scope" in e for e in _lint_errors(doc))


def test_map_binding_in_scope() -> None:
    doc = _valid_v2()
    assert not any("map.item" in e for e in _lint_errors(doc))


def test_unknown_map_binding_is_flagged() -> None:
    doc = _valid_v2()
    doc["steps"][3]["map"]["body"][0]["with"]["x"] = "${{ map.ghost }}"
    assert any("map binding 'ghost'" in e and "not in scope" in e for e in _lint_errors(doc))


def test_map_binding_does_not_leak_to_sibling_frame() -> None:
    # `item` is bound only inside the map body; a later top-level step cannot see it.
    doc = _valid_v2()
    doc["steps"].append(
        {"id": "after", "needs": ["fanout"], "uses": "emit", "with": {"x": "${{ map.item }}"}}
    )
    assert any("map binding 'item'" in e for e in _lint_errors(doc))


def test_needs_may_not_cross_into_a_nested_frame() -> None:
    # A top-level step needs a step that lives INSIDE the loop body -> unknown (the
    # `needs` edge crosses a frame boundary).
    doc = _valid_v2()
    doc["steps"][2]["needs"] = ["attempt"]  # `attempt` is inside refine's loop body
    assert any("unknown step 'attempt'" in e for e in _lint_errors(doc))


def test_needs_cycle_within_a_body_is_rejected() -> None:
    doc = _valid_v2()
    body = doc["steps"][3]["map"]["body"]
    body.append({"id": "second", "needs": ["process"], "uses": "noop"})
    body[0]["needs"] = ["second"]  # process <-> second cycle inside the map body
    assert any("cycle" in e for e in _lint_errors(doc))


def test_loop_condition_may_reference_its_own_body_output() -> None:
    # The "refine until the previous iteration's score is good" pattern: the loop
    # condition references a body step's output. That is legal (the interpreter
    # derives it from recorded outputs) and must NOT be flagged as unknown/forward.
    doc = _valid_v2()
    doc["steps"][1]["loop"]["until"] = "${{ steps.attempt.outputs.score }}"
    assert not any("attempt" in e for e in _lint_errors(doc))


def test_branch_condition_may_not_reference_secret() -> None:
    doc = _valid_v2()
    doc["steps"][2]["branch"]["when"] = "${{ secrets.TOKEN }}"
    assert any("credential" in e for e in _lint_errors(doc))


def test_map_over_resolves_in_parent_scope_not_its_own_binding() -> None:
    # `over` is evaluated BEFORE the fan-out, so it cannot reference the map's own
    # `as` binding (that only exists inside the body).
    doc = _valid_v2()
    doc["steps"][3]["map"]["over"] = "${{ map.item }}"
    assert any("map.over" in e and "not in scope" in e for e in _lint_errors(doc))


def test_outer_step_output_visible_in_nested_body() -> None:
    # A body step referencing an ENCLOSING-frame upstream output is in scope.
    doc = _valid_v2()
    doc["steps"][3]["map"]["body"][0]["with"]["y"] = "${{ steps.start.outputs.ok }}"
    assert not any("start" in e for e in _lint_errors(doc))


def test_same_frame_id_shadows_outer_for_needs_check() -> None:
    # A body step `b` references same-frame sibling `a` WITHOUT a needs edge, while an
    # outer step also named `a` exists. Same-frame precedence: the missing-needs error
    # must still fire (the outer `a` cannot silently satisfy the reference).
    doc = {
        "schema_version": "2",
        "name": "shadow",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {"id": "a", "uses": "noop"},
            {
                "id": "fan",
                "needs": ["a"],
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "body": [
                        {"id": "a", "uses": "noop"},
                        # references sibling `a` but declares no `needs: [a]`
                        {"id": "b", "uses": "noop", "with": {"x": "${{ steps.a.outputs.v }}"}},
                    ],
                },
            },
        ],
    }
    assert any("not an upstream dependency" in e for e in _lint_errors(doc))


def test_bare_control_condition_is_flagged() -> None:
    # A control condition must be a `${{ … }}` expression, never a bare truthy literal.
    doc = _valid_v2()
    doc["steps"][2]["branch"]["when"] = "yes"
    assert any("branch.when" in e and "expression" in e for e in _lint_errors(doc))
