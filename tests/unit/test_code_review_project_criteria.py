"""Behavioral coverage for project-owned code-review criteria."""

from __future__ import annotations

import pytest

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.criteria.model import CriteriaError


def test_resolver_maps_project_criterion_into_code_review_namespace() -> None:
    assert criterion_prompt_id("project.foo", gate_key="code_review") == "code-review-project-foo"


def test_resolver_maps_builtin_criterion_into_code_review_namespace() -> None:
    assert criterion_prompt_id("F1", gate_key="code_review") == "code-review-F1"


def test_resolver_preserves_default_and_rejects_unknown_gate() -> None:
    assert criterion_prompt_id("F1") == "plan-review-F1"
    assert criterion_prompt_id("project.foo") == "plan-review-project-foo"

    with pytest.raises(CriteriaError, match="unknown gate"):
        criterion_prompt_id("project.foo", gate_key="completion")


def test_resolver_keeps_gate_namespaces_disjoint() -> None:
    plan_review_id = criterion_prompt_id("project.foo")
    code_review_id = criterion_prompt_id("project.foo", gate_key="code_review")

    assert plan_review_id == "plan-review-project-foo"
    assert code_review_id == "code-review-project-foo"
    assert plan_review_id != code_review_id
