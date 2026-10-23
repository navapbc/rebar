"""Require OS sandboxing in both mutation-testing skills.

Seeded defects execute arbitrary code; a 2026-08-26 guard removal caused
``rm -rf /*`` to destroy Homebrew files. These tests require complete macOS
and Linux sandbox examples, a real ``bwrap`` capability probe, refusal to run
without a sandbox, and the incident's hostile-code rationale in both skills.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "examples" / "agent-skills"
SKILLS = ("rebar-debug", "rebar-implement")

# Require the complete macOS invocation, including its generated profile.
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
    # Bind scratch too; the rule permits worktree and scratch writes.
    '--bind "$SBX" "$SBX"',
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


@pytest.mark.parametrize(
    ("group_name", "literals", "victim"),
    [
        ("macOS invocation", MACOS_LITERALS, 'sandbox-exec -f "$SBX/p.sb"'),
        ("Linux invocation", LINUX_LITERALS, '--bind "$SBX" "$SBX"'),
        ("capability probe", CAPABILITY_LITERALS, CAPABILITY_LITERALS[0]),
        ("no-sandbox fallback", FALLBACK_LITERALS, FALLBACK_LITERALS[0]),
        ("hostile-code framing", HOSTILE_CODE_LITERALS, HOSTILE_CODE_LITERALS[0]),
    ],
)
@pytest.mark.parametrize("skill", SKILLS)
def test_rule_removal_is_detected(
    skill: str, group_name: str, literals: tuple[str, ...], victim: str
) -> None:
    """Prove each literal group detects removal in each skill.

    A group that still passes after its required text is stripped would leave
    that part of the sandbox rule unguarded.
    """
    original = _skill_text(skill)
    assert victim in original, f"{group_name}: victim literal absent from {skill}"
    stripped = original.replace(victim, "")
    assert stripped != original
    assert all(lit in original for lit in literals)
    assert not all(lit in stripped for lit in literals), (
        f"{group_name} in {skill} is not removal-sensitive: stripping {victim!r} left "
        "every literal present, so this gate would not notice the rule being lost"
    )
