"""Core transport parameters must name the resolvable TicketTransport port.

Bare parameters and Any hide missing members from mypy. The port can be
imported from core modules without a cycle. Static typing complements the
runtime conformance check and AST audit."""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parents[3] / "src" / "rebar" / "reconciler"

#: Parameter names that carry the transport port through the core.
PORT_PARAMS = ("client", "transport")

#: The runtime conformance guard accepts Any because it must inspect objects
#: that may not implement TicketTransport. Keep its exemption keyed to one site.
DELIBERATE_ANY = {("_backend.py", "assert_transport_conforms")}


def _core_modules() -> list[pathlib.Path]:
    """Core reconciler modules. ``adapters/`` implement the port rather than
    consume it, and are out of this story's scope."""
    return sorted(p for p in CORE.glob("*.py") if p.name != "__init__.py")


def _annotation_names(annotation: ast.expr) -> set[str]:
    """Every bare identifier in an annotation, including inside a string forward
    reference and through ``X | None`` / ``Optional[X]`` wrappers."""
    names: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                names |= _annotation_names(ast.parse(node.value, mode="eval").body)
            except SyntaxError:  # pragma: no cover — not a type expression
                pass
    return names


def _port_params(tree: ast.Module) -> list[tuple[str, str, ast.arg]]:
    """(function name, parameter name, arg node) for every port-shaped parameter."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for arg in args:
                if arg.arg in PORT_PARAMS:
                    found.append((node.name, arg.arg, arg))
    return found


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_port_params_are_annotated_as_the_port(module: pathlib.Path) -> None:
    tree = ast.parse(module.read_text())
    failures: list[str] = []

    for func, param, arg in _port_params(tree):
        where = f"{module.name}::{func}({param})"
        exempt = (module.name, func) in DELIBERATE_ANY

        if arg.annotation is None:
            failures.append(
                f"{where} is BARE. mypy infers nothing, so a call to any member — "
                f"declared or not — is unchecked. Annotate it as the port."
            )
            continue

        names = _annotation_names(arg.annotation)

        if "Any" in names and not exempt:
            failures.append(
                f"{where} is annotated `Any`. That satisfies `disallow_untyped_defs` "
                f"(the mypy strictness ratchet would ACCEPT it) while remaining as "
                f"blind as no annotation at all. Annotate it as the port."
            )

    assert not failures, "\n".join(["port-typing violations:", *failures])


def test_the_only_deliberate_any_is_the_runtime_guard_itself() -> None:
    """Only the runtime conformance guard may keep a deliberate Any transport parameter."""
    assert DELIBERATE_ANY == {("_backend.py", "assert_transport_conforms")}

    tree = ast.parse((CORE / "_backend.py").read_text())
    guard = next(
        f for f, p, a in _port_params(tree) if f == "assert_transport_conforms" and p == "transport"
    )
    arg = next(a for f, p, a in _port_params(tree) if f == guard and p == "transport")
    assert arg.annotation is not None
    assert "Any" in _annotation_names(arg.annotation)


def test_the_port_is_importable_from_any_core_module_without_a_cycle() -> None:
    """Every core module can import TicketTransport without a cycle or unresolved type."""
    tree = ast.parse((CORE / "_backend.py").read_text())
    intra = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rebar.reconciler")
    }
    intra |= {n.level for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level}
    assert not intra, f"_backend gained intra-package imports ({intra}); importing it may cycle"
