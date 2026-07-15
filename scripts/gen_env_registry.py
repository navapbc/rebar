#!/usr/bin/env python3
"""Generate ``docs/env-vars.md`` — the canonical registry of environment variables
read under ``src/rebar`` (audit maintainability #3).

The registry is DERIVED (like ``reviewers/index.json``): a CI drift gate regenerates it
and fails the build on any diff, so a new env-var read cannot ship undocumented.

Scope is defined RELATIVE to an explicit, enumerated set of read patterns (documented in
the generated file's header) — the generator does not claim to catch every conceivable
indirection:

  1. Direct stdlib literals: ``os.environ["X"]``, ``os.environ.get("X", …)``,
     ``os.getenv("X", …)`` with a string-literal key.
  2. Project env-read helpers (``KNOWN_ENV_HELPERS``): the string-literal env-name
     argument is resolved at each call site (one level through the shim).

Alias/deprecation status is read from ``rebar._deprecations.REGISTRY`` (``kind == "env"``).

Usage:
    python scripts/gen_env_registry.py            # regenerate docs/env-vars.md
    python scripts/gen_env_registry.py --check     # exit non-zero if the committed file is stale
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN_ROOT = REPO_ROOT / "src" / "rebar"
DOC_PATH = REPO_ROOT / "docs" / "env-vars.md"

# helper name -> (0-indexed position of the env-name argument, name prefix).
# Signatures verified against the current tree; extend by adding a row here.
KNOWN_ENV_HELPERS: dict[str, tuple[int, str]] = {
    "_rebar_env": (0, "REBAR_"),  # reconciler shim: os.environ.get(f"REBAR_{name}")
    "_env_int": (0, ""),  # outbound_differ.py, binding_store.py
    "_str_pref": (0, ""),  # llm/gate_source.py
    "_int_pref": (1, ""),  # _snapshot/janitor.py: (table, env_name, ...)
    "_llm_str": (2, ""),  # llm/config.py: (table, cli, env_name, ...)
    "_llm_int": (2, ""),  # llm/config.py: (table, cli, env_name, ...)
    "_int_env": (0, ""),  # review_bot/config.py
    "_severities_env": (0, ""),  # review_bot/config.py
    "_str_env": (0, ""),  # opcert_service/config.py: os.environ.get(name)
}


def _str_literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_environ(node: ast.expr) -> bool:
    # matches ``os.environ`` (Attribute attr=environ value=Name id=os)
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def scan(root: Path) -> tuple[dict[str, set[str]], list[tuple[str, int, str]]]:
    """Return (reads, dynamic) where ``reads`` maps each resolved env-var name to the set
    of module paths (relative to the repo root) that read it, and ``dynamic`` lists
    (module, lineno, callee) for reads whose name argument is not a string literal."""
    reads: dict[str, set[str]] = {}
    dynamic: list[tuple[str, int, str]] = []
    for py in sorted(root.rglob("*.py")):
        try:
            rel = py.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = py.as_posix()  # scan root outside the repo (e.g. a test temp dir)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # os.environ["X"]
            if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                name = _str_literal(node.slice)
                if name:
                    reads.setdefault(name, set()).add(rel)
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # os.environ.get("X", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and _is_os_environ(func.value)
            ):
                name = _str_literal(node.args[0]) if node.args else None
                if name:
                    reads.setdefault(name, set()).add(rel)
                elif node.args:
                    dynamic.append((rel, node.lineno, "os.environ.get"))
                continue
            # os.getenv("X", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                name = _str_literal(node.args[0]) if node.args else None
                if name:
                    reads.setdefault(name, set()).add(rel)
                elif node.args:
                    dynamic.append((rel, node.lineno, "os.getenv"))
                continue
            # project helper call: _rebar_env("X"), _llm_int(t, c, "X", ...), ...
            if isinstance(func, ast.Name) and func.id in KNOWN_ENV_HELPERS:
                pos, prefix = KNOWN_ENV_HELPERS[func.id]
                if len(node.args) > pos:
                    lit = _str_literal(node.args[pos])
                    if lit is not None:
                        reads.setdefault(prefix + lit, set()).add(rel)
                    else:
                        dynamic.append((rel, node.lineno, func.id))
    return reads, dynamic


def _env_aliases() -> dict[str, str]:
    """name -> annotation string, from rebar._deprecations.REGISTRY (env channel)."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from rebar._deprecations import REGISTRY

    out: dict[str, str] = {}
    for dep in REGISTRY.values():
        if dep.kind != "env":
            continue
        if dep.permanent:
            out[dep.name] = f"permanent alias of `{dep.replacement}`"
        else:
            out[dep.name] = f"deprecated alias of `{dep.replacement}` (removal in {dep.remove_in})"
    return out


def render(root: Path = DEFAULT_SCAN_ROOT) -> str:
    reads, dynamic = scan(root)
    aliases = _env_aliases()
    # Env-channel aliases are resolved through the config alias table
    # (config.py::_LEGACY_ENV_ALIASES), not a direct os.environ call the scanner sees, so
    # union them in explicitly — they are real, settable config surface.
    for alias_name in aliases:
        reads.setdefault(alias_name, set()).add("src/rebar/config.py (alias resolver)")
    # The REBAR_MCP_* gate vars are DERIVED from mcp config keys (env REBAR_MCP_<KEY_UPPER>),
    # not read through a literal os.environ call the AST scanner can see, so union them in
    # from the canonical MCP_ENV_VARS list — they are real, settable config surface.
    from rebar.mcp_server import MCP_ENV_VARS

    for entry in MCP_ENV_VARS:
        name = entry["name"]
        if name.startswith("REBAR_MCP_"):
            reads.setdefault(name, set()).add("src/rebar/_config_schema.py (mcp config)")
    lines: list[str] = []
    lines.append("# Environment variable registry")
    lines.append("")
    lines.append(
        "**Generated by `scripts/gen_env_registry.py` — do not edit by hand.** Run "
        "`python scripts/gen_env_registry.py` to regenerate; a CI drift gate fails the "
        "build if this file is stale."
    )
    lines.append("")
    lines.append(
        "This lists environment variables read under `src/rebar` via the following "
        "recognized read patterns (reads through other indirections are not captured — "
        "extend `KNOWN_ENV_HELPERS` in the generator to cover a new helper):"
    )
    lines.append("")
    lines.append('- direct `os.environ["X"]` / `os.environ.get("X", …)` / `os.getenv("X", …)`')
    lines.append(
        "- project env-read helpers: " + ", ".join(f"`{h}`" for h in sorted(KNOWN_ENV_HELPERS))
    )
    lines.append("")
    lines.append("| Variable | Read in | Alias/deprecation |")
    lines.append("|----------|---------|-------------------|")
    for name in sorted(reads):
        mods = ", ".join(f"`{m}`" for m in sorted(reads[name]))
        alias = aliases.get(name, "")
        lines.append(f"| `{name}` | {mods} | {alias} |")
    lines.append("")
    lines.append(f"_{len(reads)} variables._")
    lines.append("")
    lines.append("## Dynamically-constructed reads (resolved at runtime — see source)")
    lines.append("")
    if dynamic:
        lines.append(
            "These reads pass a non-literal name argument, so the concrete variable name "
            "is not statically resolvable:"
        )
        lines.append("")
        for mod, lineno, callee in sorted(dynamic):
            lines.append(f"- `{mod}:{lineno}` — `{callee}(<non-literal>)`")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the env-var registry.")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if the committed file is stale"
    )
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
        if current != generated:
            sys.stderr.write(
                "docs/env-vars.md is stale — regenerate with `python scripts/gen_env_registry.py`\n"
            )
            return 1
        return 0
    DOC_PATH.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
