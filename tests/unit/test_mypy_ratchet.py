"""Guard for the mypy strictness ratchet (story d2fa / dullish-computable-buck).

`[[tool.mypy.overrides]]` enables `disallow_untyped_defs` for a set of already-clean
packages. That set is SHRINK-ONLY for the exempt list — i.e. the strict set may only
GROW; a package may never be removed from it. This test pins the committed baseline as a
subset of the currently-enabled set, so dropping a package (regressing strictness) turns
the build red. To promote a package, annotate its defs until `mypy src/rebar/<pkg>
--disallow-untyped-defs` is clean, then add it to the override module list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Committed ratchet baseline. Only ever ADD to this — never remove.
_RATCHET_BASELINE = {"rebar.graph.*", "rebar.grounding.*"}


def _disallow_untyped_modules() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    enabled: set[str] = set()
    for override in data.get("tool", {}).get("mypy", {}).get("overrides", []):
        if override.get("disallow_untyped_defs") is True:
            mods = override.get("module", [])
            enabled.update([mods] if isinstance(mods, str) else mods)
    return enabled


def test_check_untyped_defs_is_globally_enabled():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["tool"]["mypy"]["check_untyped_defs"] is True


def test_ratchet_strict_set_only_grows():
    enabled = _disallow_untyped_modules()
    missing = _RATCHET_BASELINE - enabled
    assert not missing, (
        f"mypy strictness ratchet regressed — these packages lost disallow_untyped_defs: "
        f"{sorted(missing)}. The strict set is shrink-only (it may only grow); re-add them."
    )
