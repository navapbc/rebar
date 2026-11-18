#!/usr/bin/env python3
"""Run release.yml's build-and-check path locally (bug expansive-aromatic-whippet).

A bare `python -m build && python -m twine check dist/*` does not predict a release. The
isolated build resolves pyproject's open `hatchling>=1.27` to the newest hatchling, which
writes Metadata-Version 2.5, and the release-pinned twine (6.2.0) rejects 2.5. Its artifact
also differs from the one the release publishes. The release instead builds with the
hash-locked toolchain from `.github/release-requirements.txt` and `--no-isolation`.

This script repeats the `build` job of `.github/workflows/release.yml` in a fresh
temporary venv: hash-locked install, the ambient-package guard, the `--no-isolation` build,
and `twine check`. It stops at the first failing step and names it.

Run: `make release-build-check` (or `python scripts/release_build_check.py`). Needs network
access for the pinned `pip install`. Exit 0 when every step passes.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import IO

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / ".github" / "release-requirements.txt"
GUARD = ROOT / "scripts" / "check_build_env_locked.py"

# (step label, argv, file that receives stdout or None)
Step = tuple[str, list[str], Path | None]
Runner = Callable[[Sequence[str], Path | None], int]


def release_build_steps(python: str, workdir: Path) -> list[Step]:
    """The release `build` job's commands, in order, for the venv interpreter `python`."""
    freeze = workdir / "freeze.txt"
    dist = workdir / "dist"
    return [
        (
            "install the hash-locked toolchain",
            [python, "-m", "pip", "install", "--require-hashes", "--no-deps", "-r", str(LOCK)],
            None,
        ),
        ("capture pip freeze", [python, "-m", "pip", "freeze"], freeze),
        (
            "ambient-package guard",
            [python, str(GUARD), "--lock", str(LOCK), "--freeze", str(freeze)],
            None,
        ),
        (
            "build sdist + wheel (--no-isolation)",
            [python, "-m", "build", "--no-isolation", "--outdir", str(dist), str(ROOT)],
            None,
        ),
        ("twine check", [python, "-m", "twine", "check", str(dist / "*")], None),
    ]


def _run(argv: Sequence[str], stdout_path: Path | None) -> int:
    """Run one step; expand the trailing dist glob for twine (no shell is involved)."""
    args = list(argv)
    if args[-1].endswith("*"):
        pattern = Path(args[-1])
        args = args[:-1] + sorted(str(p) for p in pattern.parent.glob(pattern.name))
    if stdout_path is None:
        return _call(args, None)
    with stdout_path.open("w", encoding="utf-8") as out:
        return _call(args, out)


def _call(args: list[str], stdout: IO[str] | None) -> int:
    """Run one argv. These are pip/build/twine and the env guard in a temporary venv."""
    proc = subprocess.run(args, stdout=stdout, check=False)  # raw-git-ok: pip/build/twine only
    return proc.returncode


class _VenvBuilder(venv.EnvBuilder):
    """Records the created venv's interpreter path, whatever the OS layout."""

    env_exe = ""

    def post_setup(self, context: SimpleNamespace) -> None:
        self.env_exe = context.env_exe


def _make_venv(path: Path) -> str:
    """Create a fresh venv with pip and return its interpreter path."""
    builder = _VenvBuilder(with_pip=True)
    builder.create(path)
    return builder.env_exe


def run_steps(steps: list[Step], runner: Runner = _run) -> int:
    """Run steps in order; stop at the first failure and name it."""
    for label, argv, stdout_path in steps:
        print(f"release-build-check: {label}", flush=True)
        code = runner(argv, stdout_path)
        if code != 0:
            print(f"release-build-check: FAILED at step '{label}' (exit {code})", file=sys.stderr)
            return code or 1
    return 0


def main(runner: Runner = _run, make_venv: Callable[[Path], str] = _make_venv) -> int:
    with tempfile.TemporaryDirectory(prefix="rebar-release-build-") as tmp:
        workdir = Path(tmp)
        python = make_venv(workdir / "venv")
        code = run_steps(release_build_steps(python, workdir), runner)
        if code == 0:
            print("release-build-check: OK — the release build and twine check passed.")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
