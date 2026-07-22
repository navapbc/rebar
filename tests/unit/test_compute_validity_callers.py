"""Compatibility guard for the additive plan-review health mapping."""

from __future__ import annotations

import ast
from pathlib import Path

from rebar.llm.plan_review import attest


def _trees_with_parents():
    root = Path(__file__).resolve().parents[2] / "src" / "rebar"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent
        yield path, tree


def _validity_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
        == "compute_validity"
    ]


def test_every_compute_validity_call_consumes_a_mapping_not_a_tuple() -> None:
    assert hasattr(attest, "PlanValidityProfile"), "additive validity contract is absent"
    calls = []
    for path, tree in _trees_with_parents():
        for node in _validity_calls(tree):
            calls.append((path, node))
            parent = node.parent
            if isinstance(parent, ast.Assign):
                assert all(
                    not isinstance(target, (ast.Tuple, ast.List)) for target in parent.targets
                )
                assigned_names = {
                    target.id for target in parent.targets if isinstance(target, ast.Name)
                }
                consumers = [
                    candidate.parent
                    for candidate in ast.walk(tree)
                    if isinstance(candidate, ast.Name)
                    and candidate.id in assigned_names
                    and isinstance(candidate.ctx, ast.Load)
                ]
                assert any(
                    isinstance(consumer, ast.Subscript)
                    or (
                        isinstance(consumer, ast.Attribute)
                        and consumer.attr == "get"
                        and isinstance(consumer.parent, ast.Call)
                    )
                    for consumer in consumers
                ), f"{path}:{node.lineno} does not consume validity by named key"
                assert not any(
                    isinstance(consumer, ast.Compare)
                    and any(isinstance(term, ast.Dict) for term in consumer.comparators)
                    for consumer in consumers
                )
            else:
                assert isinstance(parent, ast.Attribute) and parent.attr == "get"
    assert calls, "compute_validity must have in-repository consumers"


def test_plan_validity_profiles_are_selected_by_named_keyword() -> None:
    assert hasattr(attest, "PlanValidityProfile"), "plan validity profiles are absent"
    profiled = []
    for path, tree in _trees_with_parents():
        for node in _validity_calls(tree):
            if any(kw.arg == "profile" for kw in node.keywords):
                profiled.append(path)
    assert profiled, "plan validity profiles are not wired at any caller"
