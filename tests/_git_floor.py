"""rebar's declared Git version floor, and the primitives that enforce it.

The two-clone convergence regressions merge two independently written tracker histories
with ``git merge-tree --write-tree`` (bug 8185-2d4b-d2bf-4282 and its sidecar sibling
55bc-b6bf-7adc-4108). That mode arrived in Git 2.38, so the suite has a hard Git
prerequisite.

rebar answers it by **declaring and enforcing a floor**, not by skipping the affected
tests on older clients. A skip guard would mean the regression silently does not run for
some contributors — coverage in the report, none in fact, which is precisely how the
guards in 34c2 and 8a5e-b88e-0c3e-4544 rotted (ticket 980d-83ac-a6bb-4edb). So an
under-floor Git fails the run, loudly, with the required version and the remedy.

The floor value itself lives in ``.github/git-version-floor.txt`` — one source read by
this module, by the contributor docs, and by the CI gate, so the three cannot drift.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOOR_FILE = REPO_ROOT / ".github" / "git-version-floor.txt"

_FLOOR_RE = re.compile(r"^(\d+)\.(\d+)$")
_VERSION_RE = re.compile(r"^git version (\d+)\.(\d+)")


def read_floor(path: Path | None = None) -> tuple[int, int]:
    """The declared ``(major, minor)`` floor, from the single-source file."""
    floor_file = FLOOR_FILE if path is None else path
    raw = floor_file.read_text(encoding="utf-8").strip()
    match = _FLOOR_RE.match(raw)
    if match is None:
        raise ValueError(
            f"git-version-floor: {floor_file} must contain a bare MAJOR.MINOR version "
            f"(e.g. '2.38'), got {raw!r}"
        )
    return int(match.group(1)), int(match.group(2))


def parse_git_version(output: str) -> tuple[int, int]:
    """The ``(major, minor)`` in ``git --version`` output.

    Real clients append build metadata — ``git version 2.39.5 (Apple Git-154)`` — so only
    the leading two components are read.
    """
    match = _VERSION_RE.match(output.strip())
    if match is None:
        raise ValueError(f"could not parse `git --version` output: {output.strip()!r}")
    return int(match.group(1)), int(match.group(2))


def installed_git_version() -> tuple[int, int]:
    """The ``(major, minor)`` of the ``git`` on PATH."""
    completed = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    return parse_git_version(completed.stdout)


def floor_violation(
    *, installed: tuple[int, int] | None = None, floor: tuple[int, int] | None = None
) -> str | None:
    """``None`` when the installed Git meets the floor, else the failure diagnostic."""
    required = read_floor() if floor is None else floor
    have = installed_git_version() if installed is None else installed
    if have >= required:
        return None
    required_text = f"{required[0]}.{required[1]}"
    have_text = f"{have[0]}.{have[1]}"
    return (
        f"rebar requires Git >= {required_text}; this environment has Git {have_text}.\n"
        f"The test suite's two-clone convergence regressions merge divergent tracker "
        f"histories with `git merge-tree --write-tree`, which Git gained in "
        f"{required_text}. rebar declares that floor and fails on older clients rather "
        f"than skipping those regressions, because a regression that quietly does not "
        f"run reads as coverage while providing none.\n"
        f"Fix: upgrade Git to {required_text} or newer (macOS: `brew install git`; "
        f"Debian/Ubuntu: the git-core PPA or a backports build), then re-run. The floor "
        f"is declared in {FLOOR_FILE.relative_to(REPO_ROOT)}."
    )
