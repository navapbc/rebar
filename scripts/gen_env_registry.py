#!/usr/bin/env python3
"""Generate the canonical ``docs/env-vars.md`` registry from shipped environment reads.

The drift gate compares this derived output so new reads cannot ship undocumented. Recognized
patterns are:

1. literal-key ``os.environ`` subscripts and get/pop/setdefault/``__getitem__`` calls, plus
   ``os.getenv``;
2. classified whole-mapping reads/writes, which name no variable and register nothing; and
3. project helpers in ``KNOWN_ENV_HELPERS``, whose literal key argument is attributed at each
   call site.

The scan fails closed for an unclassified ``os.environ`` attribute, an env-read helper missing
from the table, or a table row whose helper is absent from the shipped tree. Non-literal keys
and keys derived from runtime state are reported as dynamic. Tests are outside the scan root;
passing the mapping wholesale has no key to register; and ``getattr`` string indirection is
not statically visible.

Environment aliases come from ``rebar._deprecations.REGISTRY``. Config-resolved aliases and
derived ``REBAR_MCP_*`` variables from ``MCP_ENV_VARS`` are unioned explicitly because they
have no literal read site.

Usage:
    python scripts/gen_env_registry.py            # regenerate docs/env-vars.md
    python scripts/gen_env_registry.py --check     # exit non-zero if the committed file is stale
"""

from __future__ import annotations

import argparse
import ast
import functools
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN_ROOT = REPO_ROOT / "src" / "rebar"
DOC_PATH = REPO_ROOT / "docs" / "env-vars.md"


class UnrecognisedEnvironAccess(RuntimeError):
    """An ``os.environ`` attribute is neither key-bearing nor a whole-mapping operation."""


class UnregisteredEnvReadHelper(RuntimeError):
    """An env-read helper derives its key from a parameter but has no helper-table row.

    Its concrete names exist only at call sites, so the row must give the zero-based key
    argument position. This failure remains distinct from an unknown ``os.environ`` accessor.
    """


class StaleEnvReadHelperRow(RuntimeError):
    """A helper-table row has no definition under the shipped scan root.

    This is the table-to-tree half of the invariant. Delete or re-key the row explicitly;
    automatic pruning could hide a rename and retain a false ownership exemption.
    """


# Accessors whose first argument names one readable variable. ``__getitem__`` is included
# because an explicit call is an ``ast.Call``, not the subscript shape handled separately.
KEY_BEARING_ENVIRON_ATTRS: frozenset[str] = frozenset({"get", "pop", "setdefault", "__getitem__"})

# Whole-mapping reads, writes, iteration, and write-only dunders name no readable variable and
# register nothing. Store-context ``os.environ["X"] = value`` remains handled as a subscript.
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


# Helper name -> zero-based env-name argument position. Missing helpers and stale rows both
# abort, so this table must stay bidirectionally aligned with the shipped tree.
KNOWN_ENV_HELPERS: dict[str, int] = {
    "_llm_str": 2,  # llm/config.py: (table, cli, env_name, ...)
    "_llm_str_source": 2,  # llm/config.py: (table, cli, env_name, ...) -> (value, source)
    "_llm_int": 2,  # llm/config.py: (table, cli, env_name, ...)
    "_llm_float": 2,  # llm/config.py: (table, cli, env_name, ...)
    "_int_env": 0,  # review_bot/config.py
    "_str_env": 0,  # opcert_service/config.py: os.environ.get(name)
    "_gate_str_pref": 0,  # _config_resolvers.py: (env_name, file_key, default, root=None)
    "read_secret_env": 0,  # config.py: (env_name)
    "_env_truthy": 0,  # llm/config.py: (name)
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
        pos = KNOWN_ENV_HELPERS[func.id]
        if len(node.args) > pos:
            lit = _str_literal(node.args[pos])
            if lit is not None:
                reads.setdefault(lit, set()).add(rel)
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


# Fail closed when a function derives an environment key from a caller-supplied parameter.

FuncDef = ast.FunctionDef | ast.AsyncFunctionDef
# (helper name, module path relative to the repo root, line of its ``def``).
HelperSites = list[tuple[str, str, int]]


def _param_names(fn: FuncDef) -> set[str]:
    """Every name bound as a parameter of ``fn`` — positional-only, positional-or-keyword,
    keyword-only, plus ``*args``/``**kwargs``. A key expression touching ANY of these is
    caller-supplied, which is exactly what makes the function a helper."""
    a = fn.args
    names = {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    names.update(extra.arg for extra in (a.vararg, a.kwarg) if extra is not None)
    return names


def _own_body_nodes(fn: FuncDef) -> list[ast.AST]:
    """Nodes in ``fn``'s OWN body, NOT descending into a nested ``def``/``lambda``. A nested
    function is judged separately against its own parameters (``ast.walk`` over the module
    reaches it independently), so this both attributes each read to the right function and
    keeps a single site from being reported twice."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _call_key_expr(node: ast.Call) -> ast.expr | None:
    """The env-name argument of a key-bearing CALL, or ``None`` when the call names no single
    variable. Same shapes ``_scan_call`` registers, expressed as the raw expression so the
    caller can ask what it is BUILT FROM rather than only whether it is a literal."""
    func = node.func
    if isinstance(func, ast.Attribute) and _is_os_environ(func.value):
        if func.attr in KEY_BEARING_ENVIRON_ATTRS and node.args:
            return node.args[0]
        return None
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return node.args[0] if node.args else None
    if isinstance(func, ast.Name) and func.id in KNOWN_ENV_HELPERS:
        pos = KNOWN_ENV_HELPERS[func.id]
        if len(node.args) > pos:
            return node.args[pos]
    return None


def _key_expr(node: ast.AST) -> ast.expr | None:
    """The key expression of any key-bearing read shape — the ``os.environ[...]`` subscript
    included — or ``None`` for everything else."""
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        return node.slice
    if isinstance(node, ast.Call):
        return _call_key_expr(node)
    return None


def _is_env_read_helper(fn: FuncDef) -> bool:
    """True when a key-bearing read in ``fn``'s own body keys off one of ``fn``'s parameters.
    An f-string built from a parameter counts (any ``ast.Name`` inside the key expression is
    enough); an inline string literal does NOT — that key is already captured at the read
    site — and neither does a key from some other runtime source (a regex match group), which
    stays genuinely dynamic."""
    params = _param_names(fn)
    if not params:
        return False
    for node in _own_body_nodes(fn):
        key = _key_expr(node)
        if key is None:
            continue
        if any(isinstance(sub, ast.Name) and sub.id in params for sub in ast.walk(key)):
            return True
    return False


def _unregistered_helpers(tree: ast.Module, rel: str) -> HelperSites:
    """Every env-read helper defined in this module that is missing from ``KNOWN_ENV_HELPERS``.
    Collected rather than raised on the spot so ``scan`` can report the whole tree at once."""
    return [
        (node.name, rel, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name not in KNOWN_ENV_HELPERS
        and _is_env_read_helper(node)
    ]


def _raise_for_environ(offenders: Dynamic) -> None:
    if not offenders:
        return
    sites = "\n".join(f"  - os.environ.{attr} at {mod}:{lineno}" for mod, lineno, attr in offenders)
    raise UnrecognisedEnvironAccess(
        "unrecognised os.environ access(es) — classify each attribute into "
        "KEY_BEARING_ENVIRON_ATTRS (registers its literal key) or BULK_ENVIRON_ATTRS "
        f"(names no single variable) in {Path(__file__).name}:\n{sites}"
    )


def _raise_for_helpers(helpers: HelperSites) -> None:
    if not helpers:
        return
    sites = "\n".join(f"  - {name} defined at {mod}:{lineno}" for name, mod, lineno in helpers)
    raise UnregisteredEnvReadHelper(
        "unregistered env-read helper(s) — each reads the environment under a key taken from "
        "its own parameter, so its variable names live at its CALL SITES and are invisible "
        "until it is registered. Add a row to KNOWN_ENV_HELPERS in "
        f"{Path(__file__).name} giving the 0-indexed position of the env-name argument"
        f":\n{sites}"
    )


@functools.lru_cache(maxsize=1)
def _shipped_function_names() -> frozenset[str]:
    """Every function name defined anywhere under ``DEFAULT_SCAN_ROOT``.

    Deliberately resolved against ``DEFAULT_SCAN_ROOT`` and NEVER against ``scan``'s ``root``
    argument — the same root-independence ``check_config_ownership._check_registry_completeness``
    uses, and for the same reason. ``KNOWN_ENV_HELPERS`` describes the shipped ``src/rebar``
    surface, but ``scan(root)`` accepts an arbitrary tree (a test temp dir, say), against which
    every row would look stale.

    A name counts when ``ast.walk`` finds a ``FunctionDef``/``AsyncFunctionDef`` for it — the
    same notion of "defined" ``_unregistered_helpers`` applies in the other direction, so nested
    defs and methods count.
    """
    names: set[str] = set()
    for py in DEFAULT_SCAN_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        names.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        )
    return frozenset(names)


def _raise_for_stale_rows() -> None:
    """Abort when any ``KNOWN_ENV_HELPERS`` row names a helper the shipped tree no longer
    defines. Every stale row is reported at once, like the other two seams."""
    defined = _shipped_function_names()
    stale = sorted(name for name in KNOWN_ENV_HELPERS if name not in defined)
    if not stale:
        return
    rel = DEFAULT_SCAN_ROOT.relative_to(REPO_ROOT).as_posix()
    rows = "\n".join(f"  - {name} (row: {KNOWN_ENV_HELPERS[name]!r})" for name in stale)
    raise StaleEnvReadHelperRow(
        f"stale KNOWN_ENV_HELPERS row(s) — no function of this name is defined under {rel}/, so "
        "the row documents a helper that no longer exists: it publishes a dead name into "
        "docs/env-vars.md and exempts that callee shape from the config-ownership shim seam. "
        f"Delete the row from {Path(__file__).name} (or, if the helper was renamed, re-key the "
        f"row to its new name):\n{rows}"
    )


def scan(root: Path) -> tuple[Reads, Dynamic]:
    """Return (reads, dynamic) where ``reads`` maps each resolved env-var name to the set
    of module paths (relative to the repo root) that read it, and ``dynamic`` lists
    (module, lineno, callee) for reads whose name argument is not a string literal.

    Raises ``UnrecognisedEnvironAccess`` if any ``os.environ`` attribute access under
    ``root`` is neither key-bearing nor bulk, and ``UnregisteredEnvReadHelper`` if any
    function under ``root`` reads the environment under a key derived from its own
    parameter without a ``KNOWN_ENV_HELPERS`` row. Raises ``StaleEnvReadHelperRow`` if any
    ``KNOWN_ENV_HELPERS`` row names a helper with no definition under ``DEFAULT_SCAN_ROOT`` —
    that third check validates the table against the SHIPPED surface it describes, never
    against ``root``. The arity of this return value is load-bearing
    (``render`` below and the tests unpack a 2-tuple): the fail-closed signal is the
    exception, never a third element."""
    _raise_for_stale_rows()
    reads: Reads = {}
    dynamic: Dynamic = []
    offenders: Dynamic = []
    helpers: HelperSites = []
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
        helpers.extend(_unregistered_helpers(tree, rel))
    _raise_for_environ(offenders)
    _raise_for_helpers(helpers)
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
    # Config aliases have no literal read, so add their real, settable surface explicitly.
    for alias_name in aliases:
        reads.setdefault(alias_name, set()).add("src/rebar/config.py (alias resolver)")
    # Derived REBAR_MCP_* keys likewise come from the canonical MCP_ENV_VARS inventory.
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
        "recognized read patterns. The scan is fail-closed on both halves: an `os.environ` "
        "attribute access the generator cannot classify aborts the run instead of silently "
        "dropping a variable, and so does an unregistered project env-read helper — a "
        "function that reads the environment under a key taken from its own parameter must "
        "have a `KNOWN_ENV_HELPERS` row, because its variable names live at its call sites. "
        "Neither kind of read can therefore go missing here while the drift gate stays green:"
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
        "into another callable (`dict(os.environ)`, `f(os.environ)`), `getattr(os.environ, …)` "
        "indirection, and keys built from a runtime source other than a parameter (a regex "
        "match group, say) — no call site carries those names, so they stay dynamic."
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
