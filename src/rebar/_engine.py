"""Locate the bundled engine assets and build subprocess environments for them.

The ``rebar/_engine/`` package data holds ``jira-capability-probe.py`` (the live
preflight the library launches as a subprocess) and the alias ``resources/`` wordlist;
the Jira reconciler itself is the importable package ``rebar.reconciler``. This module
resolves that directory deterministically (editable or wheel install), exposes the in-
process ``rebar`` CLI path (:func:`in_process_cli`) the reconciler and ``validate`` read
tickets through, and builds the subprocess environment (:func:`engine_env`) that pins
the repo root for those launches.
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def engine_dir() -> Path:
    """Absolute path to the bundled ``_engine`` directory.

    ``importlib.resources.files`` returns a real filesystem path for editable and
    wheel installs (hatchling wheels install unzipped), which the subprocess
    launches require.

    The engine assets (the Jira probe and the wordlist) are read as real files, so
    the directory MUST be installed UNPACKED to a real on-disk path — zipimport /
    zip-safe installs are unsupported. We assert that here so a mispackaged install
    fails loudly at the first engine call instead of with an opaque import error.
    """
    path = Path(str(importlib.resources.files("rebar") / "_engine")).resolve()
    if not path.is_dir():
        raise RuntimeError(
            f"rebar engine directory is not a real on-disk directory: {path!s}. "
            "The engine assets (the Jira probe and the wordlist) must be "
            "installed UNPACKED to the filesystem; rebar does not support "
            "zipimport / zip-safe installs. Install from a wheel (hatchling builds "
            "unpacked) or as an editable install — not a zipapp/zip-imported package."
        )
    return path


def in_process_cli() -> str:
    """Path to the in-process ``rebar`` CLI used as a single-executable ticket reader.

    The reconciler (``reconciler/{applier,invariants,reconcile}.py``) and
    ``validate`` invoke this CLI as one executable (``[cli, "list"]``) to read local
    tickets: the ``rebar`` console script (``rebar.cli:main`` → ``rebar._cli.main``),
    which runs fully in-process. They call this resolver directly (no env handoff).

    Because callers treat the value as a single token, this returns the console
    script path rather than the multi-token ``python -m rebar`` (the package
    ``__main__`` entry, which serves as the import-path-independent fallback).
    Resolution prefers the console script next to the running interpreter (the
    venv/pipx ``bin`` dir — hermetic, independent of ``PATH``), then falls back to
    a ``PATH`` lookup. When neither is found we return the best-effort
    interpreter-adjacent path; callers warn and degrade to an empty list.
    """
    bindir = Path(sys.executable).parent
    found = shutil.which("rebar", path=str(bindir)) or shutil.which("rebar")
    return found if found else str(bindir / "rebar")


def wordlist_path() -> Path:
    """Path to the alias wordlist shipped with the engine."""
    return engine_dir() / "resources" / "ticket-wordlist.txt"


def engine_env(repo_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Return the environment for an engine subprocess (the reconciler or the Jira probe).

    The reconciler is the importable package ``rebar.reconciler`` (launched as
    ``python -m rebar.reconciler`` with ``sys.executable``) and the probe imports
    ``rebar.reconciler.*`` the same way, so nothing is prepended to ``PYTHONPATH``;
    the child inherits the parent's import path. When ``repo_root`` is given it is
    pinned as ``REBAR_ROOT``; otherwise an inherited ``REBAR_ROOT`` carries through.
    """
    env = dict(os.environ)
    if repo_root is not None:
        env["REBAR_ROOT"] = str(Path(repo_root).resolve())
    return env
