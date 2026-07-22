"""Guard the Codex-specific Gerrit fallback in the canonical agent guide."""

from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "AGENTS.md"


def test_codex_guidance_has_credential_aware_gerrit_fallback() -> None:
    text = AGENTS.read_text(encoding="utf-8")
    start = text.index("**Codex Gerrit workflow rule:")
    end = text.index("\n\n## Record your work", start)
    guidance = " ".join(text[start:end].split())

    assert "recent merged Gerrit history" in guidance
    assert "feature-branch-driver" in guidance
    assert "git push gerrit HEAD:refs/for/main" in guidance
    assert "dependency order" in guidance
    assert "LLM-Review +1" in guidance
    assert "Verified +1" in guidance
    assert "ADR 0025" in guidance
    assert "not an implementation blocker" in guidance
    assert "git credential fill" in guidance
    assert "curl --netrc" in guidance
    assert ".netrc" in guidance
    assert "401" in guidance
    assert "does not invalidate" in guidance
    assert "never echo or log" in guidance.lower()
