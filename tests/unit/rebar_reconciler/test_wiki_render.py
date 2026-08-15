"""Unit tests for the DC segmenting Markdown-to-wiki renderer (story 271c, epic 708d).

The renderer's contract is "convert only what converts losslessly": structurally-safe
units go through pandoc, everything else is emitted byte-for-byte.

These tests NEVER ``importorskip`` and never monkeypatch pandoc. The ``wiki`` extra is
installed in dev/CI, and the pin assertions below are the point — a silently absent or
drifted pandoc must FAIL, not skip. The two fallback tests that need pandoc to be
missing simulate only the renderer's own probe, not the binary.
"""

from __future__ import annotations

import logging

import pytest

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    code_fragments,
    render_markdown_to_wiki,
    substitute_arrows,
)

_MIXED = """# Heading

Prose flows -> onward with **bold**.

- alpha
- beta

```python
obj->method()
```

| a | b |
|---|---|
| 1 | 2 |

Inline `obj->m()` next to <div>markup</div>.
"""


# ---------------------------------------------------------------------------
# Pin provenance — non-skipping (ADR 0095)
# ---------------------------------------------------------------------------


def test_pypandoc_and_pandoc_versions_are_pinned() -> None:
    """A drifted or absent pandoc must FAIL here, never silently skip."""
    import subprocess

    import pypandoc

    assert pypandoc.__version__ == "1.17"
    banner = subprocess.run(
        [wiki_render._pandoc_path() or "", "--version"], capture_output=True, text=True
    ).stdout
    assert banner.splitlines()[0].strip() == "pandoc 3.9"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("# Heading\n", "h1. {anchor:}Heading\n"),
        ("`mono`\n", "{{mono}}\n"),
        ("**b**\n", "*b*\n"),
        ("*i*\n", "_i_\n"),
        ("[l](http://x)\n", "[l|http://x]\n"),
    ],
)
def test_real_jira_writer_output_shapes(source: str, expected: str) -> None:
    """The exact ``commonmark -> jira --wrap=none`` shapes ADR 0095 records.

    Asserted against the REAL binary; no monkeypatch. The heading case is also why
    the renderer strips the content-free ``{anchor:}``.
    """
    import subprocess

    completed = subprocess.run(
        [wiki_render._pandoc_path() or "", "-f", "commonmark", "-t", "jira", "--wrap=none"],
        input=source,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout == expected


def test_pandoc_emits_no_color_form() -> None:
    """Color is never a pandoc OUTPUT, so it is only ever pre-existing wiki (ADR 0095)."""
    import subprocess

    for reader in ("commonmark", "html"):
        completed = subprocess.run(
            [wiki_render._pandoc_path() or "", "-f", reader, "-t", "jira", "--wrap=none"],
            input='<span style="color: red">red text</span>\n',
            capture_output=True,
            text=True,
        )
        assert "{color:" not in completed.stdout


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_dc_segmenting_passthrough() -> None:
    """Classifier-6 units render; every other unit survives byte-for-byte, in order."""
    out = render_markdown_to_wiki(_MIXED)

    assert "h1. Heading" in out
    assert "*bold*" in out
    assert "* alpha" in out
    # Locked and dangerous units are exact.
    assert "```python\nobj->method()\n```" in out
    assert "| a | b |\n|---|---|\n| 1 | 2 |" in out
    assert "Inline `obj->m()` next to <div>markup</div>." in out
    assert out.index("h1. Heading") < out.index("| a | b |")


def test_dc_heading_has_no_content_free_anchor() -> None:
    out = render_markdown_to_wiki("# Title\n")

    assert "h1. Title" in out
    assert "anchor" not in out


def test_dc_table_wrapped_once_in_noformat() -> None:
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"

    out = render_markdown_to_wiki(table)

    assert out.count("{noformat}") == 2
    assert "| a | b |\n|---|---|\n| 1 | 2 |" in out
    assert "\\-" not in out


def test_dc_box_table_is_wrapped_verbatim() -> None:
    out = render_markdown_to_wiki("+-----+-----+\n| a   | b   |\n+-----+-----+\n")

    assert "{noformat}" in out
    assert "+-----+-----+" in out


def test_dc_regex_dense_prose_is_exact() -> None:
    body = r"Match with [^a-z]+ and \d{2,4} in one line."

    assert render_markdown_to_wiki(body).strip() == body


def test_dc_html_comment_survives_alone_and_inline() -> None:
    marker = "<!-- rebar:reconciler-echo -->"

    assert marker in render_markdown_to_wiki(f"{marker}\n")
    assert marker in render_markdown_to_wiki(f"text {marker} more\n")


def test_dc_blank_line_spacing_is_preserved() -> None:
    assert "para one\n\n\npara two" in render_markdown_to_wiki("para one\n\n\npara two\n")


def test_dc_empty_and_blank_bodies_pass_through() -> None:
    assert render_markdown_to_wiki("") == ""
    assert render_markdown_to_wiki("   \n  ") == "   \n  "


# ---------------------------------------------------------------------------
# Arrows and code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("a -> b", "a → b"),
        ("a <- b", "a ← b"),
        ("a <-> b", "a ↔ b"),
        ("a => b", "a ⇒ b"),
        ("call `x->y` now", "call `x->y` now"),
        ("```\nx->y\n```", "```\nx->y\n```"),
        ("<!-- keep -->", "<!-- keep -->"),
        ("prose --> tail", "prose --> tail"),
    ],
)
def test_arrow_substitution_cases(source: str, expected: str) -> None:
    """Prose arrows convert; code arrows and both comment delimiters stay exact."""
    assert substitute_arrows(source) == expected


def test_dc_code_arrows_are_never_rewritten() -> None:
    out = render_markdown_to_wiki(_MIXED)

    assert "flows → onward" in out
    assert "obj->method()" in out
    assert "`obj->m()`" in out


def test_code_fragments_covers_fences_indents_and_inline() -> None:
    body = "para `inline` here\n\n```\nfenced line\n```\n\n    indented line\n"

    assert code_fragments(body) == ["inline", "fenced line", "indented line"]


# ---------------------------------------------------------------------------
# Pre-existing wiki + stability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "h1. Title\n",
        "Some {{monospace}} inline.\n",
        "A [label|https://example.invalid] link.\n",
        "{color:red}red text{color}\n",
        "{noformat}\n| a | b |\n{noformat}\n",
        "* *strong* bullet with {{mono}}\n",
    ],
)
def test_dc_preexisting_wiki_passes_exactly(body: str) -> None:
    """Block and inline Jira forms are never re-rendered as Markdown."""
    assert render_markdown_to_wiki(body) == body


@pytest.mark.parametrize(
    "body",
    [
        _MIXED,
        "# T\n\n- a **b**\n\n`mono`\n",
        "Prose with a [link](http://x) and *emph*.\n",
    ],
)
def test_dc_five_pass_stability(body: str) -> None:
    """Passes 2-5 are byte-identical: rendering settles and never ratchets."""
    passes = [render_markdown_to_wiki(body)]
    for _ in range(4):
        passes.append(render_markdown_to_wiki(passes[-1]))

    assert passes[1] == passes[2] == passes[3] == passes[4]


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_dc_pandoc_absent_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing pandoc returns the exact body and warns with a content-free reason."""
    monkeypatch.setattr(wiki_render, "_pandoc_path", lambda: None)

    with caplog.at_level(logging.WARNING, logger=wiki_render.logger.name):
        assert render_markdown_to_wiki(_MIXED) == _MIXED

    record = next(r for r in caplog.records if r.getMessage() == "wiki_render_fallback")
    assert record.reason == "pandoc_absent"  # type: ignore[attr-defined]
    assert _MIXED not in record.getMessage()


def test_dc_conversion_failure_returns_exact_unit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed conversion degrades that unit to its source, and never raises."""
    monkeypatch.setattr(wiki_render, "_convert", lambda markdown, pandoc, timeout=None: None)

    with caplog.at_level(logging.DEBUG, logger=wiki_render.logger.name):
        assert render_markdown_to_wiki("# Heading\n") == "# Heading\n"


def test_dc_preservation_failure_returns_exact_unit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A conversion that drops a code fragment is rejected, not shipped."""
    monkeypatch.setattr(
        wiki_render,
        "_convert",
        lambda markdown, pandoc, timeout=None: "the code vanished",
    )
    body = "text with `fragment` inline\n"

    with caplog.at_level(logging.DEBUG, logger=wiki_render.logger.name):
        assert render_markdown_to_wiki(body) == body

    events = [r for r in caplog.records if r.getMessage() == "wiki_render_fallback"]
    reasons = [getattr(r, "reason", None) for r in events]
    assert "preservation_failure" in reasons


def test_dc_codec_remains_identity() -> None:
    """This story wires nothing: the DC codec must still be the identity."""
    from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

    codec = WikiTextCodec()
    body = "# Heading\n\n- alpha\n"

    assert codec.to_wire(body) == body
    assert codec.normalize_outbound(body) == body
    assert codec.decode_inbound(body) == body


# ---------------------------------------------------------------------------
# ADR 0095
# ---------------------------------------------------------------------------


def test_adr_0095_records_the_required_sections() -> None:
    """The renderer's decision record must carry every section the story requires.

    An ADR that omits its pin provenance or its deferrals is how the next reader
    re-litigates a settled choice, so the structure is asserted rather than trusted.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    adr = (root / "docs/adr/0095-dc-segmenting-wiki-renderer.md").read_text(encoding="utf-8")

    for heading in ("## Decision", "## Invariants", "## Pin provenance", "## Alternatives"):
        assert heading in adr
    assert "## Platform" in adr
    assert "## Deferrals" in adr
    # The pin the non-skipping tests assert must be stated here too.
    assert "3.9" in adr
    assert "1.17" in adr
    # And the marker file backing the number bijection exists.
    assert (root / "docs/adr/.numbers/0095").read_text(encoding="utf-8").strip() == (
        "0095-dc-segmenting-wiki-renderer.md"
    )
