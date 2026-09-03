"""Policy-documentation checks for ADR 0111's private-shim rule."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR = _REPO_ROOT / "docs" / "adr" / "0111-no-internal-only-compatibility-shims.md"
_API_STABILITY = _REPO_ROOT / "docs" / "api-stability.md"
_ARCHITECTURE = _REPO_ROOT / "docs" / "architecture.md"
_MARKER = _REPO_ROOT / "docs" / "adr" / ".numbers" / "0111"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _contains_all(text: str, *terms: str) -> bool:
    folded = _one_line(text).lower()
    return all(term.lower() in folded for term in terms)


def test_adr_0111_records_private_move_rule_and_exceptions() -> None:
    text = _read(_ADR)

    assert _contains_all(text, "one", "canonical binding")
    for migrated_surface in (
        "source imports",
        "tests",
        "string lookups",
        "dynamic imports",
        "module-qualified monkeypatch",
    ):
        assert migrated_surface in text

    for exception in (
        "public Python facades",
        "MCP tool names and MCP input/output schemas",
        "event readers",
        "persisted-data migrations",
    ):
        assert exception in text


def test_adr_0111_critiques_prior_decisions_and_records_resolutions() -> None:
    text = _read(_ADR)
    one_line = _one_line(text)

    for subject in ("ADR 0016", "ADR 0083", "ADR 0092", "Prior architecture split notes"):
        assert subject in text
    assert text.count("**Resolution:**") == 4
    assert "pre-policy exception" in one_line
    assert "public compatibility-contract exception" in one_line


def test_api_stability_aligns_private_names_with_adr_0111() -> None:
    text = _read(_API_STABILITY)

    assert "ADR 0111" in text
    assert _contains_all(text, "private names", "private-to-private", "compatibility promise")
    assert _contains_all(text, "source imports", "tests", "string lookups", "dynamic imports")
    assert _contains_all(text, "public", "operator")


def test_architecture_subordinates_historical_internal_shims_to_adr_0111() -> None:
    text = _read(_ARCHITECTURE)

    assert "ADR 0111" in text
    assert _contains_all(text, "current policy", "internal-only", "compatibility shims")
    assert _contains_all(text, "old private path")
    assert _contains_all(text, "split notes", "historical", "implementation records")


def test_adr_0111_marker_names_the_adr_file() -> None:
    assert _MARKER.read_text(encoding="utf-8") == "0111-no-internal-only-compatibility-shims.md\n"
