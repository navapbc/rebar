"""Hatchling build hook: bake the gate-code commit SHA into the wheel/sdist.

A PyPI/wheel install has no rebar git checkout, so the live-git SHA resolver in
``rebar.signing`` (epic jira-reb-596) cannot answer "which gate code certified this?".
This hook captures the source-tree commit at build time and writes it into a
generated, git-ignored ``src/rebar/_build_info.py`` so the resolver has a build-baked
fallback on non-git installs.

``python -m build`` builds an sdist, then builds the WHEEL *from the extracted sdist*
— a tree with no ``.git`` and (before story 6168) no way to recover the SHA, so the
naive ``git rev-parse`` path baked ``COMMIT = None`` into the published wheel. The fix
is a four-step precedence resolved by :func:`_resolve_build_commit`:

1. ``REBAR_BUILD_COMMIT`` env var, if set → use it (the release build sets it to
   ``${GITHUB_SHA::7}``). **Set-but-empty is a hard error** — release context only,
   we refuse to bake an unknown commit rather than silently falling through to None.
2. else PRESERVE an existing non-null ``COMMIT`` already baked into the target
   ``_build_info.py`` — the sdist ships that file with the SHA baked at release-build
   time, and the rebuild-from-sdist (no ``.git``/env) preserves it instead of
   overwriting with None.
3. else ``git rev-parse --short HEAD`` (a plain dev ``pip install .`` from a checkout).
4. else ``None`` (a source tree with no git and no baked SHA — never a build failure
   when the env var is unset).

The canonical format is the SHORT sha, matching ``rebar.signing``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_TARGET = Path("src/rebar/_build_info.py")
_ENV_VAR = "REBAR_BUILD_COMMIT"


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


def _read_existing_commit(target: Path) -> str | None:
    """The non-null ``COMMIT`` already baked into ``target`` (the sdist-shipped file), or None.

    Reads the generated module textually and executes it in an isolated namespace — it is
    our own trivial ``COMMIT = "..."`` assignment, never third-party code.
    """
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    ns: dict = {}
    try:
        exec(compile(text, str(target), "exec"), ns)  # noqa: S102 - our own generated module
    except Exception:  # noqa: BLE001 - a malformed/partial file must degrade to "no baked SHA"
        return None
    val = ns.get("COMMIT")
    return val if isinstance(val, str) and val else None


def _resolve_build_commit(root: Path, existing: str | None, env: Mapping[str, str]) -> str | None:
    """Resolve the commit SHA to bake, per the four-step precedence (see module docstring).

    Raises ``ValueError`` when ``REBAR_BUILD_COMMIT`` is set but empty/blank — the
    release-context fail-fast, so a release build never silently loses provenance. An
    UNSET env var degrades gracefully through steps 2–4 and never raises, so a plain dev
    ``pip install .`` cannot be broken by this hook.
    """
    if _ENV_VAR in env:
        val = (env[_ENV_VAR] or "").strip()
        if not val:
            raise ValueError(
                f"{_ENV_VAR} is set but empty — refusing to bake an unknown commit into "
                "the build (release-context fail-fast). Unset it to fall back to the git "
                "SHA / preserved value, or set it to a real short SHA."
            )
        return val
    if existing:
        return existing
    return _build_commit(root)


class CustomBuildHook(BuildHookInterface):
    """Writes ``src/rebar/_build_info.py`` before the wheel/sdist is assembled."""

    def initialize(self, version: str, build_data: dict) -> None:  # noqa: ARG002
        root = Path(self.root)
        target = root / _TARGET
        existing = _read_existing_commit(target)
        commit = _resolve_build_commit(root, existing, os.environ)
        target.write_text(
            '"""Generated at build time by hatch_build.py — do NOT edit or commit.\n\n'
            "Build-baked gate-code commit SHA for non-git (wheel/PyPI) installs; the live\n"
            "checkout SHA still wins when present. See epic jira-reb-596.\n"
            '"""\n\n'
            f"COMMIT = {commit!r}\n"
        )
        # Ensure the generated (git-ignored) file ships in BOTH the wheel and the sdist.
        # `initialize` fires for both targets, so force-including it here gives the sdist a
        # baked SHA to preserve on the rebuild-from-sdist path (precedence step 2).
        build_data.setdefault("force_include", {})[str(target)] = str(_TARGET)
