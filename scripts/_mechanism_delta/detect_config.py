"""Detect environment variables, config keys, and feature flags from canonical registries.

Environment detection path-loads ``gen_env_registry.scan`` and keeps ``REBAR_*`` reads. It
adds deprecation aliases and ``MCP_ENV_VARS`` entries with the ``REBAR_MCP_`` prefix. Names
use literal sites or the first attributed file head. A missing environment registry yields
an empty environment surface; unavailable derived imports omit only the alias and MCP
additions. Configuration detection reads ``_SECTIONS``; a missing, unreadable, unparseable,
or non-dict registry yields an empty configuration surface. Section-qualified boolean
entries belong only to ``feature_flag``. All other entries belong only to ``config_key``.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

from .markers import Site

CONFIG_SECTIONS_RELPATH = "src/rebar/_config_sections.py"
SECTIONS_SYMBOL = "_SECTIONS"
BOOL_COERCER = "_as_bool"
ENV_PREFIX = "REBAR_"
MCP_ENV_PREFIX = "REBAR_MCP_"


def _load_env_registry(repo_root: Path):
    """Load this tree's ``gen_env_registry.py`` by path, or return ``None`` if absent."""
    path = repo_root / "scripts" / "gen_env_registry.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_mechanism_delta_env_registry", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _derived_env_names(repo_root: Path) -> dict[str, str]:
    """The two families ``scan`` cannot see, mapped to the file they are attributed to."""
    src = str(repo_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    names: dict[str, str] = {}
    try:
        from rebar._deprecations import REGISTRY
        from rebar.mcp_server import MCP_ENV_VARS
    except ImportError:
        return names  # a tree without the package has no derived env surface
    for dep in REGISTRY.values():
        if dep.kind == "env" and dep.name.startswith(ENV_PREFIX):
            names[dep.name] = "src/rebar/config.py"
    for entry in MCP_ENV_VARS:
        name = str(entry["name"])
        if name.startswith(MCP_ENV_PREFIX):
            names[name] = CONFIG_SECTIONS_RELPATH
    return names


def _literal_sites(repo_root: Path, name: str, relpaths: list[str]) -> list[Site]:
    """Return literal sites, or the first attributed file head when no literal exists."""
    sites: list[Site] = []
    fallback: Path | None = None
    for relpath in relpaths:
        # Attributions may carry a parenthetical note ("… (alias resolver)").
        path = repo_root / relpath.split(" (")[0]
        if not path.is_file():
            continue
        if fallback is None:
            fallback = path
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - defensive
            continue
        sites.extend((name, path, i) for i, line in enumerate(lines, 1) if name in line)
    if not sites and fallback is not None:
        sites.append((name, fallback, None))
    return sites


def detect_env_vars(repo_root: Path) -> list[Site]:
    """Every ``REBAR_*`` environment variable rebar reads, with its literal sites."""
    registry = _load_env_registry(repo_root)
    if registry is None:
        return []
    reads, _dynamic = registry.scan(repo_root / "src" / "rebar")
    attributed: dict[str, set[str]] = {
        name: set(modules) for name, modules in reads.items() if name.startswith(ENV_PREFIX)
    }
    for name, relpath in _derived_env_names(repo_root).items():
        attributed.setdefault(name, set()).add(relpath)
    sites: list[Site] = []
    for name in sorted(attributed):
        sites.extend(_literal_sites(repo_root, name, sorted(attributed[name])))
    return sites


def _sections_node(tree: ast.Module) -> ast.Dict | None:
    """The ``_SECTIONS`` dict literal, however it is spelled (``Assign``/``AnnAssign``)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target: ast.expr = node.target
            value: ast.expr | None = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == SECTIONS_SYMBOL:
            if isinstance(value, ast.Dict):
                return value
    return None


def _config_entries(repo_root: Path) -> list[tuple[str, Site]]:
    """``[(kind, site), ...]`` for every ``_SECTIONS`` key, partitioned by boolean-ness."""
    path = repo_root / CONFIG_SECTIONS_RELPATH
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return []
    sections = _sections_node(tree)
    if sections is None:
        return []
    entries: list[tuple[str, Site]] = []
    for section_node, keys_node in zip(sections.keys, sections.values, strict=True):
        if not (isinstance(section_node, ast.Constant) and isinstance(section_node.value, str)):
            continue
        if not isinstance(keys_node, ast.Dict):
            continue
        section = section_node.value
        for key_node, coercer in zip(keys_node.keys, keys_node.values, strict=True):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            kind = "feature_flag" if BOOL_COERCER in ast.unparse(coercer) else "config_key"
            entries.append((kind, (f"{section}.{key_node.value}", path, key_node.lineno)))
    return entries


def detect_config_keys(repo_root: Path) -> list[Site]:
    """Section-qualified nonboolean config keys."""
    return [site for kind, site in _config_entries(repo_root) if kind == "config_key"]


def detect_feature_flags(repo_root: Path) -> list[Site]:
    """Section-qualified boolean-coerced config keys."""
    return [site for kind, site in _config_entries(repo_root) if kind == "feature_flag"]
