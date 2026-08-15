"""Held-out scale oracle for the build-provenance fixture."""

from __future__ import annotations

import subprocess
from pathlib import Path

from _build_provenance_fixture import materialize_build_hook_package

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fixture_materializes_only_the_production_hook_boundary(tmp_path: Path) -> None:
    tree = tmp_path / "fixture"

    materialize_build_hook_package(_REPO_ROOT, tree)

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tracked == ["hatch_build.py", "pyproject.toml", "src/rebar/__init__.py"]
    assert (tree / "hatch_build.py").read_bytes() == (_REPO_ROOT / "hatch_build.py").read_bytes()
