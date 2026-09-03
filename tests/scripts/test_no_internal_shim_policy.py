from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR = REPO_ROOT / "docs" / "adr" / "0111-no-internal-only-compatibility-shims.md"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _contains_all(text: str, *terms: str) -> bool:
    folded = _one_line(text).lower()
    return all(term.lower() in folded for term in terms)


def test_no_internal_shim_adr_is_allocated_and_complete() -> None:
    text = ADR.read_text(encoding="utf-8")
    marker = REPO_ROOT / "docs" / "adr" / ".numbers" / "0111"

    assert marker.read_text(encoding="utf-8").strip() == ADR.name
    assert _contains_all(text, "canonical binding", "private", "old private binding")
    assert _contains_all(text, "atomic", "source", "test", "dynamic import", "monkeypatch")
    assert _contains_all(text, "public", "facades")
    assert _contains_all(text, "MCP", "schemas")
    assert _contains_all(text, "event readers")
    assert _contains_all(text, "persisted-data migrations")
    assert _contains_all(text, "recurrence", "prevention")


def test_private_compatibility_guidance_points_to_the_adr_policy() -> None:
    api_text = (REPO_ROOT / "docs" / "api-stability.md").read_text(encoding="utf-8")
    architecture_text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert _contains_all(api_text, "internal-only", "compatibility shims")
    assert "0111-no-internal-only-compatibility-shims.md" in api_text
    assert _contains_all(architecture_text, "internal-only", "compatibility shims")
    assert "0111-no-internal-only-compatibility-shims.md" in architecture_text
