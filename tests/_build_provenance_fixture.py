"""Shared minimal real-Git package for build-provenance tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "rebar-build-provenance-fixture"
version = "0.0.0"

[tool.hatch.build.hooks.custom]

[tool.hatch.build.targets.wheel]
packages = ["src/rebar"]
artifacts = ["src/rebar/_build_info.py"]

[tool.hatch.build.targets.sdist]
include = ["hatch_build.py", "src/rebar"]
"""


def materialize_build_hook_package(source_repo: Path, dest: Path) -> str:
    """Create a tiny committed Hatch package using ``source_repo``'s real build hook."""
    package = dest / "src" / "rebar"
    package.mkdir(parents=True)
    (dest / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    shutil.copy2(source_repo / "hatch_build.py", dest / "hatch_build.py")
    (package / "__init__.py").write_text("", encoding="utf-8")
    for args in (
        ["git", "-c", "init.templateDir=", "init", "-q", "--initial-branch=main"],
        ["git", "config", "user.email", "fixture@example.invalid"],
        ["git", "config", "user.name", "fixture"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "-A", "-f"],
        ["git", "commit", "-q", "--no-verify", "-m", "fixture checkout"],
    ):
        subprocess.run(args, cwd=dest, check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
