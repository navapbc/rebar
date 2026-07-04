"""Hatchling build hook: bake the gate-code commit SHA into the wheel/sdist.

A PyPI/wheel install has no rebar git checkout, so the live-git SHA resolver in
``rebar.signing`` (epic jira-reb-596) cannot answer "which gate code certified this?".
This hook captures ``git rev-parse --short HEAD`` at build time and writes it into a
generated, git-ignored ``src/rebar/_build_info.py`` so the resolver has a build-baked
fallback on non-git installs. A build outside a git tree writes ``COMMIT = None`` (the
resolver then records the version only) — never a build failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_TARGET = Path("src/rebar/_build_info.py")


def _build_commit(root: Path) -> str | None:
    """Short HEAD SHA of the source tree being built, or ``None`` outside a git tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


class CustomBuildHook(BuildHookInterface):
    """Writes ``src/rebar/_build_info.py`` before the wheel/sdist is assembled."""

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        root = Path(self.root)
        commit = _build_commit(root)
        target = root / _TARGET
        target.write_text(
            '"""Generated at build time by hatch_build.py — do NOT edit or commit.\n\n'
            "Build-baked gate-code commit SHA for non-git (wheel/PyPI) installs; the live\n"
            "checkout SHA still wins when present. See epic jira-reb-596.\n"
            '"""\n\n'
            f"COMMIT = {commit!r}\n"
        )
        # Ensure the generated (git-ignored) file ships in the wheel.
        build_data.setdefault("force_include", {})[str(target)] = str(_TARGET)
