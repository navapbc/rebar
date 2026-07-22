"""Documentation-consistency checks for docs/chatgpt-agent-guide.md and its AGENTS.md link."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GUIDE = _ROOT / "docs" / "chatgpt-agent-guide.md"
_AGENTS = _ROOT / "AGENTS.md"


def _guide_text() -> str:
    return _GUIDE.read_text().lower()


def test_agents_md_links_to_the_guide():
    text = _AGENTS.read_text()
    assert "chatgpt-agent-guide.md" in text


def test_guide_does_not_recommend_github_issues_as_a_substitute():
    text = _guide_text()
    substitute_phrases = ("instead of", "substitute for", "in place of")
    negations = ("not a", "not the", "never", "do not", "don't")
    for window in re.finditer(r"github issues.{0,80}|.{0,120}github issues", text, re.DOTALL):
        snippet = window.group(0)
        if not any(p in snippet for p in substitute_phrases):
            continue
        assert any(n in snippet for n in negations), (
            f"guide affirmatively pairs 'github issues' with a substitution phrase: {snippet!r}"
        )


def test_guide_does_not_sanction_direct_contents_api_writes():
    text = _guide_text()
    for window in re.finditer(r"contents api.{0,80}|.{0,80}contents api", text, re.DOTALL):
        snippet = window.group(0)
        assert "tickets branch" not in snippet or "never" in snippet, (
            f"guide pairs 'contents api' with 'tickets branch' affirmatively: {snippet!r}"
        )


def test_guide_names_rebar_import_as_the_only_sanctioned_ingest_mechanism():
    text = _guide_text()
    assert "rebar import" in text
    assert "raw event" in text


def test_guide_fallback_fields_match_export_schema_required_array():
    import json

    schema = json.loads((_ROOT / "src/rebar/schemas/export.schema.json").read_text())
    required = schema["required"]
    text = _guide_text()
    for field in required:
        assert field in text, f"required field {field!r} missing from guide"
