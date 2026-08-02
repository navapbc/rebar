"""cc77: the core reconciler's transport parameters must be typed AS THE PORT,
through an import path mypy can RESOLVE.

Three spellings are equivalent to no annotation at all under ``mypy src/rebar``,
and this module fails on each of them:

1. a bare ``client`` / ``transport`` parameter — mypy infers nothing;
2. ``client: Any`` — satisfies ``disallow_untyped_defs`` (so promoting ``_engine``
   through the strictness ratchet would ACCEPT it) while staying completely blind;
3. ``client: TicketTransport`` where ``TicketTransport`` was imported from
   ``rebar_reconciler._backend``.

(3) is the one a reviewer will not see, and it is the reason this file exists.
``rebar_reconciler`` is not an importable distribution — it is a top-level name
created at runtime by injecting ``src/rebar/_engine`` onto ``sys.path``. mypy is
never told about that injection, and ``ignore_missing_imports = true`` is set
repo-wide, so every name reached through that prefix is widened to ``Any``.
Measured in one file, so only the import path varies::

    from rebar_reconciler._backend import TicketTransport as TT_shim
    from ._backend                import TicketTransport as TT_rel

    def via_shim(a: TT_shim) -> None:
        reveal_type(a)                  # -> "Any"
        a.definitely_not_a_member("x")  # -> NO ERROR

    def via_relative(b: TT_rel) -> None:
        reveal_type(b)  # -> "rebar._engine.rebar_reconciler._backend.TicketTransport"
        b.definitely_not_a_member("x")  # -> error: ... has no attribute ... [attr-defined]
        b.set_entity_property("K-1", "p", 1)  # -> NO ERROR (no false positive)

So a diff can show twenty correct-looking port annotations and enforce nothing,
and ``make typecheck`` stays green because it was never going to say anything.
That is the defect class behind [rebar:a357-b747-ece9-4cf5] — a writing reconcile
pass crashing on ``set_entity_property``, a method the core calls from three sites
and the port never declared — arriving by a second route.

This is a STATIC-side guard. It does not replace the runtime conformance guard
(``assert_transport_conforms``) or the AST audit; all three catch different things.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE = (
    pathlib.Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"
)

#: Parameter names that carry the transport port through the core.
PORT_PARAMS = ("client", "transport")

#: The ONE deliberate ``Any``. ``assert_transport_conforms`` is the runtime guard
#: itself: its whole job is to inspect an object that may NOT conform and say so.
#: Annotating its parameter as the port would assert precisely the property it
#: exists to check. Keyed by (module, function) so it cannot silently widen to
#: cover a second site.
DELIBERATE_ANY = {("_backend.py", "assert_transport_conforms")}


def _core_modules() -> list[pathlib.Path]:
    """Core reconciler modules. ``adapters/`` implement the port rather than
    consume it, and are out of this story's scope."""
    return sorted(p for p in CORE.glob("*.py") if p.name != "__init__.py")


def _shim_bound_names(tree: ast.Module) -> set[str]:
    """Names this module bound through the unresolvable ``rebar_reconciler.`` prefix.

    Includes ``TYPE_CHECKING``-guarded imports: the widening to ``Any`` is a
    type-checking-time effect, so guarding the import does not avoid it.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "rebar_reconciler" or mod.startswith("rebar_reconciler."):
                bound.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "rebar_reconciler" or a.name.startswith("rebar_reconciler."):
                    bound.add((a.asname or a.name).split(".")[0])
    return bound


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
def test_port_params_are_annotated_as_a_resolvable_port(module: pathlib.Path) -> None:
    tree = ast.parse(module.read_text())
    shim_names = _shim_bound_names(tree)
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
            continue

        via_shim = sorted(names & shim_names)
        if via_shim:
            failures.append(
                f"{where} names {via_shim} imported through `rebar_reconciler.` — an "
                f"import path mypy cannot resolve, so `ignore_missing_imports` widens "
                f"it to `Any` and the annotation enforces NOTHING. Import the port "
                f"relatively (`from ._backend import ...`) instead."
            )

    assert not failures, "\n".join(["port-typing violations:", *failures])


def test_the_only_deliberate_any_is_the_runtime_guard_itself() -> None:
    """Pin the exemption set so it cannot quietly become an allowlist.

    Every entry must name a function that still exists and still takes an
    ``Any``-annotated port parameter — otherwise the exemption is stale and the
    next author would inherit a hole nobody is checking.
    """
    assert DELIBERATE_ANY == {("_backend.py", "assert_transport_conforms")}

    tree = ast.parse((CORE / "_backend.py").read_text())
    guard = next(
        f for f, p, a in _port_params(tree) if f == "assert_transport_conforms" and p == "transport"
    )
    arg = next(a for f, p, a in _port_params(tree) if f == guard and p == "transport")
    assert arg.annotation is not None
    assert "Any" in _annotation_names(arg.annotation)


def test_the_port_is_reachable_by_a_relative_import_from_a_core_module() -> None:
    """The remedy the other test prescribes must actually be available.

    ``_backend`` imports nothing inside the package, so a relative import of it
    from any core module is cycle-free by construction. If that ever stopped
    being true the prescribed fix would become impossible and this test says so
    before the other one starts producing unfixable failures.
    """
    tree = ast.parse((CORE / "_backend.py").read_text())
    intra = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rebar_reconciler")
    }
    intra |= {n.level for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level}
    assert not intra, f"_backend gained intra-package imports ({intra}); relative import may cycle"
