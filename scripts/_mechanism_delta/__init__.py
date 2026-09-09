"""Private detectors and baseline plumbing for the mechanism-delta ratchet.

``DETECTORS`` maps seven mechanism kinds to callables grouped by input surface in
``detect_code``, ``detect_config``, and ``detect_ci``. The private subpackage stays outside
the top-level standalone import walk. Imports depend on the entrypoint adding ``scripts/``
to ``sys.path``.
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
