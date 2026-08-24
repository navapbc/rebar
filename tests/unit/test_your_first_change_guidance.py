from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TUTORIAL = ROOT / "docs" / "your-first-change.md"


def _tutorial() -> str:
    return TUTORIAL.read_text(encoding="utf-8")


def test_tutorial_uses_supported_hook_installation() -> None:
    body = _tutorial()
    assert "make hooks" in body
    assert "curl -Lo .git/hooks/commit-msg" not in body
    assert "/tools/hooks/commit-msg" not in body


def test_every_change_to_main_passes_through_gerrit() -> None:
    body = _tutorial()
    assert "Every change to `main` passes through Gerrit." in body


def test_maintainer_sponsorship_does_not_bypass_gerrit() -> None:
    body = _tutorial()
    assert "maintainer may sponsor" in body
    assert "still passes through Gerrit" in body
    assert "skips Gerrit entirely" not in body
    assert "shepherd the patch in directly" not in body


def test_recheck_requires_a_demonstrated_environmental_fault() -> None:
    body = _tutorial()
    assert "demonstrated environmental fault" in body
    assert "if it looks like a flake" not in body
    assert "Likely a CI flake" not in body


def test_nondeterministic_failures_use_the_debugging_workflow() -> None:
    body = _tutorial()
    assert "/rebar-debug" in body
    assert "../examples/agent-skills/rebar-debug/SKILL.md" in body
    assert "root cause" in body
