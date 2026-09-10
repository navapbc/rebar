#!/usr/bin/env python3
"""Run the required ShellCheck gate over standalone ``*.sh`` files.

Actionlint already checks workflow ``run:`` blocks; this covers repository shell scripts.

The severity floor is ``warning`` because destructive-glob check SC2115 is a warning::

    rm -rf "${dir}"/*
    #  warning: Use "${var:?}" to ensure this never expands to /* . [SC2115]

Using ``-S error`` would miss the exact unsafe expansion this gate must reject.

``shellcheck-py`` is pinned in the ``dev`` extra and installed by ``make install``. A missing
binary fails the gate; it never degrades to a skip.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Severity floor. See the module docstring — SC2115 is a `warning`, so raising
#: this to `error` would silence the very finding this gate exists to catch.
SEVERITY = "warning"

#: Directories never linted: virtualenvs, git internals, agent scratch, the
#: ticket store, and nested worktrees. These hold vendored or generated shell we
#: neither own nor can fix.
EXCLUDED_DIRS = frozenset(
    {
        ".venv",
        ".git",
        ".claude",
        ".tickets-tracker",
        ".tickets-hotpath-authoritative",
        "node_modules",
    }
)

INSTALL_HINT = (
    "shellcheck not found on PATH. It is pinned as `shellcheck-py` in pyproject's "
    "[dev] extra — run `make install` inside the worktree venv, or activate it "
    "with `source .venv/bin/activate`."
)


def discover(root: Path) -> list[Path]:
    """Return repo-relative paths of every lintable ``*.sh`` file, sorted."""
    found: list[Path] = []
    for path in root.rglob("*.sh"):
        rel = path.relative_to(root)
        if EXCLUDED_DIRS.intersection(rel.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        found.append(rel)
    return sorted(found)


def run_shellcheck(root: Path, files: list[Path], binary: str) -> tuple[int, str]:
    """Run shellcheck over ``files``; return (returncode, combined output)."""
    if not files:
        return 0, ""
    # Fixed argv; every path comes from discover() walking the repo tree.
    proc = subprocess.run(
        [binary, "-S", SEVERITY, "-f", "gcc", *[str(f) for f in files]],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to scan")
    args = parser.parse_args(argv)
    root = Path(args.root)

    binary = shutil.which("shellcheck")
    if binary is None:
        print(f"check_shellcheck: {INSTALL_HINT}", file=sys.stderr)
        return 1

    files = discover(root)
    code, output = run_shellcheck(root, files, binary)
    if code != 0:
        if output:
            print(output, file=sys.stderr)
        print(
            f"check_shellcheck: findings at severity>={SEVERITY} across "
            f"{len(files)} shell script(s). SC2115 in particular means an "
            'unguarded `rm -rf "$var"/*` — use `: "${var:?}"` plus an explicit '
            'deny-list, and prefer `cd -- "$dir" && rm -rf -- ./*`.',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
