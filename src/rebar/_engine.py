"""Locate and invoke the bundled ticket engine.

The engine (bash dispatcher + ``ticket-*.sh`` + the ``ticket_reducer`` /
``ticket_graph`` / ``rebar_reconciler`` Python packages + ``rebar_reconciler/acli.py``)
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

    rebar's engine is bash + python helpers exec'd as real files, so it MUST be
    installed UNPACKED to a real on-disk directory — zipimport / zip-safe installs
    are unsupported. We assert that here so a mispackaged install fails loudly at
    the first engine call instead of with an opaque bash error.
    """
    path = Path(str(importlib.resources.files("rebar") / "_engine")).resolve()
    if not path.is_dir():
        raise RuntimeError(
            f"rebar engine directory is not a real on-disk directory: {path!s}. "
            "The engine (bash dispatcher + ticket-*.sh + python helpers) must be "
            "installed UNPACKED to the filesystem; rebar does not support "
            "zipimport / zip-safe installs. Install from a wheel (hatchling builds "
            "unpacked) or as an editable install — not a zipapp/zip-imported package."
        )
    return path


def dispatcher() -> Path:
    """Path to the ``rebar`` bash dispatcher inside the engine."""
    return engine_dir() / "rebar"


def wordlist_path() -> Path:
    """Path to the alias wordlist shipped with the engine."""
    return engine_dir() / "resources" / "ticket-wordlist.txt"


def engine_env(repo_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Environment for engine subprocesses (bash dispatcher + ``python3`` helpers).

    This is the ONLY place the engine dir is put on an import path — and it is
    scoped to subprocesses, never the in-process library path (the library imports
    ``rebar.reducer`` / ``rebar.graph`` / ``rebar._engine_support.*`` directly).

    - Prepends the engine dir to ``PYTHONPATH`` so the engine's bare ``python3``
      resolves the old top-level names (``ticket_reducer`` / ``ticket_graph`` /
      ``ticket_reads`` …, now thin compat shims) and ``python -m rebar_reconciler``.
      The shims self-bootstrap the ``rebar`` package onto ``sys.path`` from their
      own location, so the subprocess does not need ``rebar`` pre-resolved.
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
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the ``rebar`` bash dispatcher as a subprocess.

    When no explicit ``cwd`` is given, the dispatcher runs *inside* the repo
    root (resolved from ``repo_root`` / REBAR_ROOT / PROJECT_ROOT) so that the
    engine's cwd-relative git operations resolve the right repository even when
    the library/CLI is invoked from an unrelated directory.
    """
    env = engine_env(repo_root)
    if env_extra:
        env.update(env_extra)
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
