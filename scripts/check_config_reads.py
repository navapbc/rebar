#!/usr/bin/env python3
"""Config-is-read DET check (ticket 6754): every _config_schema.py dataclass field
must have at least one attribute-read site outside the schema/config plumbing, or
carry a ``# read-via: <pointer>`` escape marker.

A field counts as read only when some ``ast.Attribute`` load whose terminal name
equals the field name has a RECEIVER that resolves to the dataclass DECLARING that
field (bug 2c58): a global set of attribute names satisfied a field from any
same-named attribute on any unrelated object, so an inert field passed whenever its
name was not globally unique.

Receiver resolution binds an expression to a set of candidate schema classes via:
  1. section-attribute names (``Config.mcp: McpConfig`` makes ``*.mcp`` / ``mcp``
     resolve to ``McpConfig``),
  2. parameter / ``AnnAssign`` annotations naming a schema class,
  3. local alias assignments (``m = cfg.mcp``), propagated to a fixed point,
  4. function return annotations (``load_config() -> Config``) for call expressions,
  5. ``IfExp`` / ``BoolOp`` branches (union of the branch resolutions),
  6. cross-module parameter binding — an unannotated parameter inherits the
     resolution of the arguments passed at its call sites.

An expression that resolves to nothing contributes to no field.

API contract:
  - check(schema_path: Path, root: Path) -> list[str]
  - main(argv: list[str] | None = None) -> int
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = REPO_ROOT / "src" / "rebar" / "_config_schema.py"
DEFAULT_ROOT = REPO_ROOT / "src" / "rebar"

# Matches ``# read-via: <pointer>`` — group(1) is the pointer (may be empty/whitespace).
_MARKER_RE = re.compile(r"#\s*read-via:(.*)")

# Plumbing filenames excluded from the read-site scan.
_PLUMBING = {"_config_schema.py", "config.py"}

# Safety bound on the binding fixed-point iterations.
_MAX_PASSES = 8

_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)

Path_ = tuple[str, ...]


def _is_dataclass(node: ast.ClassDef) -> bool:
    """Return True if the class has a @dataclass (or @dataclasses.dataclass) decorator."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        if isinstance(dec, ast.Call):
            inner = dec.func
            if isinstance(inner, ast.Name) and inner.id == "dataclass":
                return True
            if isinstance(inner, ast.Attribute) and inner.attr == "dataclass":
                return True
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
    return False


def _collect_fields(schema_path: Path) -> list[tuple[str, str, int]]:
    """Return ``[(class_name, field_name, lineno), ...]`` for every AnnAssign field
    in a ``@dataclass`` class."""
    tree = _parse(schema_path)
    if tree is None:
        return []
    fields: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not _is_dataclass(node):
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                fields.append((node.name, item.target.id, item.lineno))
    return fields


def _check_marker(lines: list[str], lineno: int) -> tuple[bool, str]:
    """Check for a ``# read-via:`` marker on the field's own line or the preceding line.

    Returns ``(found, pointer)`` where ``found`` is True when a marker is present,
    and ``pointer`` is the text after the colon (stripped). When no marker is found,
    ``found`` is False and ``pointer`` is ``""``.
    """
    candidates = [lineno - 1]  # own line (0-indexed)
    if lineno >= 2:
        candidates.append(lineno - 2)  # immediately preceding line (0-indexed)
    for idx in candidates:
        if idx < 0 or idx >= len(lines):
            continue
        m = _MARKER_RE.search(lines[idx])
        if m:
            return True, m.group(1).strip()
    return False, ""


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def _annotation_names(node: ast.expr | None) -> set[str]:
    """Return the bare names mentioned by an annotation expression.

    Unwraps string annotations, ``X | None`` unions and ``Optional[X]``-style
    subscripts so an annotated binding is never missed (widening only).
    """
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            inner = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return set()
        return _annotation_names(inner)
    if isinstance(node, ast.BinOp):
        return _annotation_names(node.left) | _annotation_names(node.right)
    if isinstance(node, ast.Subscript):
        return _annotation_names(node.value) | _annotation_names(node.slice)
    if isinstance(node, ast.Tuple):
        out: set[str] = set()
        for elt in node.elts:
            out |= _annotation_names(elt)
        return out
    return set()


def _dotted(node: ast.expr) -> Path_ | None:
    """Return the dotted name tuple for a Name/Attribute chain, else None."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return tuple(reversed(parts))
    return None


def _walk(node: ast.AST, stack: tuple[str, ...] = ()) -> Iterator[tuple[ast.AST, tuple[str, ...]]]:
    """Yield ``(node, enclosing_function_name_stack)`` for the whole subtree."""
    for child in ast.iter_child_nodes(node):
        child_stack = stack
        if isinstance(child, _FUNC_DEFS):
            child_stack = (*stack, child.name)
        yield child, child_stack
        yield from _walk(child, child_stack)


def _schema_model(schema_path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(class_fields, section_map)`` for the schema module.

    ``class_fields`` maps a schema dataclass name to its declared field names.
    ``section_map`` maps a section-attribute name to the schema classes it names
    (``Config.mcp: McpConfig`` -> ``{"mcp": {"McpConfig"}}``).
    """
    tree = _parse(schema_path)
    class_fields: dict[str, set[str]] = {}
    annotated: list[tuple[str, set[str]]] = []
    if tree is None:
        return class_fields, {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_dataclass(node):
            continue
        names = class_fields.setdefault(node.name, set())
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                names.add(item.target.id)
                annotated.append((item.target.id, _annotation_names(item.annotation)))
    section_map: dict[str, set[str]] = {}
    for field_name, ann in annotated:
        hits = ann & class_fields.keys()
        if hits:
            section_map.setdefault(field_name, set()).update(hits)
    return class_fields, section_map


class _Module:
    """One parsed source file plus the bindings discovered inside it."""

    def __init__(self, path: Path, tree: ast.Module, *, plumbing: bool) -> None:
        self.path = path
        self.tree = tree
        self.plumbing = plumbing
        self.env: dict[Path_, set[str]] = {}
        self.assigns: list[tuple[Path_, ast.expr, tuple[str, ...]]] = []
        self.calls: list[tuple[ast.Call, tuple[str, ...]]] = []
        self.attributes: list[tuple[ast.Attribute, tuple[str, ...]]] = []
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node, stack in _walk(tree):
            if isinstance(node, _FUNC_DEFS):
                self.functions.append(node)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    dotted = _dotted(target)
                    if dotted is not None:
                        self.assigns.append((dotted, node.value, stack))
            elif isinstance(node, ast.AnnAssign):
                dotted = _dotted(node.target)
                if dotted is not None and node.value is not None:
                    self.assigns.append((dotted, node.value, stack))
            elif isinstance(node, ast.Call):
                self.calls.append((node, stack))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                self.attributes.append((node, stack))

    def bind(self, key: Path_, classes: set[str]) -> bool:
        """Add ``classes`` to the binding for ``key``; return True if it grew."""
        if not classes:
            return False
        current = self.env.setdefault(key, set())
        before = len(current)
        current |= classes
        return len(current) != before


def _iter_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.arg]:
    a = fn.args
    yield from a.posonlyargs
    yield from a.args
    yield from a.kwonlyargs
    if a.vararg is not None:
        yield a.vararg
    if a.kwarg is not None:
        yield a.kwarg


class _Resolver:
    """Binds receiver expressions to the schema classes they may denote."""

    def __init__(self, class_fields: dict[str, set[str]], section_map: dict[str, set[str]]) -> None:
        self.classes = set(class_fields)
        self.section_map = section_map
        self.func_returns: dict[str, set[str]] = {}
        self.positional: dict[str, set[Path_]] = {}
        self.param_names: dict[str, set[str]] = {}
        self.param_env: dict[tuple[str, str], set[str]] = {}
        self.modules: list[_Module] = []

    # ---- construction -------------------------------------------------

    def add_module(self, module: _Module) -> None:
        self.modules.append(module)
        for fn in module.functions:
            returns = _annotation_names(fn.returns) & self.classes
            if returns:
                self.func_returns.setdefault(fn.name, set()).update(returns)
            params = list(_iter_params(fn))
            self.positional.setdefault(fn.name, set()).add(
                tuple(arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args))
            )
            self.param_names.setdefault(fn.name, set()).update(arg.arg for arg in params)
            for arg in params:
                module.bind((arg.arg,), _annotation_names(arg.annotation) & self.classes)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.AnnAssign):
                dotted = _dotted(node.target)
                if dotted is not None:
                    module.bind(dotted, _annotation_names(node.annotation) & self.classes)

    # ---- resolution ---------------------------------------------------

    def resolve(self, module: _Module, node: ast.expr, stack: tuple[str, ...]) -> set[str]:
        if isinstance(node, ast.Name):
            out = set(module.env.get((node.id,), ()))
            for fname in stack:
                out |= self.param_env.get((fname, node.id), set())
            out |= self.section_map.get(node.id, set())
            return out
        if isinstance(node, ast.Attribute):
            out = set(self.section_map.get(node.attr, set()))
            dotted = _dotted(node)
            if dotted is not None:
                out |= module.env.get(dotted, set())
            return out
        if isinstance(node, ast.Call):
            fn = node.func
            name: str | None = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            return set(self.func_returns.get(name, set())) if name else set()
        if isinstance(node, ast.IfExp):
            branches = (node.body, node.orelse)
            return set().union(*(self.resolve(module, b, stack) for b in branches))
        if isinstance(node, ast.BoolOp):
            out = set()
            for value in node.values:
                out |= self.resolve(module, value, stack)
            return out
        return set()

    # ---- fixed point --------------------------------------------------

    def _propagate_assigns(self) -> bool:
        changed = False
        for module in self.modules:
            for target, value, stack in module.assigns:
                if module.bind(target, self.resolve(module, value, stack)):
                    changed = True
        return changed

    def _propagate_calls(self) -> bool:
        changed = False
        for module in self.modules:
            for call, stack in module.calls:
                fn = call.func
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                else:
                    continue
                if name not in self.param_names:
                    continue
                for signature in self.positional.get(name, set()):
                    for arg, param in zip(call.args, signature, strict=False):
                        if isinstance(arg, ast.Starred):
                            break
                        if self._bind_param(name, param, self.resolve(module, arg, stack)):
                            changed = True
                for kw in call.keywords:
                    if kw.arg is None or kw.arg not in self.param_names[name]:
                        continue
                    if self._bind_param(name, kw.arg, self.resolve(module, kw.value, stack)):
                        changed = True
        return changed

    def _bind_param(self, func: str, param: str, classes: set[str]) -> bool:
        if not classes:
            return False
        current = self.param_env.setdefault((func, param), set())
        before = len(current)
        current |= classes
        return len(current) != before

    def solve(self) -> None:
        for _ in range(_MAX_PASSES):
            changed = self._propagate_assigns()
            changed = self._propagate_calls() or changed
            if not changed:
                return

    # ---- reads --------------------------------------------------------

    def reads(self) -> set[tuple[str, str]]:
        """Return ``{(class_name, attribute_name), ...}`` for every resolved read."""
        found: set[tuple[str, str]] = set()
        for module in self.modules:
            if module.plumbing:
                continue
            for node, stack in module.attributes:
                for cls in self.resolve(module, node.value, stack):
                    found.add((cls, node.attr))
        return found


def _collect_reads(schema_path: Path, root: Path) -> set[tuple[str, str]]:
    """Return the ``(class, field)`` pairs actually read outside the plumbing."""
    class_fields, section_map = _schema_model(schema_path)
    if not class_fields:
        return set()
    resolver = _Resolver(class_fields, section_map)
    for py in sorted(root.rglob("*.py")):
        tree = _parse(py)
        if tree is None:
            continue
        resolver.add_module(_Module(py, tree, plumbing=py.name in _PLUMBING))
    resolver.solve()
    return resolver.reads()


def check(schema_path: Path, root: Path) -> list[str]:
    """Return a list of error strings; ``[]`` means the tree is clean."""
    fields = _collect_fields(schema_path)
    if not fields:
        return []

    source_lines = schema_path.read_text(encoding="utf-8").splitlines()
    reads = _collect_reads(schema_path, root)
    errors: list[tuple[int, str]] = []  # (lineno, message) for sorting

    for cls_name, field_name, lineno in fields:
        found, pointer = _check_marker(source_lines, lineno)
        if found:
            if not pointer:
                errors.append((
                    lineno,
                    f"{schema_path}:{lineno}: {cls_name}.{field_name}: bare "
                    f"'# read-via:' marker — a pointer/reason is required "
                    f"(e.g. '# read-via: path/to/reader.py description').",
                ))
            # valid marker → ok
        else:
            if (cls_name, field_name) in reads:
                pass  # at least one read site found
            else:
                errors.append((
                    lineno,
                    f"{schema_path}:{lineno}: {cls_name}.{field_name} has no "
                    f"attribute-read site outside the schema/config plumbing. "
                    f"Add a read site, or add a '# read-via: <pointer>' marker "
                    f"on the field line (or the immediately preceding line) to "
                    f"document where it is consumed.",
                ))

    errors.sort(key=lambda t: t[0])
    return [msg for _, msg in errors]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every _config_schema.py dataclass field has a read site.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="Path to _config_schema.py (default: %(default)s)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help="Source root to scan for read sites (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    errors = check(args.schema, args.root)
    if errors:
        for err in errors:
            print(err)
        print(
            f"\n{len(errors)} field(s) lack a read site. "
            f"Add a '# read-via: <pointer>' marker to document indirect access."
        )
        return 1

    print(f"Config-read gate: OK ({args.schema.name} — all dataclass fields are read).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
