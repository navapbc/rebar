"""rebar root/config resolution (Python side).

Mirrors ``_engine/rebar-config.sh`` so the library and CLI agree with the bash
engine on repo-root and config-file location.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def repo_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the repository root.

    Order: explicit arg > REBAR_ROOT > PROJECT_ROOT > git toplevel of cwd.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            return Path(out).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def config_file(root: str | os.PathLike[str] | None = None) -> Path | None:
    """First existing of $REBAR_CONFIG, <root>/.rebar/config.conf, <root>/.rebar.conf."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    base = repo_root(root)
    for candidate in (base / ".rebar" / "config.conf", base / ".rebar.conf"):
        if candidate.is_file():
            return candidate
    return None


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store (.tickets-tracker), honoring the env override."""
    env = os.environ.get("TICKETS_TRACKER_DIR")
    if env:
        return Path(env)
    return repo_root(root) / ".tickets-tracker"
