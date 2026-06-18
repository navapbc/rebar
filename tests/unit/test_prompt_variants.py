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


# ── prior-art hardening (epic a88f follow-up, ticket crude-hook-stomp) ─────────


def test_front_matter_survives_crlf_and_bom() -> None:
    # A Windows checkout / core.autocrlf must not defeat the \n-anchored fence.
    meta, body = prompts.parse_front_matter(
        "﻿---\r\nvariables: [x]\r\nrequired: [x]\r\n---\r\nBODY {{x}}\r\n"
    )
    assert meta == {"variables": ["x"], "required": ["x"]}
    assert "\r" not in body and body.startswith("BODY {{x}}")


def test_declared_optional_var_defaults_to_empty(monkeypatch) -> None:
    # A declared-but-not-required var that IS used must default to empty when
    # omitted (matching prompt_input_schema marking it optional) — not raise.
    rv = prompts.Reviewer(id="demo", dimension="d", fallback_file="demo.md")
    monkeypatch.setattr(
        prompts,
        "_prompt_file",
        lambda reviewer, repo_root, variant: (
            "---\nvariables: [a, b]\nrequired: [a]\n---\nHELLO {{a}} {{b}}"
            if variant is None
            else None
        ),
    )
    compiled, _ = prompts.resolve_prompt(rv, {"a": "X"})  # b omitted
    assert compiled == "HELLO X "
    # required var still enforced
    with pytest.raises(PromptError):
        prompts.resolve_prompt(rv, {})

    # …and the schema agrees: a required, b optional.
    schema = prompts.prompt_input_schema(rv)
    assert schema["required"] == ["a"]
    assert set(schema["properties"]) == {"a", "b"}


def test_variant_overlay_unions_base_declarations(monkeypatch) -> None:
    # A variant that splices the base (<!--base-->) must NOT drop the base's
    # declared/required vars via a shallow overlay.
    rv = prompts.Reviewer(id="demo", dimension="d", fallback_file="demo.md")

    def fake(reviewer, repo_root, variant):
        if variant is None:
            return (
                "---\nvariables: [ticket, diff]\nrequired: [ticket]\n---\n"
                "BASE {{ticket}} {{diff}}"
            )
        if variant == "friendly":
            return "---\nvariables: [tone]\n---\nTONE {{tone}}\n<!--base-->"
        return None

    monkeypatch.setattr(prompts, "_prompt_file", fake)
    body, meta = prompts.load_prompt(rv, variant="friendly")
    assert set(meta["variables"]) == {"ticket", "diff", "tone"}
    assert meta["required"] == ["ticket"]  # base requirement preserved
    assert "BASE" in body and "TONE" in body
    assert prompts.check_prompt_parity(rv, variant="friendly") == []


def test_full_override_variant_keeps_only_its_own_vars(monkeypatch) -> None:
    rv = prompts.Reviewer(id="demo", dimension="d", fallback_file="demo.md")

    def fake(reviewer, repo_root, variant):
        if variant is None:
            return "---\nvariables: [ticket, diff]\n---\nBASE {{ticket}} {{diff}}"
        if variant == "full":
            return "---\nvariables: [tone]\n---\nFULL {{tone}}"  # no <!--base-->
        return None

    monkeypatch.setattr(prompts, "_prompt_file", fake)
    _, meta = prompts.load_prompt(rv, variant="full")
    assert meta["variables"] == ["tone"]
