#!/usr/bin/env python3
"""CI guard: freeze the internal import-cycle surface and prevent it GROWING.

WHY THIS EXISTS
---------------
A static import-graph analysis of ``src/rebar`` finds a large strongly-connected
component (SCC) — a web of modules that (transitively) import one another, held
together largely by function-local "lazy" imports across the ``rebar`` facade,
``_commands``, and most of ``llm``. We are deliberately NOT untangling that here.

Like the module-size gate (``.github/module-size-allowlist.txt``), this is a
**strangler** guard: it snapshots the CURRENT cycle surface into
``.import-cycle-baseline.json`` and fails CI only when a change makes the tangle
WORSE. Untangling (shrinking the baseline) is always welcome; growth is not.

WHAT IT CHECKS
--------------
All three are measured from grimp's static import graph, which DOES capture
function-local / lazy imports — precisely the edges that hold the SCC together:

  1. ``largest_scc_size`` — the module count of the biggest import cycle must not
     grow. This stops the existing tangle from absorbing more modules.
  2. ``cross_package_cycle_pairs`` — the set of unordered ``{pkgA, pkgB}`` top-level
     package pairs that sit together in some import cycle must not gain a NEW pair.
     A new pair means a new *cross-package* cycle (a currently-clean package such as
     ``graph`` or ``reducer`` being drawn into the tangle). Churn of edges *within*
     the existing surface is fine — only a genuinely new cross-package cycle fails.
  3. ``forbidden_layer_imports`` — a small set of NAMED architectural invariants that
     hold today and must never regress (see FORBIDDEN_LAYER_IMPORTS). These are
     layering rules independent of whether a cycle has formed yet; e.g. ``reducer``
     (the pure event-replay layer) must never import "up" into
     ``_commands`` / ``_engine_support`` / ``llm`` / …  Encoding it stops the exact
     inversion that was just fixed from silently returning. Unlike (1)/(2) these are
     absolute — they are not baselined and must always be empty.

The guard PASSES the current tree by construction (the committed baseline was
generated from it). It fails ONLY on regression; an *improvement* exits 0 and just
prints a hint to ratchet the baseline down.

HOW TO MAINTAIN
---------------
* You made the graph BETTER (untangled something) and the guard prints an
  "improved" hint: re-run with ``--update`` to ratchet the baseline down, and commit
  ``.import-cycle-baseline.json``. The guard never auto-tightens.
* You believe you genuinely need a NEW cross-package edge that forms a new cycle:
  first ask whether it can be avoided (that is the whole point of the gate). If it is
  truly required, run ``--update`` and commit — the baseline diff is the reviewable
  record of the new tangle, exactly like adding a file to the module-size allowlist.
  Every change to ``main`` is LLM-reviewed via Gerrit, so a baseline that loosens is
  a visible, reviewed act.
* To add a named layering invariant, extend ``FORBIDDEN_LAYER_IMPORTS`` below.

Fast (grimp builds the src/rebar graph in ~0.02s) and dependency-light: the only
dependency is ``grimp`` (a [dev] dep — it is the engine import-linter runs on).

Usage
-----
    python scripts/check_import_cycles.py           # check; exit 1 on regression
    python scripts/check_import_cycles.py --update   # regenerate the baseline
    python scripts/check_import_cycles.py --json      # print current state as JSON
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

# The internal package whose import graph we analyse.
ROOT_PACKAGE = "rebar"

# Baseline snapshot lives next to pyproject.toml (repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / ".import-cycle-baseline.json"

# ── Named layering invariants (the "forbidden contract" half of the guard) ──────
# Absolute rules that hold on the current tree and must never regress. Each key is a
# top-level unit (see :func:`top_package`) that must NOT import any of the listed
# top-level units. ``reducer`` is the pure event-replay layer: it may only reach
# genuine leaves (``_ids`` / ``_alias``) and must never reach "up" into the command,
# engine-support, or LLM layers. (The reducer -> _engine_support inversion was just
# fixed; this pins it fixed.) Add rules here as further invariants are established.
FORBIDDEN_LAYER_IMPORTS: Mapping[str, frozenset[str]] = {
    "reducer": frozenset(
        {
            "_engine_support",
            "_commands",
            "_cli",
            "llm",
            "graph",
            "grounding",
            "review_bot",
            "mcp_server",
            "config",
            "signing",
            "_io",
            "_reads",
            "_store",
            "_snapshot",
        }
    ),
}


def top_package(module: str) -> str:
    """Return the top-level unit a fully-qualified module belongs to.

    ``rebar.llm.workflow.foo`` -> ``llm``; ``rebar.signing`` -> ``signing``; the
    facade module ``rebar`` (the package ``__init__``) -> ``rebar``. This is the
    granularity at which "cross-package" cycles are judged.
    """
    parts = module.split(".")
    return parts[1] if len(parts) >= 2 else parts[0]


def strongly_connected_components(
    adjacency: Mapping[str, Iterable[str]],
) -> list[list[str]]:
    """Tarjan's SCC algorithm (iterative, so it never hits the recursion limit).

    ``adjacency`` maps a node to the nodes it points at (directed edges). Returns a
    list of components; a component with more than one node — or a single node with a
    self-edge — is a cycle.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in adjacency:
        if root in index_of:
            continue
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[str, Iterable[str]]] = [(root, iter(adjacency.get(root, ())))]
        while work:
            node, successors = work[-1]
            descended = False
            for succ in successors:
                if succ not in index_of:
                    index_of[succ] = lowlink[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(adjacency.get(succ, ()))))
                    descended = True
                    break
                if succ in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[succ])
            if descended:
                continue
            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    component.append(popped)
                    if popped == node:
                        break
                result.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return result


def _package_pair_key(a: str, b: str) -> str:
    """Stable, order-independent key for an unordered pair of top-level units."""
    lo, hi = sorted((a, b))
    return f"{lo}::{hi}"


def analyze(adjacency: Mapping[str, Iterable[str]]) -> dict:
    """Reduce an import graph to the ratcheted cycle metrics.

    Returns ``largest_scc_size``, the sorted ``cross_package_cycle_pairs`` (unordered
    top-level package pairs that co-occur in some cycle), and, for diagnostics,
    ``total_modules_in_cycles`` and ``num_cyclic_sccs``.
    """
    cyclic = [c for c in strongly_connected_components(adjacency) if len(c) > 1]
    largest = max((len(c) for c in cyclic), default=0)
    modules_in_cycles = sum(len(c) for c in cyclic)
    pairs: set[str] = set()
    for component in cyclic:
        tops = sorted({top_package(m) for m in component})
        for i in range(len(tops)):
            for j in range(i + 1, len(tops)):
                pairs.add(_package_pair_key(tops[i], tops[j]))
    return {
        "largest_scc_size": largest,
        "cross_package_cycle_pairs": sorted(pairs),
        "total_modules_in_cycles": modules_in_cycles,
        "num_cyclic_sccs": len(cyclic),
    }


def forbidden_layer_violations(
    adjacency: Mapping[str, Iterable[str]],
) -> list[tuple[str, str]]:
    """Return (importer, imported) edges that break a named layering invariant."""
    violations: list[tuple[str, str]] = []
    for importer, imported_modules in adjacency.items():
        banned = FORBIDDEN_LAYER_IMPORTS.get(top_package(importer))
        if not banned:
            continue
        for imported in imported_modules:
            if top_package(imported) in banned:
                violations.append((importer, imported))
    return sorted(violations)


def compare(current: Mapping, baseline: Mapping) -> tuple[list[str], list[str]]:
    """Compare current metrics to the baseline.

    Returns ``(regressions, improvements)`` as human-readable strings. A non-empty
    ``regressions`` list means the guard must fail.
    """
    regressions: list[str] = []
    improvements: list[str] = []

    cur_size = current["largest_scc_size"]
    base_size = baseline["largest_scc_size"]
    if cur_size > base_size:
        regressions.append(
            f"largest import cycle grew: {base_size} -> {cur_size} modules "
            f"(a change pulled {cur_size - base_size} more module(s) into the tangle)"
        )
    elif cur_size < base_size:
        improvements.append(f"largest import cycle shrank: {base_size} -> {cur_size} modules")

    cur_pairs = set(current["cross_package_cycle_pairs"])
    base_pairs = set(baseline["cross_package_cycle_pairs"])
    new_pairs = sorted(cur_pairs - base_pairs)
    gone_pairs = sorted(base_pairs - cur_pairs)
    if new_pairs:
        regressions.append(
            "new cross-package import cycle(s) — these package pairs are now "
            "co-cyclic and were not before: " + ", ".join(new_pairs)
        )
    if gone_pairs:
        improvements.append("cross-package cycles removed: " + ", ".join(gone_pairs))
    return regressions, improvements


def build_adjacency() -> dict[str, list[str]]:
    """Build the internal import adjacency for ``ROOT_PACKAGE`` via grimp.

    grimp does static AST analysis (no import side effects) and, by default, includes
    only internal modules — so every edge here is a ``rebar.*`` -> ``rebar.*`` edge,
    including function-local/lazy imports.
    """
    import grimp  # local import so the pure functions above stay grimp-free (testable)

    # cache_dir=None disables grimp's on-disk cache: the build is ~0.02s, and a
    # ``.grimp_cache/`` dropped in the CWD would pollute the checkout (and trip the
    # test suite's repo-leak guard).
    graph = grimp.build_graph(ROOT_PACKAGE, cache_dir=None)
    return {
        module: sorted(graph.find_modules_directly_imported_by(module)) for module in graph.modules
    }


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text())


def save_baseline(metrics: Mapping) -> None:
    payload = {
        "_comment": (
            "Strangler baseline for scripts/check_import_cycles.py - the FROZEN "
            "import-cycle surface of src/rebar. The guard fails CI if largest_scc_size "
            "grows or a NEW cross-package pair appears. Shrinking is welcome: re-run "
            "`python scripts/check_import_cycles.py --update` and commit. See the "
            "script docstring for why the current SCC is grandfathered."
        ),
        "root_package": ROOT_PACKAGE,
        "largest_scc_size": metrics["largest_scc_size"],
        "cross_package_cycle_pairs": metrics["cross_package_cycle_pairs"],
        "total_modules_in_cycles": metrics["total_modules_in_cycles"],
        "num_cyclic_sccs": metrics["num_cyclic_sccs"],
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate .import-cycle-baseline.json from the current tree",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the current metrics as JSON and exit 0",
    )
    args = parser.parse_args(argv)

    adjacency = build_adjacency()
    current = analyze(adjacency)
    forbidden = forbidden_layer_violations(adjacency)

    if args.json:
        print(json.dumps(current, indent=2))
        return 0

    if args.update:
        save_baseline(current)
        print(f"Updated {BASELINE_PATH.name}:")
        print(json.dumps(current, indent=2))
        if forbidden:
            print(
                "\nWARNING: --update does NOT baseline forbidden layering imports; "
                "these are absolute and still fail:"
            )
            for importer, imported in forbidden:
                print(f"  {importer} -> {imported}")
            return 1
        return 0

    baseline = load_baseline()
    regressions, improvements = compare(current, baseline)

    print(
        f"Import-cycle guard: largest SCC = {current['largest_scc_size']} modules, "
        f"{len(current['cross_package_cycle_pairs'])} cross-package cycle pair(s), "
        f"{current['total_modules_in_cycles']} module(s) in cycles."
    )

    # Absolute layering invariants (not baselined).
    if forbidden:
        print("::error::forbidden layering import(s) detected — a lower layer reached up:")
        for importer, imported in forbidden:
            print(f"  {importer} -> {imported}")

    for note in improvements:
        print(f"  improved: {note}")
    if improvements and not regressions:
        print(
            "  hint: the import graph improved — run "
            "`python scripts/check_import_cycles.py --update` to ratchet the baseline down."
        )

    if regressions or forbidden:
        print("::error::import-cycle regression — the guard blocks this change:")
        for note in regressions:
            print(f"  {note}")
        if forbidden:
            print("  a forbidden layering import (above) reintroduced an architectural inversion.")
        print(
            "\nDo not grow the import tangle. Break the new edge, or — if the growth is "
            "genuinely required — run `python scripts/check_import_cycles.py --update`, "
            "commit the baseline diff, and expect it to be reviewed."
        )
        return 1

    print("Import-cycle guard: OK (no regression vs the frozen baseline).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
