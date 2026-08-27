"""Regression gate: both TDD skills require an OS sandbox around mutation runs.

`rebar-debug` and `rebar-implement` instruct agents to perturb code with a
defect-seeded mutation and then execute the tests.  A seeded defect is arbitrary
code, so that step *executes hostile code*: on 2026-08-26 an ad-hoc mutation
script stripped a safety guard from a shell script that performs real deletion
and the resulting `rm -rf /*` destroyed `/opt/homebrew` and every
Homebrew-installed app in `/Applications`.

Ticket f321-cb86-6863-44c7 added the sandbox requirement inline to both skills
(inline, so it does not wait on any wrapper script landing).  This module fails
if either skill loses any part of it: the complete macOS `sandbox-exec` snippet
including the heredoc that writes the profile, the complete Linux `bwrap` argv,
the `bwrap` capability probe plus the statement that presence on PATH is not
capability, the no-sandbox injected-seam fallback, and the hostile-code framing
with the 2026-08-26 outcome as its reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "examples" / "agent-skills"
SKILLS = ("rebar-debug", "rebar-implement")

# The complete macOS invocation: the heredoc writes the profile, so nothing
# external is referenced.  Every line is load-bearing — a skill that ships only
# `sandbox-exec -f some-profile.sb` leaves the profile's provenance unstated.
MACOS_LITERALS = (
    'SBX="$(mktemp -d)"',
    'cat > "$SBX/p.sb" <<EOF',
    "(version 1)",
    "(allow default)",
    "(deny file-write*)",
    "(allow file-write*",
    '(subpath "$PWD")',
    '(subpath "$SBX")',
    '(literal "/dev/null")',
    'sandbox-exec -f "$SBX/p.sb"',
)

# The complete Linux invocation — an abbreviated fragment does not confine.
LINUX_LITERALS = (
    "bwrap",
    "--ro-bind / /",
    "--dev /dev",
    "--proc /proc",
    '--bind "$PWD" "$PWD"',
)

# Presence on PATH is not capability (Ubuntu 23.10+ AppArmor restricts
# unprivileged user namespaces); both failure strings were observed.
CAPABILITY_LITERALS = (
    "bwrap --ro-bind / / -- /bin/true",
    "Creating new namespace failed",
    "setting up uid map: Permission denied",
)

# The fallback when no sandbox is available.
FALLBACK_LITERALS = (
    "do not proceed unsandboxed",
    "injected seam",
    "records its argv",
)

# A seeded defect is arbitrary code, and the 2026-08-26 outcome is the reason.
HOSTILE_CODE_LITERALS = (
    "arbitrary code",
    "hostile code",
    "2026-08-26",
    "rm -rf /*",
    "/opt/homebrew",
)


def _skill_text(skill: str) -> str:
    path = SKILLS_DIR / skill / "SKILL.md"
    assert path.is_file(), f"skill missing: {path}"
    return path.read_text(encoding="utf-8")


def _assert_all_present(skill: str, literals: tuple[str, ...], what: str) -> None:
    text = _skill_text(skill)
    missing = [lit for lit in literals if lit not in text]
    assert not missing, (
        f"{skill}/SKILL.md lost {what}: missing {missing!r}. The mutation step "
        "executes a seeded defect — arbitrary code — so the sandbox requirement "
        "and its complete invocations must stay in the skill (ticket "
        "f321-cb86-6863-44c7)."
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_requires_a_sandbox_for_the_mutation_step(skill: str) -> None:
    """The rule itself: perturb-then-execute runs inside an OS sandbox."""
    text = _skill_text(skill)
    assert "OS sandbox" in text, f"{skill}/SKILL.md no longer requires an OS sandbox"
    assert "Sandbox the mutation run" in text, (
        f"{skill}/SKILL.md lost the 'Sandbox the mutation run' requirement heading"
    )
    assert "denies writes outside the worktree" in text, (
        f"{skill}/SKILL.md no longer states what the sandbox must deny"
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_carries_complete_macos_invocation(skill: str) -> None:
    _assert_all_present(skill, MACOS_LITERALS, "the complete macOS sandbox-exec invocation")


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_carries_complete_linux_invocation(skill: str) -> None:
    _assert_all_present(skill, LINUX_LITERALS, "the complete Linux bwrap invocation")


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_carries_bwrap_capability_probe(skill: str) -> None:
    _assert_all_present(skill, CAPABILITY_LITERALS, "the bwrap capability probe")
    text = _skill_text(skill)
    assert "NOT** capability" in text or "not** capability" in text, (
        f"{skill}/SKILL.md no longer says bwrap on PATH is not capability"
    )


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_carries_no_sandbox_fallback(skill: str) -> None:
    _assert_all_present(skill, FALLBACK_LITERALS, "the no-sandbox injected-seam fallback")


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_frames_the_mutation_as_hostile_code(skill: str) -> None:
    _assert_all_present(skill, HOSTILE_CODE_LITERALS, "the hostile-code framing")


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_does_not_weaken_the_mutation_discipline(skill: str) -> None:
    """The sandbox is added around the existing discipline, never in place of it."""
    text = _skill_text(skill)
    assert "tautology" in text, (
        f"{skill}/SKILL.md lost the mutation-teeth rationale (a test that stays "
        "green under mutation is a tautology)"
    )
    assert "held-out" in text, f"{skill}/SKILL.md lost the held-out-oracle discipline"


def test_rule_removal_is_detected() -> None:
    """The literal check this gate relies on is sensitive to the rule's removal.

    Guards the gate itself: a check written against text so generic that a
    stripped skill still passes would be no gate at all.
    """
    original = _skill_text("rebar-debug")
    stripped = original.replace('sandbox-exec -f "$SBX/p.sb"', "")
    assert stripped != original
    assert all(lit in original for lit in MACOS_LITERALS)
    assert not all(lit in stripped for lit in MACOS_LITERALS)
