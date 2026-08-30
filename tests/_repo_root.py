"""Install-agnostic repository-root discovery for the test suite.

Many tests need to reach *repo-root-only* development artifacts — ``scripts/``,
``docs/``, ``.github/``, ``hatch_build.py``, ``pyproject.toml``, ``LICENSE``,
``Makefile`` — that ship in the checkout but NOT inside the installed package.

Deriving that root from an imported package's location
(``Path(rebar.__file__).resolve().parents[2]``) is only correct under an
*editable* install, where ``rebar.__file__`` is ``<checkout>/src/rebar/__init__.py``.
Under a real (wheel/site-packages) install ``rebar.__file__`` is
``<venv>/lib/pythonX.Y/site-packages/rebar/__init__.py``, so the same climb lands
in the virtualenv and the artifact is not found — which is exactly why the
"Test Suite (mirror)" non-editable sweep legs went red.

Test *files*, by contrast, always live in the checkout (they are never
installed). Resolving the repo root from *this* module's own location is
therefore correct under BOTH the editable and the installed layouts. We walk up
from here to the nearest ancestor that carries the repository markers
(``pyproject.toml`` and ``hatch_build.py``) so the value is robust to the file
being moved between test subdirectories.
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
