"""Config-registry mechanism detectors: ``env_var``, ``config_key`` and ``feature_flag``.

Split from the other detectors by INPUT SURFACE: all three read rebar's own configuration
registries rather than the tree at large, so all three are only as complete as those
registries are — and each one is derived from the SAME source the shipped code resolves it
from, never from a parallel list that could drift.

``env_var``
    Reuses ``scripts/gen_env_registry.py``'s :func:`scan`, the fail-closed AST scanner that
    already backs ``docs/env-vars.md``, filtered to ``REBAR_*``. ``scan`` alone is NOT the
    whole surface: ``gen_env_registry.render`` unions in two further families that no literal
    ``os.environ`` call names — the env-channel aliases from ``rebar._deprecations.REGISTRY``
    and the ``REBAR_MCP_*`` variables derived from ``rebar.mcp_server.MCP_ENV_VARS``. That
    union is replicated here; omitting it would report ~40 spurious deltas the moment the
    baseline were regenerated from either half alone.

``config_key`` / ``feature_flag``
    Both come from ``rebar._config_sections._SECTIONS``, and together they PARTITION it:
    ``feature_flag`` claims the boolean-coerced keys (their coercer mentions ``_as_bool``)
    and ``config_key`` claims only the non-boolean remainder. Counting a boolean key as both
    would demand two justifications for one definition site, which a per-kind marker cannot
    express. Names are SECTION-QUALIFIED (``<section>.<key>``) because ``_SECTIONS`` repeats
    key names across sections — ``allow_insecure`` lives in both ``reconciler`` and ``jira``,
    ``threshold`` in both ``ticket_clarity`` and ``compact`` — so a bare-key name would
    silently merge four definition sites into two entries.
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
    """Load ``scripts/gen_env_registry.py`` by path, or ``None`` if this tree has none.

    By PATH rather than by name so the detector reads the generator belonging to the tree it
    was pointed at, and so it works with ``scripts/`` off ``sys.path`` — the standalone-import
    contract ``scripts/check_import_walk.py`` enforces. A tree without the generator has no
    detectable env-var surface, which is a legitimate answer (an empty one), not an error.
    """
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
    """Sites where ``name`` appears as a literal in its attributed files.

    Falls back to the file-head shape when no line carries the literal — a derived variable
    (an alias resolved through the config table, a ``REBAR_MCP_*`` name built from a config
    key) is real, settable surface with no literal anywhere, and must still be admittable.
    """
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
    """Section-qualified NON-boolean config keys (the ``feature_flag`` remainder)."""
    return [site for kind, site in _config_entries(repo_root) if kind == "config_key"]


def detect_feature_flags(repo_root: Path) -> list[Site]:
    """Section-qualified boolean-coerced config keys."""
    return [site for kind, site in _config_entries(repo_root) if kind == "feature_flag"]
