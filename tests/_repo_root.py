"""Find the checkout root from this test file, including under wheel installs.

Development artifacts are absent from site-packages, so the installed rebar module
cannot anchor their paths. Walk to the ancestor with repository markers instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_MARKERS = ("pyproject.toml", "hatch_build.py")


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Return the repository checkout root, independent of install layout."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if all((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    raise RuntimeError(
        f"could not locate the repository root (no ancestor of {here} contains all of {_MARKERS})"
    )


REPO_ROOT = repo_root()
