"""WS-F2: prompt variant overlays (+ cycle guard), front-matter parity, JSON schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.prompts import PromptError


def _rv():
    return prompts.get_reviewer("ticket-quality")


def _write(tmp_path: Path, name: str, text: str) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / name).write_text(text)


# ── front-matter ──────────────────────────────────────────────────────────────


def test_parse_front_matter() -> None:
    meta, body = prompts.parse_front_matter(
        "---\nvariables: [a, b]\nrequired: [a]\n---\nBODY {{a}}"
    )
    assert meta == {"variables": ["a", "b"], "required": ["a"]}
    assert body == "BODY {{a}}"


def test_no_front_matter_is_passthrough() -> None:
    meta, body = prompts.parse_front_matter("just a body {{x}}")
    assert meta == {} and body == "just a body {{x}}"


# ── variant overlays + cycle guard ────────────────────────────────────────────


def test_variant_overlays_base_via_marker(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "BASE {{ticket_id}}")
    _write(tmp_path, "ticket-quality.strict.md", "<!--base-->\nAND be strict.")
    body = prompts.canonical_prompt_text(_rv(), repo_root=str(tmp_path), variant="strict")
    assert body == "BASE {{ticket_id}}\nAND be strict."


def test_variant_without_marker_is_full_override(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "BASE")
    _write(tmp_path, "ticket-quality.terse.md", "JUST THIS {{ticket_id}}")
    body = prompts.canonical_prompt_text(_rv(), repo_root=str(tmp_path), variant="terse")
    assert body == "JUST THIS {{ticket_id}}"


def test_variant_chain_via_variant_of(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "L0")
    _write(tmp_path, "ticket-quality.mid.md", "<!--base-->/L1")
    _write(tmp_path, "ticket-quality.top.md", "---\nvariant_of: mid\n---\n<!--base-->/L2")
    body = prompts.canonical_prompt_text(_rv(), repo_root=str(tmp_path), variant="top")
    assert body == "L0/L1/L2"


def test_variant_cycle_is_guarded(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "base")
    _write(tmp_path, "ticket-quality.a.md", "---\nvariant_of: b\n---\n<!--base-->A")
    _write(tmp_path, "ticket-quality.b.md", "---\nvariant_of: a\n---\n<!--base-->B")
    with pytest.raises(PromptError, match="cycle"):
        prompts.canonical_prompt_text(_rv(), repo_root=str(tmp_path), variant="a")


def test_unknown_variant_raises(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "base")
    with pytest.raises(PromptError, match="unknown prompt variant"):
        prompts.canonical_prompt_text(_rv(), repo_root=str(tmp_path), variant="nope")


# ── JSON schema + parity ──────────────────────────────────────────────────────


def test_prompt_input_schema_explicit_required(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ticket-quality.md",
        "---\nvariables: [ticket_id, ticket_context]\nrequired: [ticket_id]\n---\n{{ticket_id}}",
    )
    schema = prompts.prompt_input_schema(_rv(), repo_root=str(tmp_path))
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"ticket_id", "ticket_context"}
    assert schema["required"] == ["ticket_id"]


def test_parity_flags_undeclared_used(tmp_path: Path) -> None:
    _write(
        tmp_path, "ticket-quality.md", "---\nvariables: [ticket_id]\n---\n{{ticket_id}} {{rogue}}"
    )
    errors = prompts.check_prompt_parity(_rv(), repo_root=str(tmp_path))
    assert any("rogue" in e and "not declared" in e for e in errors)


def test_parity_flags_declared_unused(tmp_path: Path) -> None:
    _write(tmp_path, "ticket-quality.md", "---\nvariables: [ticket_id, extra]\n---\n{{ticket_id}}")
    errors = prompts.check_prompt_parity(_rv(), repo_root=str(tmp_path))
    assert any("extra" in e and "unused" in e for e in errors)


def test_required_var_enforced_at_resolve(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "ticket-quality.md",
        "---\nvariables: [ticket_id]\nrequired: [ticket_id]\n---\nReview {{ticket_id}}",
    )
    with pytest.raises(PromptError, match="requires variable"):
        prompts.resolve_prompt(_rv(), {}, repo_root=str(tmp_path))


def test_undeclared_prompt_has_no_parity_findings() -> None:
    # The packaged reviewers have no front-matter → parity gate is a no-op for them.
    assert prompts.check_prompt_parity(_rv()) == []
