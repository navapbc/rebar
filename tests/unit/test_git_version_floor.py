"""The declared Git version floor is real, single-sourced, and unskippable.

rebar's two-clone convergence regressions (added for bug 8185-2d4b-d2bf-4282 and its
sidecar sibling 55bc-b6bf-7adc-4108) merge two independently written tracker histories
with ``git merge-tree --write-tree`` — a mode Git gained in 2.38. The project answers
that dependency by DECLARING and ENFORCING a floor, never by skipping the affected
tests on older clients: a regression that quietly does not run reads as coverage while
providing none, which is exactly how the guards in 34c2 and 8a5e-b88e-0c3e-4544 went
vacuous. Ticket 980d-83ac-a6bb-4edb.

These tests pin three things:

1. the floor is single-sourced in ``.github/git-version-floor.txt`` and every consumer
   (contributor docs, CI, the suite's own preflight) agrees with it;
2. the preflight FAILS — with a diagnostic naming the required version and the fix —
   rather than skipping, and the running Git really does satisfy it;
3. no merge-tree regression anywhere under ``tests/`` is guarded by skip machinery.
   Check (3) is deliberately fail-closed: it locates the regressions by searching for
   the capability they use rather than by a pinned file path, so a module split or a
   rename makes it fail loudly instead of silently scanning nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _git_floor

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"


# ── the floor is single-sourced and parseable ────────────────────────────────


def test_floor_file_declares_a_parseable_version() -> None:
    assert _git_floor.FLOOR_FILE.is_file(), f"missing floor file: {_git_floor.FLOOR_FILE}"
    assert _git_floor.read_floor() == (2, 38)


def test_floor_file_read_rejects_a_malformed_declaration(tmp_path: Path) -> None:
    bad = tmp_path / "git-version-floor.txt"
    bad.write_text("two point thirty-eight\n", encoding="utf-8")
    with pytest.raises(ValueError, match="git-version-floor"):
        _git_floor.read_floor(bad)


# ── version parsing (the shapes real `git --version` output takes) ───────────


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("git version 2.38.0", (2, 38)),
        ("git version 2.39.5 (Apple Git-154)", (2, 39)),
        ("git version 2.55.0\n", (2, 55)),
        ("git version 3.0.0", (3, 0)),
        ("git version 2.30.2 (Debian 1:2.30.2-1+deb11u2)", (2, 30)),
        ("git version 2.9", (2, 9)),
    ],
)
def test_parse_git_version(output: str, expected: tuple[int, int]) -> None:
    assert _git_floor.parse_git_version(output) == expected


@pytest.mark.parametrize("output", ["", "not a version", "git version x.y", "2.38.0"])
def test_parse_git_version_rejects_unrecognized_output(output: str) -> None:
    with pytest.raises(ValueError, match="git --version"):
        _git_floor.parse_git_version(output)


# ── enforcement: FAIL with a clear diagnostic, never skip ────────────────────


@pytest.mark.parametrize("installed", [(2, 37), (2, 9), (1, 9)])
def test_below_floor_is_reported_with_an_actionable_diagnostic(
    installed: tuple[int, int],
) -> None:
    message = _git_floor.floor_violation(installed=installed, floor=(2, 38))
    assert message is not None, "an under-floor git must be reported, not tolerated"
    assert "2.38" in message, "the diagnostic must name the REQUIRED version"
    assert ".".join(str(part) for part in installed) in message, (
        "the diagnostic must name the version actually installed"
    )
    assert "merge-tree" in message, "the diagnostic must name the capability that needs it"
    assert "upgrade" in message.lower(), "the diagnostic must say what to do about it"


@pytest.mark.parametrize("installed", [(2, 38), (2, 39), (2, 55), (3, 0)])
def test_at_or_above_floor_is_accepted(installed: tuple[int, int]) -> None:
    assert _git_floor.floor_violation(installed=installed, floor=(2, 38)) is None


def test_the_running_git_satisfies_the_declared_floor() -> None:
    """The floor is enforced, so reaching this line already proves it — assert anyway,
    so the check is named in the report rather than only implied by the run existing."""
    assert _git_floor.floor_violation() is None


def test_the_running_git_really_performs_merge_tree_write_tree(tmp_path: Path) -> None:
    """A real capability probe, not a version-string inference: build two divergent
    histories and merge them without a worktree, exactly as the 8185 regression does."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "probe@example.invalid")
    git("config", "user.name", "floor probe")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "side")
    (tmp_path / "side.txt").write_text("side\n", encoding="utf-8")
    git("add", "side.txt")
    git("commit", "-q", "-m", "side")
    git("checkout", "-q", "main")
    (tmp_path / "main.txt").write_text("main\n", encoding="utf-8")
    git("add", "main.txt")
    git("commit", "-q", "-m", "main")

    merged = git("merge-tree", "--write-tree", "main", "side")
    tree = merged.stdout.splitlines()[0]
    listing = git("ls-tree", "-r", "--name-only", tree).stdout.split()
    assert sorted(listing) == ["base.txt", "main.txt", "side.txt"]


def test_the_suite_refuses_to_run_on_an_under_floor_git(tmp_path: Path) -> None:
    """End-to-end proof of AC2: with an under-floor ``git`` on PATH the suite FAILS at
    startup with the diagnostic — it does not skip, and it does not run degraded.

    A shim ahead of the real git reports 2.37.0 for ``--version`` and delegates
    everything else, so the only thing that changes is the reported version.
    """
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH"
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "git version 2.37.0"; exit 0; fi\n'
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    env = subprocess_env({"PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"})
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_git_version_floor.py",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, f"an under-floor git must fail the run:\n{output}"
    assert "2.38" in output and "2.37" in output, output
    assert "merge-tree" in output, output
    assert "skip" not in output.lower().replace("skipping", ""), (
        f"the floor must FAIL, never skip:\n{output}"
    )


# ── the regressions that need the floor are never skipped ────────────────────

_SKIP_MACHINERY = re.compile(
    r"pytest\.skip\(|pytest\.importorskip|mark\.skip|mark\.xfail|pytest\.xfail\("
)


def _merge_tree_test_files() -> list[Path]:
    return sorted(
        path
        for path in TESTS_DIR.rglob("test_*.py")
        if "merge-tree" in path.read_text(encoding="utf-8")
    )


def test_the_merge_tree_regressions_are_still_present() -> None:
    """Fail-closed: if the regressions are renamed, moved, or deleted, this test goes
    RED rather than silently scanning an empty set (the 34c2 vacuity mode)."""
    files = _merge_tree_test_files()
    assert files, (
        "no test under tests/ uses `git merge-tree` any more — the two-clone union "
        "regressions for 8185-2d4b-d2bf-4282 must not be removed or weakened; if they "
        "moved, this guard needs no change (it searches the whole tree), so their "
        "absence means the coverage itself is gone"
    )
    bodies = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "--write-tree" in bodies, (
        "the merge-tree regressions no longer use `--write-tree`, the Git 2.38 mode "
        "this floor exists for"
    )


def test_no_merge_tree_regression_is_guarded_by_skip_machinery() -> None:
    """The operator decision on 980d-83ac-a6bb-4edb: declare and enforce the floor,
    never skip. A skipped regression reads as coverage while providing none."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _merge_tree_test_files()
        if _SKIP_MACHINERY.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "skip/xfail machinery appeared in a module holding a merge-tree regression: "
        f"{offenders}. The Git floor is DECLARED and ENFORCED (see "
        f"{_git_floor.FLOOR_FILE.name}); these regressions must run everywhere the "
        "suite runs."
    )


# ── every consumer agrees with the single source ─────────────────────────────


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "CONTRIBUTING.md",
        "docs/local-dev-env.md",
        ".github/workflows/_build-and-test.yml",
    ],
)
def test_consumers_declare_the_same_floor(relative_path: str) -> None:
    major, minor = _git_floor.read_floor()
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert f"{major}.{minor}" in text, (
        f"{relative_path} does not name the declared Git floor {major}.{minor}"
    )


def test_ci_reads_the_floor_from_the_single_source() -> None:
    """CI must derive the floor from the file, not hardcode a second copy that drifts."""
    workflow = (REPO_ROOT / ".github/workflows/_build-and-test.yml").read_text(encoding="utf-8")
    assert _git_floor.FLOOR_FILE.name in workflow, (
        "the CI git-floor gate must read .github/git-version-floor.txt"
    )
