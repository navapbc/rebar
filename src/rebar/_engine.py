"""Locate and invoke the bundled ticket engine.

The engine (bash dispatcher + ``ticket-*.sh`` + the ``ticket_reducer`` /
``ticket_graph`` / ``dso_reconciler`` Python packages + ``acli-integration.py``)
ships as package data under ``rebar/_engine/``. This module resolves that
directory deterministically (editable or wheel install) and runs the dispatcher
as a subprocess with an environment that pins repo-root and import paths.
"""

from __future__ import annotations

import importlib.resources
import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def engine_dir() -> Path:
    """Absolute path to the bundled ``_engine`` directory.

    ``importlib.resources.files`` returns a real filesystem path for editable
    and wheel installs (hatchling wheels install unzipped), which bash requires.
    """
    return Path(str(importlib.resources.files("rebar") / "_engine")).resolve()


def dispatcher() -> Path:
    """Path to the ``rebar`` bash dispatcher inside the engine."""
    return engine_dir() / "rebar"


def wordlist_path() -> Path:
    """Path to the alias wordlist shipped with the engine."""
    return engine_dir() / "resources" / "ticket-wordlist.txt"


def engine_env(repo_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Environment for engine subprocesses.

    - Prepends the engine dir to ``PYTHONPATH`` so bundled ``python3`` helpers
      and ``python -m dso_reconciler`` resolve ``ticket_reducer`` / ``ticket_graph``
      / ``dso_reconciler`` imports.
    - Pins ``REBAR_ROOT`` *and* ``PROJECT_ROOT`` (the bash write path reads
      ``PROJECT_ROOT``; the reconciler reads ``REBAR_ROOT`` — they must agree).
    - Pins ``TICKET_WORDLIST_PATH`` so alias generation never falls back to hex
      regardless of the engine dir's path shape.
    """
    env = dict(os.environ)
    eng = str(engine_dir())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = eng + (os.pathsep + existing if existing else "")
    env["TICKET_WORDLIST_PATH"] = str(wordlist_path())
    env.setdefault("REBAR_TICKET_CLI", str(dispatcher()))

    if repo_root is not None:
        root = str(Path(repo_root).resolve())
        env["REBAR_ROOT"] = root
        env["PROJECT_ROOT"] = root
    else:
        # If a caller (or parent env) set one of the two, mirror it to the other
        # so the write path and reconciler never disagree on repo-root.
        root = env.get("REBAR_ROOT") or env.get("PROJECT_ROOT")
        if root:
            env["REBAR_ROOT"] = root
            env["PROJECT_ROOT"] = root
    return env


def run(
    args,
    *,
    repo_root: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    input: str | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Invoke the ``rebar`` bash dispatcher as a subprocess.

    When no explicit ``cwd`` is given, the dispatcher runs *inside* the repo
    root (resolved from ``repo_root`` / REBAR_ROOT / PROJECT_ROOT) so that the
    engine's cwd-relative git operations resolve the right repository even when
    the library/CLI is invoked from an unrelated directory.
    """
    env = engine_env(repo_root)
    if cwd is None:
        cwd = env.get("REBAR_ROOT") or env.get("PROJECT_ROOT")
        if cwd and not os.path.isdir(cwd):
            cwd = None
    cmd = ["bash", str(dispatcher()), *(str(a) for a in args)]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        input=input,
        env=env,
        text=True,
        capture_output=capture,
        check=check,
    )
