"""Contracts for the agentic code-review documentation overlay."""

from __future__ import annotations

from importlib import resources

import pytest

from rebar.llm.code_review import registry
from rebar.llm.prompting.prompts import get_prompt

pytestmark = pytest.mark.unit


def _rubric() -> str:
    return resources.files("rebar.llm").joinpath("reviewers/code-review-docs.md").read_text("utf-8")


def test_documentation_overlay_uses_agentic_execution() -> None:
    prompt = get_prompt("code-review-docs")
    routing = registry.routing_index()["docs"]

    assert prompt.execution_mode == "agentic"
    assert routing["exec"] == "AGENT"


def test_documentation_overlay_preserves_routing_posture() -> None:
    routing = registry.routing_index()["docs"]

    assert routing["applies_to"] == ["**/*.md", "docs/**", "**/README*"]
    assert routing["default_posture"] == "advisory"
    assert routing["blocking_enabled"] is False
    assert routing["nit_suppressed"] is True


def test_rubric_requires_changed_text_and_repository_evidence() -> None:
    rubric = _rubric().lower()

    assert "changed `path:line`" in rubric
    assert "second repository source" in rubric
    assert "bounded search" in rubric
    assert "repository-wide search with no match does not prove" in rubric
    assert "unrelated defects in unchanged documentation" in rubric


def test_rubric_applies_documentation_roles_and_generated_ownership() -> None:
    rubric = _rubric()

    assert "docs/documentation-policy.md" in rubric
    assert "docs/generated-artifacts.md" in rubric
    for role in ("Internal documentation", "External documentation", "Shipped help", "Comments"):
        assert role in rubric
    assert "ADRs" in rubric
    assert "Generated artifacts" in rubric


def test_rubric_requires_abstention_and_protects_excluded_content() -> None:
    rubric = _rubric().lower()

    assert "abstain" in rubric
    for excluded in (
        "protected evidence",
        "quotations",
        "historical ticket events",
        "adr decision substance",
        "punctuation",
        "character restrictions",
        "vocabulary",
        "diction",
        "tone",
        "wrapping",
        "subjective concision",
    ):
        assert excluded in rubric


def test_obsolete_diff_only_contract_is_removed() -> None:
    rubric = _rubric().lower()

    assert "both sides of the contradiction" not in rubric
    assert "diff text alone" not in rubric
