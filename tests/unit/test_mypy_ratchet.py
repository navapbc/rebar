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

# Committed ratchet baseline (tier 1: every def annotated). Only ever ADD — never remove.
_RATCHET_BASELINE = {
    "rebar._cli.*",
    "rebar._engine_support.*",
    "rebar._io.*",
    "rebar._snapshot.*",
    "rebar._store.*",
    "rebar.attest.*",
    "rebar.audit.*",
    "rebar.graph.*",
    "rebar.grounding.*",
    "rebar.metrics.*",
    "rebar.opcert_service.*",
    "rebar.reducer.*",
    "rebar.review_bot.*",
    "rebar.schemas.*",
}

# Committed baseline for ratchet TIER 2 (no explicit `Any`). Tier 1 is satisfied by
# `: Any`, which leaves a parameter as unchecked as no annotation at all (ticket cc77),
# so this second tier is what actually buys the navigability/contract drivers. Same
# shrink-only discipline: only ever ADD.
_ANY_RATCHET_BASELINE = {"rebar._cli.*", "rebar._snapshot.*"}

# Approved global strictness flags enabled by story d9f5 (apivorous-amebic-earwig epic).
# These are gated wholesale in [tool.mypy] because the tree is clean under them.
_REQUIRED_GLOBAL_FLAGS = (
    "no_implicit_optional",
    "strict_equality",
    "warn_redundant_casts",
    "warn_unused_ignores",
)
# Explicitly DEFERRED by the same epic — they require broader annotation work and must
# NOT be enabled globally yet. Pinning them here prevents an accidental wholesale enable.
_DEFERRED_GLOBAL_FLAGS = (
    "disallow_any_generics",
    "disallow_untyped_defs",
    # Tier 2 of the ratchet: per-package only. A wholesale enable would go red on the
    # ~390 explicit `Any` still in llm / _engine / _commands and invite `cast()` noise.
    "disallow_any_explicit",
)


def _global_mypy_config() -> dict:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["mypy"]


def _modules_with_flag(flag: str) -> set[str]:
    """Every module pattern that `[[tool.mypy.overrides]]` turns ``flag`` on for."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    enabled: set[str] = set()
    for override in data.get("tool", {}).get("mypy", {}).get("overrides", []):
        if override.get(flag) is True:
            mods = override.get("module", [])
            enabled.update([mods] if isinstance(mods, str) else mods)
    return enabled


def _disallow_untyped_modules() -> set[str]:
    return _modules_with_flag("disallow_untyped_defs")


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


def test_any_ratchet_strict_set_only_grows():
    """Tier 2 (`disallow_any_explicit`) is shrink-only exactly like tier 1."""
    enabled = _modules_with_flag("disallow_any_explicit")
    missing = _ANY_RATCHET_BASELINE - enabled
    assert not missing, (
        f"the explicit-`Any` ratchet regressed — these packages lost "
        f"disallow_any_explicit: {sorted(missing)}. That tier is shrink-only (it may "
        f"only grow); re-add them. `disallow_untyped_defs` alone does NOT cover them: "
        f"it is satisfied by `: Any` (ticket cc77)."
    )


def test_any_ratchet_packages_are_also_fully_annotated():
    """Tier 2 is a STRENGTHENING of tier 1, never an alternative to it: a package
    cannot ban explicit `Any` while still allowing un-annotated defs."""
    tier1 = _disallow_untyped_modules()
    tier2 = _modules_with_flag("disallow_any_explicit")
    assert tier2 <= tier1, (
        f"these packages ban explicit `Any` but are not in the disallow_untyped_defs "
        f"tier: {sorted(tier2 - tier1)}. Add them to the tier-1 override too."
    )


@pytest.mark.parametrize("flag", _REQUIRED_GLOBAL_FLAGS)
def test_approved_strictness_flag_enabled_globally(flag: str):
    """Each approved strictness flag is gated wholesale in [tool.mypy] (story d9f5)."""
    assert _global_mypy_config().get(flag) is True, (
        f"[tool.mypy].{flag} must be enabled globally (= true); the tree is clean under it."
    )


@pytest.mark.parametrize("flag", _DEFERRED_GLOBAL_FLAGS)
def test_deferred_strictness_flag_not_enabled_globally(flag: str):
    """Deferred flags must NOT be enabled at global scope (story d9f5).

    `disallow_untyped_defs` may only appear inside `[[tool.mypy.overrides]]` (the
    per-package shrink-only ratchet), never as a top-level `[tool.mypy]` key.
    """
    assert _global_mypy_config().get(flag) is not True, (
        f"[tool.mypy].{flag} must NOT be enabled globally — it is deferred by the epic."
    )
