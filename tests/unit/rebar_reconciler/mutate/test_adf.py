"""Tests for rebar_reconciler/adf.py — ADF round-trip conversion.

Tests assert behavioral contracts for:
  - adf_to_text: each ADF node type produces expected plain text
  - text_to_adf: plain text -> minimal ADF doc
  - Round-trip: adf_to_text(text_to_adf(text)) == text
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (importlib convention per conftest.py docs)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
ADF_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "adapters" / "jira" / "adf.py"
)


def _load_adf() -> ModuleType:
    spec = importlib.util.spec_from_file_location("adf", ADF_PATH)
    assert spec is not None and spec.loader is not None, f"Cannot load adf module from {ADF_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def adf_mod() -> ModuleType:
    return _load_adf()


# ---------------------------------------------------------------------------
# adf_to_text — individual node types
# ---------------------------------------------------------------------------


class TestAdfToTextDoc:
    def test_doc_with_paragraph(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hello"}],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "hello"


class TestAdfToTextParagraph:
    def test_simple_paragraph(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "line one"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "line two"}],
                },
            ],
        }
        assert adf_mod.adf_to_text(doc) == "line one\nline two"


class TestAdfToTextText:
    def test_plain_text(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "plain"}],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "plain"

    def test_bold_mark(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold",
                            "marks": [{"type": "strong"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "**bold**"

    def test_italic_mark(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "italic",
                            "marks": [{"type": "em"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "*italic*"

    def test_code_mark(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "code",
                            "marks": [{"type": "code"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "`code`"


class TestAdfToTextHeading:
    def test_h1(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Title"}],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "# Title"

    def test_h3(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "Sub"}],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "### Sub"


class TestAdfToTextBulletList:
    def test_bullet_list(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "alpha"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "beta"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "- alpha\n- beta"


class TestAdfToTextOrderedList:
    def test_ordered_list(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "first"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "second"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "1. first\n2. second"


class TestAdfToTextCodeBlock:
    def test_code_block(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": "x = 1"}],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "```\nx = 1\n```"


class TestAdfToTextBlockquote:
    def test_blockquote(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "blockquote",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "quoted"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "> quoted"


class TestAdfToTextHardBreak:
    def test_hard_break(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "hardBreak"},
                        {"type": "text", "text": "b"},
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "a\nb"


class TestAdfToTextMention:
    def test_mention(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "attrs": {"text": "alice", "id": "123"},
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "@alice"


class TestAdfToTextInlineCard:
    def test_inline_card(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "inlineCard",
                            "attrs": {"url": "https://example.com"},
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "[link](https://example.com)"


class TestAdfToTextRule:
    def test_rule(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "rule"},
            ],
        }
        assert adf_mod.adf_to_text(doc) == "---"


class TestAdfToTextTable:
    def test_table(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Name"}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Value"}],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "a"}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "1"}],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        result = adf_mod.adf_to_text(doc)
        assert "| Name | Value |" in result
        assert "| a | 1 |" in result


class TestAdfToTextMedia:
    def test_media_single(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "mediaSingle",
                    "attrs": {"id": "abc-123"},
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "[media: abc-123]"

    def test_media_group_no_id(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "mediaGroup", "attrs": {}},
            ],
        }
        assert adf_mod.adf_to_text(doc) == "[media: attachment]"


class TestAdfToTextPanel:
    def test_panel(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "panel",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "info"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "> [panel] info"


class TestAdfToTextExpand:
    def test_expand(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "expand",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "details"}],
                        }
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "details"


class TestAdfToTextEmoji:
    def test_emoji_short_name(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "emoji", "attrs": {"shortName": ":thumbsup:"}},
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == ":thumbsup:"

    def test_emoji_fallback(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "emoji", "attrs": {"text": "smile"}},
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "smile"


class TestAdfToTextStatus:
    def test_status(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "status", "attrs": {"text": "IN PROGRESS"}},
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "[STATUS: IN PROGRESS]"


class TestAdfToTextDate:
    def test_date(self, adf_mod):
        # 2024-01-15 00:00:00 UTC = 1705276800000 ms
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "date", "attrs": {"timestamp": "1705276800000"}},
                    ],
                }
            ],
        }
        assert adf_mod.adf_to_text(doc) == "2024-01-15"


# ---------------------------------------------------------------------------
# Unknown node type
# ---------------------------------------------------------------------------


class TestAdfToTextUnknown:
    def test_unknown_node(self, adf_mod):
        doc = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "futureNode"}],
        }
        assert adf_mod.adf_to_text(doc) == "[unsupported: futureNode]"


# ---------------------------------------------------------------------------
# Empty / null input
# ---------------------------------------------------------------------------


class TestAdfToTextEmpty:
    def test_none_input(self, adf_mod):
        assert adf_mod.adf_to_text(None) == ""

    def test_empty_dict(self, adf_mod):
        assert adf_mod.adf_to_text({}) == ""


# ---------------------------------------------------------------------------
# Complex document
# ---------------------------------------------------------------------------


class TestAdfToTextComplex:
    def test_nested_document(self, adf_mod):
        """A document with headings, nested lists, code blocks, and a table."""
        doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Overview"}],
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Some "},
                        {
                            "type": "text",
                            "text": "bold",
                            "marks": [{"type": "strong"}],
                        },
                        {"type": "text", "text": " text."},
                    ],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "item A"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "item B"}],
                                }
                            ],
                        },
                    ],
                },
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": "print('hi')"}],
                },
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Col"}],
                                        }
                                    ],
                                },
                            ],
                        },
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableCell",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "val"}],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        result = adf_mod.adf_to_text(doc)
        assert "# Overview" in result
        assert "**bold**" in result
        assert "- item A" in result
        assert "```" in result
        assert "| Col |" in result


# ---------------------------------------------------------------------------
# text_to_adf round-trip
# ---------------------------------------------------------------------------


class TestTextToAdfRoundTrip:
    @pytest.mark.parametrize(
        "text",
        [
            "hello world",
            # Structural lines keep their own line (hardBreak-separated), so a
            # two-line block round-trips verbatim. Soft-wrapped PROSE lines are
            # deliberately rejoined instead -- see
            # ``test_round_trip_joins_soft_wrapped_prose`` below.
            "- line one\n- line two",
            "line one\n\nline three",
            "",
            "single",
        ],
    )
    def test_round_trip_preserves_text(self, adf_mod, text: str):
        """adf_to_text(text_to_adf(text)) should reproduce the original text."""
        assert adf_mod.adf_to_text(adf_mod.text_to_adf(text)) == text

    def test_round_trip_joins_soft_wrapped_prose(self, adf_mod):
        """Hard-wrapped prose becomes ONE paragraph, and the join is idempotent.

        Rebar descriptions are authored hard-wrapped at a fixed column width;
        emitting one paragraph per source line made Jira render a visible break
        at every wrap point. Re-encoding the decoded value must reproduce it, or
        the description differ re-emits an update on every reconcile pass.
        """
        text = "line one\nline two"
        once = adf_mod.adf_to_text(adf_mod.text_to_adf(text))
        assert once == "line one line two"
        assert adf_mod.adf_to_text(adf_mod.text_to_adf(once)) == once

    def test_text_to_adf_structure(self, adf_mod):
        result = adf_mod.text_to_adf("hello\nworld")
        assert result["type"] == "doc"
        assert result["version"] == 1
        # One semantic paragraph -> exactly ONE paragraph node, soft-wrapped
        # lines joined with a single space.
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "paragraph"
        assert result["content"][0]["content"] == [{"type": "text", "text": "hello world"}]

    def test_text_to_adf_structural_lines_use_hard_breaks(self, adf_mod):
        """Structural (markdown-marker) lines stay separate via ``hardBreak``."""
        result = adf_mod.text_to_adf("- one\n- two")
        assert len(result["content"]) == 1
        assert result["content"][0]["content"] == [
            {"type": "text", "text": "- one"},
            {"type": "hardBreak"},
            {"type": "text", "text": "- two"},
        ]

    def test_text_to_adf_blank_line_emits_empty_paragraph(self, adf_mod):
        """Blank lines still emit an empty paragraph -- it carries the separation."""
        result = adf_mod.text_to_adf("a\n\nb")
        assert [n["type"] for n in result["content"]] == ["paragraph"] * 3
        assert result["content"][1]["content"] == []

    def test_text_to_adf_preserves_fenced_code_verbatim(self, adf_mod):
        """Lines inside a ``` fence are never joined or stripped."""
        text = "```\n  foo = 1\nbar = 2\n```"
        assert adf_mod.adf_to_text(adf_mod.text_to_adf(text)) == text

    def test_normalize_description_is_idempotent(self, adf_mod):
        """The public compare-path helper is a fixed point after one pass."""
        text = "wrapped prose that\ncontinues here.\n\n- a\n- b"
        once = adf_mod.normalize_description(text)
        assert once == "wrapped prose that continues here.\n\n- a\n- b"
        assert adf_mod.normalize_description(once) == once

    def test_normalize_description_guards_non_str(self, adf_mod):
        assert adf_mod.normalize_description(None) is None
        assert adf_mod.normalize_description(123) == 123
