"""Detectors and baseline plumbing for the mechanism-delta ratchet (ticket 9ca8-675e).

The seven detectors live in three modules split by INPUT SURFACE — Python AST
(:mod:`detect_code`), the config registries (:mod:`detect_config`), globs and YAML
(:mod:`detect_ci`) — rather than one module per kind or one module for all seven. Seven
detectors in one file would breach both the repository's module-size and per-function
complexity limits; splitting by surface keeps each module's file walk, parse cache and
failure mode in one place.

:data:`DETECTORS` is a FLAT ``{kind: callable}`` table, so the entrypoint dispatches with a
single comprehension instead of a seven-branch conditional and stays near complexity 4.

This is a private ``scripts/`` subpackage, deliberately: ``scripts/check_import_walk.py``
imports every top-level ``scripts/*.py`` standalone with the scripts directory stripped from
``sys.path``, and a subpackage is invisible to that walk. The entrypoint reaches it with the
``sys.path.insert`` pattern its sibling scripts already use.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .baseline import KINDS, SCHEMA_VERSION, SchemaError, parse_baseline, render_baseline
from .compare import Counters, compare, drain_stale, evaluate
from .detect_ci import detect_ci_gates, detect_test_helpers
from .detect_code import detect_autouse_fixtures, detect_locks
from .detect_config import detect_config_keys, detect_env_vars, detect_feature_flags
from .markers import MARKER, MarkerMap, Site, harvest

DETECTORS: dict[str, Callable[[Path], list[Site]]] = {
    "lock": detect_locks,
    "env_var": detect_env_vars,
    "config_key": detect_config_keys,
    "feature_flag": detect_feature_flags,
    "ci_gate": detect_ci_gates,
    "autouse_fixture": detect_autouse_fixtures,
    "test_helper": detect_test_helpers,
}


def scan_sites(repo_root: Path | str) -> dict[str, list[Site]]:
    """Run every detector once and return ``{kind: [site, ...]}``."""
    return {kind: detect(Path(repo_root)) for kind, detect in DETECTORS.items()}


__all__ = [
    "DETECTORS",
    "KINDS",
    "MARKER",
    "SCHEMA_VERSION",
    "Counters",
    "MarkerMap",
    "SchemaError",
    "Site",
    "compare",
    "drain_stale",
    "evaluate",
    "harvest",
    "parse_baseline",
    "render_baseline",
    "scan_sites",
]
