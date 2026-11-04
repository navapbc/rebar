"""List the files a repository owns, by name pattern, for the tree-walking lint gates.

Inside a git work tree the candidate set is ``git ls-files --cached --others
--exclude-standard``: tracked files plus untracked files git does not ignore. Git's own ignore
rules therefore decide what the gates judge, whatever an ignored directory is called. A venv
or reproduction tree under the git-ignored ``.rebar/scratch`` once failed ``make lint`` because
the gates walked the whole tree and skipped only a hand-written directory-name list (bug
frouzy-downfallen-salamander, d552-a55e-2877-47ef). The same listing is the precedent in
``check_criteria_vocabulary.py::_git_tracked_relatives``.

Outside a git work tree, or when git cannot run, there are no ignore rules to consult, so the
helper walks the tree. Either way the caller's ``excluded_dirs`` still applies, which keeps
behaviour identical for the plain temp-directory trees the gates' own tests build.

Stdlib only and independent of any CI provider, so it runs the same through ``make lint``, the
pre-commit hook, or a bare shell.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path


def _git_listed(root: Path) -> list[Path] | None:
    """Repo-relative tracked and non-ignored untracked paths, or None outside usable git."""
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return [Path(os.fsdecode(entry)) for entry in completed.stdout.split(b"\0") if entry]


def _walked(root: Path, patterns: Iterable[str]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        found.update(path.relative_to(root) for path in root.rglob(pattern))
    return list(found)


def files_named(root: Path, patterns: Iterable[str], excluded_dirs: frozenset[str]) -> list[Path]:
    """Sorted absolute paths under ``root`` whose file name matches any ``fnmatch`` pattern.

    A path is dropped when any of its repo-relative parts is in ``excluded_dirs``, or when it
    is not a file (a matching directory, or a tracked file deleted from the work tree). Symlink
    policy stays with the caller, which keeps each gate's existing behaviour.
    """
    patterns = tuple(patterns)
    listed = _git_listed(root)
    relatives = _walked(root, patterns) if listed is None else listed
    return sorted(
        root / rel
        for rel in set(relatives)
        if any(fnmatchcase(rel.name, pattern) for pattern in patterns)
        and not excluded_dirs.intersection(rel.parts)
        and (root / rel).is_file()
    )
