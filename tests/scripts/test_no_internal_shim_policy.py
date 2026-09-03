from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR = REPO_ROOT / "docs" / "adr" / "0111-no-internal-only-compatibility-shims.md"


def test_no_internal_shim_adr_is_allocated_and_complete() -> None:
    text = ADR.read_text(encoding="utf-8")
    marker = REPO_ROOT / "docs" / "adr" / ".numbers" / "0111"

    assert marker.read_text(encoding="utf-8").strip() == ADR.name
    assert "one canonical binding" in text
    assert "atomic" in text
    assert "source, test, string, dynamic import, and monkeypatch" in text
    assert "Public facades" in text
    assert "MCP wire schemas" in text
    assert "event readers" in text
    assert "persisted-data migrations" in text
    assert "recurrence prevention" in text


def test_private_compatibility_guidance_points_to_the_adr_policy() -> None:
    api_text = (REPO_ROOT / "docs" / "api-stability.md").read_text(encoding="utf-8")
    architecture_text = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "Internal-only compatibility shims" in api_text
    assert "0111-no-internal-only-compatibility-shims.md" in api_text
    assert "Internal-only compatibility shims" in architecture_text
    assert "0111-no-internal-only-compatibility-shims.md" in architecture_text
