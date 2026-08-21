"""Generated-surface catalog and marker contracts.

Every generated file identifies its generator through a text banner or a reserved JSON key.
``docs/generated-artifacts.md`` catalogs generated surfaces and hand-authored parity surfaces.
The tests keep that catalog aligned with the focused registries below. See ticket
``forworn-zanyish-narwhale`` for the adoption history.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar.llm.plan_review import guide_parity
from rebar.llm.prompting import prompts

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = "docs/generated-artifacts.md"
POLICY_PATH = "docs/documentation-policy.md"
POLICY_ROLES = (
    "Tickets",
    "ADRs",
    "Internal documentation",
    "External documentation",
    "Shipped help",
    "Comments",
    "Generated artifacts",
    "Protected evidence",
)

#: The reserved top-level key that marks a generated JSON document.
MARKER_KEY = "_generated_by"

#: Every generated surface maps a path to the regenerate command its marker must name.
#: A new generator MUST be added here and to the generated-artifact catalog together.
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

#: Hand-authored parity-gated surfaces that must appear in the catalog's second table.
#: They must not carry a generated banner because no generator exists for them.
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


# ── the generated-artifact catalog ──────────────────────────────────────────────

_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def _catalog_section(section: str) -> str:
    """Return the content under one third-level catalog heading."""
    catalog = _read(CATALOG_PATH)
    start = catalog.index(f"### {section}")
    rest = catalog[start + len(section) :]
    end = rest.find("\n## ")
    body = rest[: end if end != -1 else len(rest)]
    nxt = body.find("\n### ")
    if nxt != -1:
        body = body[:nxt]
    return body


def _catalog_rows(section: str) -> set[str]:
    """Return paths from the table under one third-level catalog heading."""
    return set(_ROW.findall(_catalog_section(section)))


def test_catalog_table_matches_registry() -> None:
    """The generated-surface table cannot omit a known derived surface (nor invent one)."""
    assert _catalog_rows("Generated files") == set(GENERATED)


def test_catalog_lists_the_hand_authored_gated_surfaces() -> None:
    assert _catalog_rows("Hand-authored parity-gated files") == set(GATED_HAND_AUTHORED)


def test_catalog_keeps_the_parseable_section_order() -> None:
    catalog = _read(CATALOG_PATH)
    generated = catalog.index("### Generated files")
    parity = catalog.index("### Hand-authored parity-gated files")
    assert generated < parity


def test_catalog_rows_name_the_regenerate_command() -> None:
    """Each generated row must carry the command that regenerates it."""
    body = _catalog_section("Generated files")
    for rel, command in GENERATED.items():
        row = next(ln for ln in body.splitlines() if ln.startswith(f"| `{rel}`"))
        assert command in row, f"catalog row for {rel} must name `{command}`"


@pytest.mark.parametrize("rel", sorted(set(GENERATED) - set(JSON_SURFACES)))
def test_text_surface_carries_a_banner(rel: str) -> None:
    """Markdown/Python surfaces announce themselves near the top, naming their generator."""
    head = "\n".join(_read(rel).splitlines()[:8])
    assert "generated" in head.lower(), f"{rel} does not announce itself as generated"
    assert GENERATED[rel] in head, f"{rel}'s banner must name `{GENERATED[rel]}`"


def test_contributing_points_at_the_generated_artifacts_catalog() -> None:
    contributing = _read("CONTRIBUTING.md")
    assert "Generated artifacts" in contributing
    assert CATALOG_PATH in contributing


def test_policy_defines_all_documentation_roles() -> None:
    policy = _read(POLICY_PATH)
    header = (
        "| Role | Primary audience | Purpose | Lifecycle | Canonical source | Citation use | "
        "Correction method | Exclusions | Example |"
    )
    assert header in policy
    for role in POLICY_ROLES:
        assert f"| {role} |" in policy


def test_policy_and_catalog_pointers_are_discoverable() -> None:
    assert POLICY_PATH in _read("AGENTS.md")
    contributing = _read("CONTRIBUTING.md")
    assert POLICY_PATH in contributing
    assert CATALOG_PATH in contributing
    docs_index = _read("docs/README.md")
    assert "(documentation-policy.md)" in docs_index
    assert "(generated-artifacts.md)" in docs_index


def test_contributing_drops_the_removed_catalog_anchor() -> None:
    removed_anchor = "docs/README.md" + "#generated-artifacts"
    assert removed_anchor not in _read("CONTRIBUTING.md")
