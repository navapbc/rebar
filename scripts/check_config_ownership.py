#!/usr/bin/env python3
"""Config-ownership gate (RP-04 S7.1, ticket 29a9).

A deterministic, offline AST gate proving that ONLY approved composition roots and
provider boundaries read ambient configuration and credentials. Every prohibited ambient
access (env read, ``load_config`` call, credential read, backend reload, configurable
default) BELOW an approved seam is an error unless it is a recorded legacy exception or
carries a ``# read-via:`` marker.

Env-read detection REUSES ``scripts/gen_env_registry.py`` (``scan`` / ``KNOWN_ENV_HELPERS``
/ ``_is_os_environ`` / ``_str_literal``) by direct import — there is NO parallel env-read
parser here. On top of those primitives this gate layers seam/ownership classification,
the getattr/backend/configurable-default categories, and receiver-object aliasing.

API contract:
  - check(root: Path) -> list[str]          # sorted error strings, [] == clean
  - main(argv: list[str] | None) -> int      # 0 clean, 1 failures
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# Reuse the canonical env-read primitives from the sibling generator.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_ownership_exceptions import LEGACY_EXCEPTIONS  # noqa: E402
from gen_env_registry import (  # noqa: E402
    KNOWN_ENV_HELPERS,
    _is_os_environ,
    _str_literal,
    scan,
)

# --------------------------------------------------------------------------- #
# Approved seams — a finding fires only BELOW the relevant seam.
# --------------------------------------------------------------------------- #

# Composition roots own the ambient-config categories (env read, helper-shim env read,
# load_config call, getattr-env) — classified by module BASENAME.
COMPOSITION_ROOT_BASENAMES: frozenset[str] = frozenset(
    {
        "config.py",
        "_config_sources.py",
        "_config_schema.py",
        "_child_env.py",
        "model_classes.py",
    }
)

# Exact composition-root adapters, keyed on the path relative to the scan root.
_COMPOSITION_ROOT_RELPATHS: frozenset[str] = frozenset(
    {
        "llm/anthropic_model.py",
        "llm/bedrock_model.py",
    }
)

# Provider-credential boundaries own the credential-read category.
_CREDENTIAL_BOUNDARY_RELPATHS: frozenset[str] = frozenset(
    {
        "_engine/rebar_reconciler/runtime.py",
        "_engine/rebar_reconciler/access_check.py",
        "_engine/rebar_reconciler/adapters/jira_datacenter/settings.py",
        "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
    }
)

# The backend-selection boundary owns the backend-reload category (whole package).
_BACKEND_PACKAGE_PREFIX = "_engine/rebar_reconciler/"

# Owner tokens carried by an Access.
_OWNER_COMPOSITION = "composition"
_OWNER_CREDENTIAL = "credential"
_OWNER_BACKEND = "backend"

# A helper whose NAME looks like an env-read shim (fail-closed cat-2 sub-case i).
_SHIM_RE = re.compile(r"^_.*(env|pref|getenv)")
# An env-name-shaped string literal.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
# A glob metacharacter in an exception path.
_GLOB_RE = re.compile(r"[*?\[\]]")
# A ``# read-via:`` suppression marker on the reading line.
_MARKER_RE = re.compile(r"#\s*read-via:")

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


# --------------------------------------------------------------------------- #
# Derivations (live, not hand-copied).
# --------------------------------------------------------------------------- #


def _ensure_src_on_path() -> None:
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def derive_credential_names() -> dict[str, str]:
    """Return ``{env-name: owning-adapter}`` for every adapter send credential.

    Derived live from ``rebar._child_env._ADAPTER_SECRET_NAMES`` (names only, never
    values), so a new adapter secret is picked up without editing this gate.
    """
    _ensure_src_on_path()
    from rebar._child_env import _ADAPTER_SECRET_NAMES

    out: dict[str, str] = {}
    for adapter, names in _ADAPTER_SECRET_NAMES.items():
        for name in names:
            out[name] = adapter
    return out


def derive_backend_keys() -> set[str]:
    """Return the set of backend keys registered via ``@register("<literal>")``.

    AST-scans the adapter packages under ``_engine/rebar_reconciler/adapters/`` rather
    than reading ``_backend_registry._REGISTRY`` (empty until the adapters import).
    """
    base = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "adapters"
    keys: set[str] = set()
    if not base.exists():
        return keys
    for py in sorted(base.rglob("*.py")):
        tree = _parse(py)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, *_FUNC_DEFS)):
                continue
            for dec in node.decorator_list:
                key = _register_key(dec)
                if key is not None:
                    keys.add(key)
    return keys


def _register_key(dec: ast.expr) -> str | None:
    if not isinstance(dec, ast.Call) or not dec.args:
        return None
    name = _resolved_callee(dec.func, {})
    if name != "register":
        return None
    return _str_literal(dec.args[0])


def derive_env_baseline() -> set[str]:
    """Return the union of scanned env-var names and env-channel deprecation aliases."""
    _ensure_src_on_path()
    reads, _dynamic = scan(REPO_ROOT / "src" / "rebar")
    names: set[str] = set(reads)
    from rebar._deprecations import REGISTRY

    for dep in REGISTRY.values():
        if dep.kind == "env":
            names.add(dep.name)
    return names


# --------------------------------------------------------------------------- #
# Seam classification (keyed on the path RELATIVE TO ROOT).
# --------------------------------------------------------------------------- #


def _is_composition_root(relpath: str) -> bool:
    return relpath.rsplit("/", 1)[-1] in COMPOSITION_ROOT_BASENAMES or (
        relpath in _COMPOSITION_ROOT_RELPATHS
    )


def _is_credential_boundary(relpath: str) -> bool:
    return relpath in _CREDENTIAL_BOUNDARY_RELPATHS


def _is_backend_package(relpath: str) -> bool:
    return relpath.startswith(_BACKEND_PACKAGE_PREFIX)


def _is_owned(relpath: str, owner: str) -> bool:
    if owner == _OWNER_COMPOSITION:
        return _is_composition_root(relpath)
    if owner == _OWNER_CREDENTIAL:
        return _is_credential_boundary(relpath)
    if owner == _OWNER_BACKEND:
        return _is_backend_package(relpath)
    return False


# --------------------------------------------------------------------------- #
# Per-module AST analysis.
# --------------------------------------------------------------------------- #


class Access:
    """One prohibited-category access discovered in a module."""

    __slots__ = ("kind", "lineno", "node", "owner", "symbol")

    def __init__(self, lineno: int, symbol: str, kind: str, owner: str, node: ast.expr) -> None:
        self.lineno = lineno
        self.symbol = symbol
        self.kind = kind
        self.owner = owner
        self.node = node


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def _resolved_callee(func: ast.expr, alias: dict[str, str]) -> str | None:
    """Resolve a call target to its canonical callee name (alias-aware)."""
    if isinstance(func, ast.Name):
        return alias.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Scanner:
    """Detects prohibited-category accesses in a single parsed module."""

    def __init__(self, tree: ast.Module, credential_names: dict[str, str]) -> None:
        self.tree = tree
        self.cred = credential_names
        self.callee_alias: dict[str, str] = {}
        self.module_alias: dict[str, str] = {}
        self.env_callee_alias: dict[str, str] = {}
        self.environ_aliases: set[str] = set()
        self.config_default_nodes: set[int] = set()
        self._build_module_aliases()
        self._build_env_callee_aliases()
        self._build_callee_aliases()
        self._build_environ_aliases()
        self._build_config_default_positions()

    # -- alias / receiver binding ------------------------------------------- #

    def _build_module_aliases(self) -> None:
        """Map every local binding of the ``os`` module (``import os``,
        ``import os as _o``) to the canonical name ``os``."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        self.module_alias[alias.asname or alias.name] = "os"

    def _resolves_os_module(self, name: str) -> bool:
        return name == "os" or self.module_alias.get(name) == "os"

    def _build_env_callee_aliases(self) -> None:
        """Map every local rebinding of ``os.getenv`` to the canonical ``os.getenv``:
        ``from os import getenv as ge`` and ``ge = os.getenv`` (alias-aware receiver)."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name == "getenv":
                        self.env_callee_alias[alias.asname or alias.name] = "os.getenv"
            elif isinstance(node, ast.Assign) and self._is_getenv_attr(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.env_callee_alias[target.id] = "os.getenv"

    def _is_getenv_attr(self, value: ast.expr) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "getenv"
            and isinstance(value.value, ast.Name)
            and self._resolves_os_module(value.value.id)
        )

    def _build_callee_aliases(self) -> None:
        canonical = {"load_config", "select_backend"}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in canonical:
                        self.callee_alias[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                src = self.callee_alias.get(node.value.id, node.value.id)
                if src in canonical:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.callee_alias[target.id] = src

    def _is_os_environ_aliased(self, node: ast.expr) -> bool:
        """``os.environ`` or ``<alias>.environ`` where the alias resolves to ``os``."""
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and self._resolves_os_module(node.value.id)
        )

    def _resolves_environ(self, node: ast.expr) -> bool:
        if _is_os_environ(node) or self._is_os_environ_aliased(node):
            return True
        return isinstance(node, ast.Name) and node.id in self.environ_aliases

    def _binds_environ(self, value: ast.expr) -> bool:
        candidates: list[ast.expr] = [value]
        if isinstance(value, ast.IfExp):
            candidates += [value.body, value.orelse]
        elif isinstance(value, ast.BoolOp):
            candidates += list(value.values)
        return any(self._resolves_environ(c) for c in candidates)

    def _build_environ_aliases(self) -> None:
        assigns: list[tuple[str, ast.expr]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigns.append((target.id, node.value))
            elif isinstance(node, ast.AnnAssign) and (
                isinstance(node.target, ast.Name) and node.value is not None
            ):
                assigns.append((node.target.id, node.value))
        for _ in range(3):
            grew = False
            for name, value in assigns:
                if name not in self.environ_aliases and self._binds_environ(value):
                    self.environ_aliases.add(name)
                    grew = True
            if not grew:
                break

    def _build_config_default_positions(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                self._mark_subtree(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._mark_subtree(node.value)
        for fnode in ast.walk(self.tree):
            if isinstance(fnode, _FUNC_DEFS):
                self._mark_defaults(fnode)

    def _mark_defaults(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        defaults: list[ast.expr] = list(fn.args.defaults)
        defaults += [d for d in fn.args.kw_defaults if d is not None]
        for default in defaults:
            self._mark_subtree(default)

    def _mark_subtree(self, node: ast.AST) -> None:
        for descendant in ast.walk(node):
            self.config_default_nodes.add(id(descendant))

    # -- access classification ---------------------------------------------- #

    def accesses(self) -> list[Access]:
        out: list[Access] = []
        for node in ast.walk(self.tree):
            acc = self._classify(node)
            if acc is None:
                continue
            self._apply_config_default(acc)
            out.append(acc)
        return out

    def _apply_config_default(self, acc: Access) -> None:
        if acc.owner == _OWNER_BACKEND:
            return
        if id(acc.node) in self.config_default_nodes:
            acc.kind = "configurable-default"

    def _classify(self, node: ast.AST) -> Access | None:
        if isinstance(node, ast.Subscript):
            return self._classify_subscript(node)
        if isinstance(node, ast.Call):
            return self._classify_call(node)
        return None

    def _classify_subscript(self, node: ast.Subscript) -> Access | None:
        if not self._resolves_environ(node.value):
            return None
        return self._env_access(node, _str_literal(node.slice), "os.environ[...]")

    def _classify_call(self, node: ast.Call) -> Access | None:
        func = node.func
        direct = self._classify_environ_call(node, func)
        if direct is not None:
            return direct
        return self._classify_named_call(node, func)

    def _is_getenv_callee(self, func: ast.expr) -> bool:
        """A callee resolving to ``os.getenv``: ``os.getenv``, ``<alias>.getenv``, or a
        name rebound to ``os.getenv`` (``from os import getenv as ge`` / ``ge = os.getenv``)."""
        if isinstance(func, ast.Attribute) and func.attr == "getenv":
            return isinstance(func.value, ast.Name) and self._resolves_os_module(func.value.id)
        if isinstance(func, ast.Name):
            return self.env_callee_alias.get(func.id) == "os.getenv"
        return False

    def _classify_environ_call(self, node: ast.Call, func: ast.expr) -> Access | None:
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if self._resolves_environ(func.value) and node.args:
                return self._env_access(node, _str_literal(node.args[0]), "os.environ.get")
            return None
        if self._is_getenv_callee(func):
            if node.args:
                return self._env_access(node, _str_literal(node.args[0]), "os.getenv")
            return None
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and self._resolves_environ(node.args[0])
        ):
            return self._env_access(node, _str_literal(node.args[1]), "getattr")
        return None

    def _classify_named_call(self, node: ast.Call, func: ast.expr) -> Access | None:
        if isinstance(func, ast.Name) and func.id in KNOWN_ENV_HELPERS:
            return self._helper_access(node, func.id)
        name = _resolved_callee(func, self.callee_alias)
        if name == "load_config":
            return Access(node.lineno, "load_config", "load_config", _OWNER_COMPOSITION, node)
        if name == "select_backend":
            return Access(node.lineno, "select_backend", "backend-reload", _OWNER_BACKEND, node)
        return self._shim_access(node, func)

    def _helper_access(self, node: ast.Call, helper: str) -> Access | None:
        pos, prefix = KNOWN_ENV_HELPERS[helper]
        if len(node.args) <= pos:
            return None
        lit = _str_literal(node.args[pos])
        if lit is None:
            return self._unresolved(node, helper)
        return self._resolved_env(node, prefix + lit)

    def _shim_access(self, node: ast.Call, func: ast.expr) -> Access | None:
        if not isinstance(func, ast.Name):
            return None
        if func.id in KNOWN_ENV_HELPERS or not _SHIM_RE.search(func.id):
            return None
        lit = _str_literal(node.args[0]) if node.args else None
        if lit is not None and _ENV_NAME_RE.match(lit):
            return self._unresolved(node, lit)
        return None

    def _env_access(self, node: ast.expr, name: str | None, dynamic_symbol: str) -> Access:
        if name is None:
            return self._unresolved(node, dynamic_symbol)
        return self._resolved_env(node, name)

    def _resolved_env(self, node: ast.expr, name: str) -> Access:
        if name in self.cred:
            return Access(node.lineno, name, "credential-read", _OWNER_CREDENTIAL, node)
        return Access(node.lineno, name, "env-read", _OWNER_COMPOSITION, node)

    def _unresolved(self, node: ast.expr, symbol: str) -> Access:
        return Access(node.lineno, symbol, "unresolved-env-read", _OWNER_COMPOSITION, node)


# --------------------------------------------------------------------------- #
# check() algorithm.
# --------------------------------------------------------------------------- #


def _validate_exceptions() -> list[tuple[tuple[str, int], str]]:
    """Root-independent structural validation of ``LEGACY_EXCEPTIONS`` entries."""
    errors: list[tuple[tuple[str, int], str]] = []
    for entry in LEGACY_EXCEPTIONS:
        path = str(entry.get("path", ""))
        symbol = str(entry.get("symbol", ""))
        rationale = str(entry.get("rationale", ""))
        if _GLOB_RE.search(path):
            errors.append(
                ((path, 0), f"exception {path!r} ({symbol}): glob metacharacter '*' in path")
            )
        elif not path.endswith(".py"):
            errors.append(
                ((path, 0), f"exception {path!r} ({symbol}): names a directory, not a .py file")
            )
        if not rationale.strip():
            errors.append(((path, 0), f"exception {path!r} ({symbol}): empty rationale"))
    return errors


def _check_registry_completeness() -> list[tuple[tuple[str, int], str]]:
    """Every derived credential name must be read by >=1 credential-boundary module."""
    cred = derive_credential_names()
    read_names: set[str] = set()
    for relpath in _CREDENTIAL_BOUNDARY_RELPATHS:
        py = REPO_ROOT / "src" / "rebar" / relpath
        tree = _parse(py)
        if tree is None:
            continue
        for acc in _Scanner(tree, cred).accesses():
            if acc.symbol in cred:
                read_names.add(acc.symbol)
    errors: list[tuple[tuple[str, int], str]] = []
    for name in sorted(cred):
        if name not in read_names:
            errors.append(
                (
                    (name, 0),
                    f"credential {name} (adapter {cred[name]}) is UNOWNED: no "
                    f"provider-credential boundary reads it",
                )
            )
    return errors


def _exception_pairs() -> set[tuple[str, str]]:
    return {(str(e["path"]), str(e["symbol"])) for e in LEGACY_EXCEPTIONS}


def _suppressed(
    relpath: str, symbol: str, lines: list[str], lineno: int, pairs: set[tuple[str, str]]
) -> bool:
    if (relpath, symbol) in pairs:
        return True
    idx = lineno - 1
    if 0 <= idx < len(lines) and _MARKER_RE.search(lines[idx]):
        return True
    return False


def _scan_files(root: Path) -> list[tuple[tuple[str, int], str]]:
    cred = derive_credential_names()
    pairs = _exception_pairs()
    errors: list[tuple[tuple[str, int], str]] = []
    for py in sorted(root.rglob("*.py")):
        rel = py.relative_to(root).as_posix()
        tree = _parse(py)
        if tree is None:
            continue
        lines = py.read_text(encoding="utf-8").splitlines()
        for acc in _Scanner(tree, cred).accesses():
            if _is_owned(rel, acc.owner):
                continue
            if _suppressed(rel, acc.symbol, lines, acc.lineno, pairs):
                continue
            errors.append(
                (
                    (rel, acc.lineno),
                    f"{rel}:{acc.lineno}: {acc.symbol}: {acc.kind} below approved "
                    f"seam (owner: UNOWNED)",
                )
            )
    return errors


def check(root: Path) -> list[str]:
    """Return sorted error strings for the tree at ``root``; ``[]`` means clean."""
    collected: list[tuple[tuple[str, int], str]] = []
    collected += _validate_exceptions()
    collected += _check_registry_completeness()
    collected += _scan_files(root)
    collected.sort(key=lambda item: item[0])
    return [msg for _key, msg in collected]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="config-ownership gate")
    parser.add_argument("--root", default=str(REPO_ROOT / "src" / "rebar"))
    args = parser.parse_args(argv)
    errors = check(Path(args.root))
    for err in errors:
        print(err)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
