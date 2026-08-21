#!/usr/bin/env python3
"""Generate ``docs/env-vars.md`` — the canonical registry of environment variables
read under ``src/rebar`` (audit maintainability #3).

The registry is DERIVED (like ``reviewers/index.json``): a CI drift gate regenerates it
and fails the build on any diff, so a new env-var read cannot ship undocumented.

Scope is defined RELATIVE to an explicit, enumerated set of read patterns (documented in
the generated file's header). Within ``os.environ`` the scan is now FAIL-CLOSED: every
attribute access on ``os.environ`` must be classified, and an unclassified one raises
``UnrecognisedEnvironAccess`` rather than being silently walked past (bug: a
``os.environ.pop("K")`` read used to drop ``K`` from the registry while the drift gate
stayed green, because the generator and the committed doc were blind in the same way).

  1. Key-bearing stdlib reads — the accessor returns the current value of ONE named
     variable, so its string-literal key is registered: the ``os.environ["X"]``
     subscript, ``os.environ.get/pop/setdefault("X", …)``
     (``KEY_BEARING_ENVIRON_ATTRS``), and ``os.getenv("X", …)``.
  2. Bulk/whole-mapping accesses (``BULK_ENVIRON_ATTRS``) — ``os.environ.copy()``,
     ``.items()``, ``.keys()``, ``.values()``, … name no single variable, so they are
     ALLOWED and register nothing.
  3. Project env-read helpers (``KNOWN_ENV_HELPERS``): the string-literal env-name
     argument is resolved at each call site (one level through the shim).

Deliberately NOT recognised (and why):
  * reads under ``tests/`` — outside the scan root by design; the registry documents the
    shipped ``src/rebar`` surface, not test fixtures.
  * non-literal keys (``os.environ.get(name)``) — the concrete name only exists at
    runtime, so these are REPORTED in the ``dynamic`` list instead of dropped.
  * ``os.environ`` passed by reference into another callable (``dict(os.environ)``,
    ``f(os.environ)``) — a bulk handoff with no literal key, indistinguishable from the
    allowed whole-mapping accesses; nothing to register.
  * ``getattr(os.environ, "get")(…)`` and similar string-indirection — not statically
    resolvable at all; a fail-closed AST scan cannot see it, so it is out of scope rather
    than pretended-covered.

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


class UnrecognisedEnvironAccess(RuntimeError):
    """Raised when ``os.environ`` is touched through an attribute the scan cannot classify.

    Fail-closed by design: an unknown accessor might be a key-bearing read (whose variable
    would then be silently missing from the registry while ``--check`` stayed green), so the
    generator refuses to emit a registry it knows to be incomplete. Fix by classifying the
    attribute into ``KEY_BEARING_ENVIRON_ATTRS`` or ``BULK_ENVIRON_ATTRS`` below.
    """


# ``os.environ.<attr>(...)`` accessors that RETURN the current value of ONE named variable.
# Their first positional argument is the variable name, so it is registered exactly like the
# ``os.environ["X"]`` subscript. ``pop``/``setdefault`` also mutate, but the read is what the
# registry cares about.
# ``__getitem__`` is here, not in the bulk set below: an explicit ``os.environ.__getitem__("X")``
# call is an ast.Call, NOT an ast.Subscript, so the subscript branch never sees it — treating it
# as a whole-mapping access would silently drop X, which is the very hole this scan closes.
KEY_BEARING_ENVIRON_ATTRS: frozenset[str] = frozenset({"get", "pop", "setdefault", "__getitem__"})

# Whole-mapping ``MutableMapping`` members: they name no single variable, so they are allowed
# and register nothing. Each is here for a concrete reason:
#   copy/items/keys/values — bulk reads of the whole mapping (``copy`` occurs under src/rebar
#     today, for building a child-process environment).
#   update/clear/popitem  — bulk WRITES/removals; nothing readable is named by a literal
#     (``popitem`` returns an arbitrary pair, not a requested variable).
#   __contains__/__iter__/__len__ — dunder forms reached only through explicit attribute
#     access; membership/iteration/length name no single variable.
#   __setitem__/__delitem__ — key-bearing but WRITE-only: they set or remove rather than
#     return a value, and the registry documents readable surface. (The ``os.environ["X"] = v``
#     Store subscript is registered by the Subscript branch; that predates this bug and is
#     left as-is deliberately.) The READ dunder ``__getitem__`` is key-bearing — see above.
BULK_ENVIRON_ATTRS: frozenset[str] = frozenset(
    {
        "copy",
        "items",
        "keys",
        "values",
        "update",
        "clear",
        "popitem",
        "__contains__",
        "__iter__",
        "__len__",
        "__setitem__",
        "__delitem__",
    }
)


# helper name -> (0-indexed position of the env-name argument, name prefix).
# Signatures verified against the current tree; extend by adding a row here.
KNOWN_ENV_HELPERS: dict[str, tuple[int, str]] = {
    "_rebar_env": (0, "REBAR_"),  # reconciler shim: os.environ.get(f"REBAR_{name}")
    "_env_int": (0, ""),  # outbound_differ.py, binding_store.py
    "_str_pref": (0, ""),  # llm/gate_source.py
    "_int_pref": (1, ""),  # _snapshot/janitor.py: (table, env_name, ...)
    "_llm_str": (2, ""),  # llm/config.py: (table, cli, env_name, ...)
    "_llm_int": (2, ""),  # llm/config.py: (table, cli, env_name, ...)
    # Omitting this one cost four undocumented vars (bug b00f): a helper absent from this table
    # is not an error, it is INVISIBLE — the scan walks past every call and the drift gate stays
    # green, so a clean `--check` proves agreement with the generator, never completeness.
    "_llm_float": (2, ""),  # llm/config.py: (table, cli, env_name, ...)
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


Reads = dict[str, set[str]]
Dynamic = list[tuple[str, int, str]]


def _record(reads: Reads, dynamic: Dynamic, rel: str, node: ast.Call, callee: str) -> None:
    """Register ``node``'s first positional argument as an env-var name, or report it as
    dynamic when it is not a string literal. Shared by every key-bearing read shape."""
    name = _str_literal(node.args[0]) if node.args else None
    if name:
        reads.setdefault(name, set()).add(rel)
    elif node.args:
        dynamic.append((rel, node.lineno, callee))


def _scan_call(node: ast.Call, rel: str, reads: Reads, dynamic: Dynamic) -> None:
    """Handle the call-shaped read patterns: ``os.environ.<key-bearing>(…)``,
    ``os.getenv(…)`` and the ``KNOWN_ENV_HELPERS`` shims. Bulk ``os.environ`` accessors
    fall through here without recording anything; CLASSIFICATION of every ``os.environ``
    attribute — including rejecting an unrecognised one — happens in ``_scan_module``,
    which sees the ``ast.Attribute`` node whether or not it is called."""
    func = node.func
    # os.environ.get/pop/setdefault("X", ...)
    if isinstance(func, ast.Attribute) and _is_os_environ(func.value):
        if func.attr in KEY_BEARING_ENVIRON_ATTRS:
            _record(reads, dynamic, rel, node, f"os.environ.{func.attr}")
        return
    # os.getenv("X", ...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        _record(reads, dynamic, rel, node, "os.getenv")
        return
    # project helper call: _rebar_env("X"), _llm_int(t, c, "X", ...), ...
    if isinstance(func, ast.Name) and func.id in KNOWN_ENV_HELPERS:
        pos, prefix = KNOWN_ENV_HELPERS[func.id]
        if len(node.args) > pos:
            lit = _str_literal(node.args[pos])
            if lit is not None:
                reads.setdefault(prefix + lit, set()).add(rel)
            else:
                dynamic.append((rel, node.lineno, func.id))


def _scan_module(tree: ast.Module, rel: str, reads: Reads, dynamic: Dynamic) -> Dynamic:
    """Walk one parsed module, filling ``reads``/``dynamic`` and RETURNING the list of
    unrecognised ``os.environ`` attribute sites as ``(rel, lineno, attr)``. Offending sites
    are collected rather than raised on the spot so ``scan`` can report every one at once."""
    offenders: Dynamic = []
    for node in ast.walk(tree):
        # os.environ["X"] (also the Store-context ``os.environ["X"] = v`` form)
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name = _str_literal(node.slice)
            if name:
                reads.setdefault(name, set()).add(rel)
        elif isinstance(node, ast.Attribute) and _is_os_environ(node.value):
            # Classify EVERY os.environ attribute access, called or not — this is the
            # fail-closed seam that stops a new accessor from silently dropping a variable.
            if node.attr not in KEY_BEARING_ENVIRON_ATTRS and node.attr not in BULK_ENVIRON_ATTRS:
                offenders.append((rel, node.lineno, node.attr))
        elif isinstance(node, ast.Call):
            _scan_call(node, rel, reads, dynamic)
    return offenders


def scan(root: Path) -> tuple[Reads, Dynamic]:
    """Return (reads, dynamic) where ``reads`` maps each resolved env-var name to the set
    of module paths (relative to the repo root) that read it, and ``dynamic`` lists
    (module, lineno, callee) for reads whose name argument is not a string literal.

    Raises ``UnrecognisedEnvironAccess`` if any ``os.environ`` attribute access under
    ``root`` is neither key-bearing nor bulk. The arity of this return value is
    load-bearing (``scripts/check_config_ownership.py`` unpacks a 2-tuple): the
    fail-closed signal is the exception, never a third element."""
    reads: Reads = {}
    dynamic: Dynamic = []
    offenders: Dynamic = []
    for py in sorted(root.rglob("*.py")):
        try:
            rel = py.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = py.as_posix()  # scan root outside the repo (e.g. a test temp dir)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        offenders.extend(_scan_module(tree, rel, reads, dynamic))
    if offenders:
        sites = "\n".join(
            f"  - os.environ.{attr} at {mod}:{lineno}" for mod, lineno, attr in offenders
        )
        raise UnrecognisedEnvironAccess(
            "unrecognised os.environ access(es) — classify each attribute into "
            "KEY_BEARING_ENVIRON_ATTRS (registers its literal key) or BULK_ENVIRON_ATTRS "
            f"(names no single variable) in {Path(__file__).name}:\n{sites}"
        )
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
        "recognized read patterns. Within `os.environ` the scan is fail-closed — an "
        "attribute access the generator cannot classify aborts the run instead of "
        "silently dropping a variable — but reads through other indirections are still "
        "not captured (extend `KNOWN_ENV_HELPERS` in the generator to cover a new helper):"
    )
    lines.append("")
    lines.append(
        "- key-bearing stdlib reads (the literal key is registered): "
        '`os.environ["X"]`, '
        + ", ".join(f'`os.environ.{a}("X", …)`' for a in sorted(KEY_BEARING_ENVIRON_ATTRS))
        + ', `os.getenv("X", …)`'
    )
    lines.append(
        "- bulk/whole-mapping accesses (allowed, register nothing — they name no single "
        "variable): " + ", ".join(f"`os.environ.{a}`" for a in sorted(BULK_ENVIRON_ATTRS))
    )
    lines.append(
        "- project env-read helpers: " + ", ".join(f"`{h}`" for h in sorted(KNOWN_ENV_HELPERS))
    )
    lines.append(
        "- NOT recognized: reads under `tests/` (outside the scan root), non-literal keys "
        "(reported as dynamic below instead of dropped), `os.environ` passed by reference "
        "into another callable (`dict(os.environ)`, `f(os.environ)`), and "
        "`getattr(os.environ, …)` indirection."
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
