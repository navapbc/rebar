"""Drift gate for the client AGENTS.md template's rebar-usage region (story 064f).

``templates/rebar-usage.md`` is the single **canonical source** for the portable
"how to drive rebar" guidance. ``templates/AGENTS.md`` — the file a project adopting
rebar copies into its own repo — embeds a **byte-identical** copy of that source
between the markers::

    <!-- BEGIN rebar-usage (generated; do not edit) -->
    <!-- END rebar-usage -->

This gate fails if the embedded region diverges from the canonical source, or if the
markers go missing — so the shipped template cannot silently drift from its source.
Client placeholders (build / test / landing commands) live OUTSIDE the markers and are
deliberately NOT checked. After editing the canonical source, re-sync by replacing the
lines between the markers in ``templates/AGENTS.md`` with the exact contents of
``templates/rebar-usage.md`` (the markers stay; the placeholders around them are untouched).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"
CANONICAL = TEMPLATES_DIR / "rebar-usage.md"
TEMPLATE = TEMPLATES_DIR / "AGENTS.md"

BEGIN = "<!-- BEGIN rebar-usage (generated; do not edit) -->"
END = "<!-- END rebar-usage -->"

# Rebar-internal dev specifics that must NEVER leak into a client-facing template.
FORBIDDEN_INTERNAL = ("Gerrit", "module-size", "Serena")


def extract_region(text: str) -> str | None:
    """Return the content of the lines strictly BETWEEN the BEGIN and END marker
    lines, normalized to a single trailing newline — or ``None`` if either marker
    line is absent. The marker lines themselves are excluded.

    Line-based extraction (not raw string slicing) so the reconstructed region is
    byte-identical to a normal source file: ``"\\n".join(inner_lines) + "\\n"``
    reproduces ``canonical.read_text()`` exactly when the canonical file ends in a
    single newline.
    """
    lines = text.splitlines()
    if BEGIN not in lines or END not in lines:
        return None
    i = lines.index(BEGIN)
    j = lines.index(END)
    if j <= i:
        return None
    inner = lines[i + 1 : j]
    return "\n".join(inner) + "\n"


# ─────────────────────────── HAPPY PATH (shown) ───────────────────────────────


def test_canonical_source_exists() -> None:
    assert CANONICAL.is_file(), f"canonical rebar-usage source missing: {CANONICAL}"


def test_template_exists() -> None:
    assert TEMPLATE.is_file(), f"client AGENTS.md template missing: {TEMPLATE}"


def test_embedded_region_is_byte_identical_to_canonical() -> None:
    """The delimited region in templates/AGENTS.md equals the canonical source."""
    region = extract_region(TEMPLATE.read_text(encoding="utf-8"))
    assert region is not None, f"{TEMPLATE} is missing the rebar-usage BEGIN/END markers"
    assert region == CANONICAL.read_text(encoding="utf-8"), (
        "templates/AGENTS.md rebar-usage region has drifted from "
        "templates/rebar-usage.md; re-sync the delimited region."
    )


# ─────────────────────────── EDGE CASES / SELF-TESTS (held out) ────────────────


def test_divergence_is_detected() -> None:
    """A perturbed region no longer matches the canonical source — the checker has
    teeth (it is not a tautology that always passes)."""
    region = extract_region(TEMPLATE.read_text(encoding="utf-8"))
    assert region is not None
    perturbed = region + "an injected drifting line\n"
    assert perturbed != CANONICAL.read_text(encoding="utf-8")


def test_missing_markers_return_none() -> None:
    """extract_region reports absence rather than silently returning empty."""
    assert extract_region("no markers at all\n") is None
    assert extract_region(f"{BEGIN}\nonly a begin marker\n") is None
    assert extract_region(f"{END}\nonly an end marker\n") is None


def test_end_before_begin_returns_none() -> None:
    """A malformed template with END above BEGIN is treated as missing, not matched."""
    assert extract_region(f"{END}\nbody\n{BEGIN}\n") is None


def test_region_roundtrips_a_synthetic_block() -> None:
    """A well-formed synthetic template's region reconstructs its embedded body
    byte-for-byte (the byte-identity contract, independent of the real files)."""
    body = "# Portable\n\n- a\n- b\n"
    synthetic = f"outside top\n{BEGIN}\n{body.rstrip(chr(10))}\n{END}\noutside bottom\n"
    assert extract_region(synthetic) == body


# ─────────────────────────── CONTENT INVARIANTS (held out) ─────────────────────


def test_template_is_provider_neutral() -> None:
    """The client template carries no rebar-internal dev specifics."""
    text = TEMPLATE.read_text(encoding="utf-8")
    leaked = [w for w in FORBIDDEN_INTERNAL if w in text]
    assert not leaked, f"client template leaks rebar-internal content: {leaked}"


def test_client_placeholders_sit_outside_the_region() -> None:
    """The build/test/landing placeholders are OUTSIDE the delimited block, so a
    client edits them without touching the generated rebar-usage region."""
    text = TEMPLATE.read_text(encoding="utf-8")
    region = extract_region(text) or ""
    # The template must have at least one client placeholder marker, and it must not
    # fall inside the generated region.
    assert "PLACEHOLDER" in text, "template has no client placeholder markers"
    assert "PLACEHOLDER" not in region, "a client placeholder leaked inside the generated region"
