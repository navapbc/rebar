"""Dependency-direction guard for the reconciler (ticket pokey-matte-flute).

The reconciler may depend INWARD on the rest of the engine only through two
well-defined seams: the reducer (read ticket state) and the shared event-append
module (write ticket events under the I2 filename + I5 lock). Any other engine
top-level import (ticket_output, ticket_reads, ticket_txn, ticket_graph, …) would
be an undeclared coupling — this test fails on it, statically, by AST.

Sibling ``rebar_reconciler.*`` modules and the stdlib are unrestricted.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE = REPO_ROOT / "src" / "rebar" / "_engine"
RECON = ENGINE / "rebar_reconciler"

# Engine top-level importable modules/packages (excluding the reconciler itself).
ENGINE_MODULES = {
    "event_append",
    "ticket_graph",
    "ticket_output",
    "ticket_reads",
    "ticket_reducer",
    "ticket_resolver",
    "ticket_txn",
}

# The only engine seams the reconciler is allowed to import.
ALLOWED = {"ticket_reducer", "event_append"}


def _imported_top_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # Only absolute imports name an engine module (level 0); relative
            # imports (level>0) are intra-package and irrelevant here.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_reconciler_inward_engine_deps_are_reducer_and_event_append_only() -> None:
    offenders: dict[str, set[str]] = {}
    for py in sorted(RECON.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        engine_imports = _imported_top_names(tree) & ENGINE_MODULES
        disallowed = engine_imports - ALLOWED
        if disallowed:
            offenders[str(py.relative_to(REPO_ROOT))] = disallowed
    assert not offenders, (
        "reconciler files import engine modules outside the allowed seams "
        f"({sorted(ALLOWED)}): {offenders}"
    )
