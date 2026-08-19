"""Ticket 9100: every DERIVED surface says so, and `docs/README.md` indexes them all.

Story 316a burned four CI iterations because a plan cited a *generated* file by line
number and the implementer edited the artifact instead of its source. The fix is
two-part and both parts are pinned here:

1. **Each generated file is self-identifying** — a banner (markdown / Python) or, where
   the format has no comment syntax, a reserved top-level ``_generated_by`` key (JSON).
2. **`docs/README.md` carries one table indexing every derived surface** — so an author
   who does not already know which of nine files is derived has one place to look.

The table is the thing that can silently go stale, so :func:`test_readme_table_matches_registry`
compares its row set against :data:`GENERATED` + :data:`GATED_HAND_AUTHORED` below — adding a
generated surface without a table row fails here.

Markdown/Python banners are byte-verified against their generators by the existing
per-generator suites (``test_gen_cli_reference.py`` etc.); what those cannot catch is a
*missing table row*, which is this module's job. The two JSON markers are new, so their
fixed-point property is proven here directly: a marker that a regeneration strips would fail
the drift gate, which is worse than no marker at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar.llm.plan_review import guide_parity
from rebar.llm.prompting import prompts

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The reserved top-level key that marks a generated JSON document.
MARKER_KEY = "_generated_by"

#: Every GENERATED surface: path → the regenerate command its marker must name.
#: A new generator MUST be added here and to the `docs/README.md` table together.
GENERATED: dict[str, str] = {
    "docs/cli-reference.md": "python scripts/gen_cli_reference.py",
    "docs/config-reference.md": "python scripts/gen_config_reference.py",
    "docs/security.md": "python scripts/gen_config_reference.py",
    "docs/env-vars.md": "python scripts/gen_env_registry.py",
    "docs/mcp-reference.md": "python scripts/gen_mcp_reference.py",
    "docs/plan-review-criteria-guide.md": (
        "python -m rebar.llm.plan_review.registry regenerate-criteria-guide"
    ),
    "src/rebar/types.py": "python -m rebar.schemas.gen_types",
    "src/rebar/llm/reviewers/index.json": (
        "python -m rebar.llm.prompting.prompts regenerate-index"
    ),
    "src/rebar/_guides/criterion-pins.json": (
        "python -m rebar.llm.plan_review.guide_parity regenerate"
    ),
}

#: Surfaces that are HAND-AUTHORED but gated for parity. These must appear in the
#: README's second table and must NOT carry a generated banner — bannering them would
#: send an author to a generator that does not exist.
GATED_HAND_AUTHORED: frozenset[str] = frozenset(
    {
        "server.json",
        "src/rebar/llm/plan_review/criteria_routing.json",
        "src/rebar/llm/reviewers/*.md",
    }
)

#: JSON surfaces, whose marker lives in a reserved key rather than a comment.
JSON_SURFACES = (
    "src/rebar/llm/reviewers/index.json",
    "src/rebar/_guides/criterion-pins.json",
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ── the two NEW JSON markers ────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", JSON_SURFACES)
def test_committed_json_surface_carries_marker(rel: str) -> None:
    doc = json.loads(_read(rel))
    assert MARKER_KEY in doc, f"{rel} has no {MARKER_KEY} — JSON cannot carry a banner"
    assert GENERATED[rel] in doc[MARKER_KEY], (
        f"{rel}'s {MARKER_KEY} must name its regenerate command {GENERATED[rel]!r}; "
        f"got {doc[MARKER_KEY]!r}"
    )


def test_prompt_index_marker_is_a_fixed_point() -> None:
    """Regenerating `reviewers/index.json` reproduces the marker byte-for-byte.

    A marker the generator strips would go stale on the next regeneration and fail the
    Prompt-index drift gate — the exact failure this ticket exists to prevent.
    """
    committed = _read("src/rebar/llm/reviewers/index.json")
    regenerated = json.dumps(prompts.build_index_document(), indent=2, ensure_ascii=False) + "\n"
    assert regenerated == committed


def test_guide_pins_marker_is_a_fixed_point() -> None:
    committed = _read("src/rebar/_guides/criterion-pins.json")
    regenerated = (
        json.dumps(guide_parity.build_guide_pins(), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )
    assert regenerated == committed


def test_marker_does_not_leak_into_the_pure_index_builder() -> None:
    """`build_prompt_index()` stays the marker-less prompt mapping.

    Its invariant tests (one-default, dimension-collision, retire-on-regen) treat every
    key as a prompt id, so a marker here would corrupt them.
    """
    assert MARKER_KEY not in prompts.build_prompt_index()
    assert MARKER_KEY in prompts.build_index_document()
    assert {k: v for k, v in prompts.build_index_document().items() if k != MARKER_KEY} == (
        prompts.build_prompt_index()
    )


def test_load_catalog_ignores_the_reserved_marker_key() -> None:
    """The reserved key must never become a phantom reviewer."""
    catalog = prompts.load_catalog()
    assert MARKER_KEY not in catalog
    assert not any(rid.startswith("_") for rid in catalog)
    # and the catalog still resolves the real reviewers it always did
    assert "ticket-quality" in catalog


def test_guide_pins_marker_is_inert_for_the_parity_gate() -> None:
    """`diff_pins` reads only `schema_version` and `guides`, so the marker is ignored."""
    assert (
        guide_parity.diff_pins(
            guide_parity.load_guide_pins(),
            guide_parity._real_guides(),
            guide_parity._real_criteria(),
        )
        == []
    )


# ── the docs/README.md index (the part that can silently go stale) ───────────────

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def _readme_rows(section: str) -> set[str]:
    """Paths in the table under the `### <section>` heading of the Generated-artifacts part."""
    readme = _read("docs/README.md")
    start = readme.index(f"### {section}")
    rest = readme[start + len(section) :]
    end = rest.find("\n## ")
    body = rest[: end if end != -1 else len(rest)]
    nxt = body.find("\n### ")
    if nxt != -1:
        body = body[:nxt]
    return set(_ROW.findall(body))


def test_readme_table_matches_registry() -> None:
    """The generated-surface table cannot omit a known derived surface (nor invent one)."""
    assert _readme_rows("Generated — never edit these by hand") == set(GENERATED)


def test_readme_lists_the_hand_authored_gated_surfaces() -> None:
    assert _readme_rows("Hand-authored but parity-gated — do NOT banner these") == set(
        GATED_HAND_AUTHORED
    )


def test_readme_rows_name_the_regenerate_command() -> None:
    """Each generated row must carry the command that regenerates it."""
    readme = _read("docs/README.md")
    start = readme.index("### Generated — never edit these by hand")
    body = readme[start : start + readme[start:].find("\n### ")]
    for rel, command in GENERATED.items():
        row = next(ln for ln in body.splitlines() if ln.startswith(f"| `{rel}`"))
        assert command in row, f"docs/README.md row for {rel} must name `{command}`"


@pytest.mark.parametrize("rel", sorted(set(GENERATED) - set(JSON_SURFACES)))
def test_text_surface_carries_a_banner(rel: str) -> None:
    """Markdown/Python surfaces announce themselves near the top, naming their generator."""
    head = "\n".join(_read(rel).splitlines()[:8])
    assert "generated" in head.lower(), f"{rel} does not announce itself as generated"
    assert GENERATED[rel] in head, f"{rel}'s banner must name `{GENERATED[rel]}`"


def test_contributing_points_at_the_generated_artifacts_table() -> None:
    contributing = _read("CONTRIBUTING.md")
    assert "Generated artifacts" in contributing
    assert "docs/README.md#generated-artifacts" in contributing
