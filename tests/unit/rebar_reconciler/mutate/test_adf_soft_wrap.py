"""Preserve soft-wrapped text-to-ADF convergence (bug 4e21).

Lines within a prose block join one paragraph, blank lines retain paragraph boundaries,
and lists, headings, blockquotes, tables, and fenced code remain structural. The
text-to-ADF-to-text transform is idempotent so the differ does not re-emit updates.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ADF_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "adapters" / "jira" / "adf.py"
)


def _load_adf() -> ModuleType:
    spec = importlib.util.spec_from_file_location("adf_soft_wrap", ADF_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def adf_mod() -> ModuleType:
    return _load_adf()


def _paragraph_texts(doc: dict) -> list[str]:
    """Flatten each top-level node to its concatenated text (hardBreak -> newline)."""
    out: list[str] = []
    for node in doc["content"]:
        buf = ""
        for child in node.get("content", []):
            if child.get("type") == "hardBreak":
                buf += "\n"
            else:
                buf += child.get("text", "")
        out.append(buf)
    return out


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------


class TestSoftWrapJoining:
    def test_soft_wrapped_prose_becomes_one_paragraph(self, adf_mod):
        """The reported bug: a wrapped prose paragraph must not become N paragraphs."""
        text = (
            "User identity is the second real difference. Jira Cloud identifies users by "
            "an opaque\naccountId; Data Center identifies them by name. This leaks into "
            "two places."
        )
        doc = adf_mod.text_to_adf(text)
        assert len(doc["content"]) == 1, (
            f"expected ONE paragraph for a soft-wrapped block, got {len(doc['content'])}"
        )
        assert _paragraph_texts(doc) == [
            "User identity is the second real difference. Jira Cloud identifies users by "
            "an opaque accountId; Data Center identifies them by name. This leaks into "
            "two places."
        ]

    def test_no_hard_break_inside_joined_prose(self, adf_mod):
        """A joined prose block must contain no hardBreak -- that is still a line break."""
        doc = adf_mod.text_to_adf("alpha beta\ngamma delta")
        kinds = [c.get("type") for n in doc["content"] for c in n.get("content", [])]
        assert "hardBreak" not in kinds, f"prose was separated by hardBreak: {kinds}"


# ---------------------------------------------------------------------------
# Spacing that MUST survive (the "without removing legitimate breaks" half)
# ---------------------------------------------------------------------------


class TestLegitimateBreaksPreserved:
    def test_blank_line_still_separates_paragraphs(self, adf_mod):
        text = "First paragraph here.\n\nSecond paragraph here."
        rendered = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert rendered == text

    def test_list_items_are_not_joined_into_a_run_on(self, adf_mod):
        text = "- first bullet\n- second bullet\n- third bullet"
        rendered = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert rendered == text, "list items were collapsed onto one line"

    def test_heading_is_not_joined_to_following_prose(self, adf_mod):
        text = "## Context / Problem\nThe adapter splits on newlines."
        rendered = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert rendered == text, "heading was run together with the prose beneath it"

    def test_fenced_code_lines_are_never_joined(self, adf_mod):
        text = "```\nfoo = 1\nbar = 2\n```"
        rendered = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert rendered == text, "code-fence contents were reflowed"

    def test_numbered_list_and_quote_lines_are_not_joined(self, adf_mod):
        text = "1. first step\n2. second step\n\n> quoted line one\n> quoted line two"
        rendered = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert rendered == text


# ---------------------------------------------------------------------------
# Churn safety -- the gate that prior bugs 626d / 85a1 / DIG-4175 regressed on
# ---------------------------------------------------------------------------


class TestNoResyncChurn:
    @pytest.mark.parametrize(
        "text",
        [
            "hello world",
            "line one\nline two",
            "line one\n\nline three",
            "",
            "single",
            "## Heading\n\nSome wrapped prose that\ncontinues on the next line.\n\n- a\n- b",
            "trailing spaces   \nnext line",
        ],
    )
    def test_transform_is_idempotent(self, adf_mod, text: str):
        """normalize(normalize(t)) == normalize(t).

        Without this the differ compares a hard-wrapped local value against a joined
        Jira-decoded value forever and re-emits a description update every pass.
        """
        once = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        twice = adf_mod.adf_to_text(adf_mod.text_to_adf(once))
        assert twice == once, f"transform not idempotent: {once!r} -> {twice!r}"

    def test_already_normalized_text_round_trips_exactly(self, adf_mod):
        """Round-trip is lossless on already-normalized text (the restated invariant)."""
        normalized = "One single line paragraph.\n\nAnother paragraph.\n\n- bullet a\n- bullet b"
        assert adf_mod.adf_to_text(adf_mod.text_to_adf(normalized)) == normalized
