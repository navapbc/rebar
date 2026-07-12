#!/usr/bin/env python3
"""README-quickstart golden-path extractor + runner (ticket 0435).

Extract the ``bash`` fenced code block under the README's ``## Quickstart``
heading and EXECUTE those exact lines end-to-end in a throwaway git repo, so a
wrong command *printed* in the README fails CI.

API:
  - extract_quickstart_bash(readme_text: str) -> str
  - run_quickstart(block: str) -> int
  - main(argv) -> int
  - DEFAULT_README: Path

Usage:
  python scripts/check_readme_quickstart.py            # run against README.md
  python scripts/check_readme_quickstart.py --readme P # run against P
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = REPO_ROOT / "README.md"

# ``## Quickstart`` heading, then the FIRST ```bash fenced block after it. The
# heading match stops at the next ``## `` heading so we never reach into a later
# section, and the language guard (``bash``) skips the adjacent python/json
# blocks.
_QUICKSTART_HEADING = re.compile(r"^##\s+Quickstart\s*$", re.MULTILINE)
_BASH_FENCE = re.compile(r"```bash[^\n]*\n(.*?)```", re.DOTALL)
_NEXT_HEADING = re.compile(r"^##\s+\S", re.MULTILINE)


def extract_quickstart_bash(readme_text: str) -> str:
    """Return the contents of the first ```bash block under ``## Quickstart``.

    Raises ``ValueError`` if there is no ``## Quickstart`` heading or no bash
    block within that section (before the next ``## `` heading).
    """
    heading = _QUICKSTART_HEADING.search(readme_text)
    if heading is None:
        raise ValueError("no '## Quickstart' heading found in README")

    section_start = heading.end()
    next_heading = _NEXT_HEADING.search(readme_text, section_start)
    section_end = next_heading.start() if next_heading else len(readme_text)
    section = readme_text[section_start:section_end]

    block = _BASH_FENCE.search(section)
    if block is None:
        raise ValueError("no ```bash block found under '## Quickstart'")
    return block.group(1)


def run_quickstart(block: str) -> int:
    """Execute ``block`` end-to-end in an isolated throwaway git repo.

    Returns the subprocess exit code (0 = success). The block is run under
    ``bash -euo pipefail`` so any failing command yields a non-zero exit.
    """
    workdir = tempfile.mkdtemp(prefix="rebar-quickstart-")
    try:
        # rebar needs a git repo; init it first with a local identity.
        subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
        subprocess.run(
            ["git", "config", "user.email", "quickstart@example.com"],
            cwd=workdir,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Quickstart Runner"],
            cwd=workdir,
            check=True,
        )

        # Clean env: keep PATH (so the venv's `rebar` resolves) but drop any
        # REBAR_* / gate vars so a fresh `rebar init` uses default config with
        # gates OFF, and point REBAR_ROOT at a temp path inside the workdir.
        env = {k: v for k, v in os.environ.items() if not k.startswith("REBAR_")}
        env["REBAR_ROOT"] = os.path.join(workdir, ".rebar-store")

        proc = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", block],
            cwd=workdir,
            env=env,
        )
        return proc.returncode
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract and run the README '## Quickstart' bash block."
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="path to the README (default: repo README.md)",
    )
    args = parser.parse_args(argv)

    readme = args.readme if args.readme is not None else DEFAULT_README
    block = extract_quickstart_bash(Path(readme).read_text(encoding="utf-8"))
    code = run_quickstart(block)
    if code == 0:
        print(f"PASS: README quickstart ({readme}) ran green")  # noqa: T201
    else:
        print(f"FAIL: README quickstart ({readme}) exited {code}")  # noqa: T201
    return code


if __name__ == "__main__":
    raise SystemExit(main())
