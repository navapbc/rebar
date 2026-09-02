#!/usr/bin/env python3
"""Shrink-only mechanism-delta ratchet (ticket 9ca8-675e-4dfb-427d, unblacked-loveless-toad).

56% of sampled fixes add a new mechanism (a lock, a knob, an env var, a gate script, a
fixture, a helper, a flag) against 30% that are pure logic fixes, so the surface that
produces future defect classes grows with every fix cycle — and nothing in the repository
pushed back on that growth. This is the counter-pressure: a shrink-only ratchet over a
committed per-``(kind, name)`` baseline, modelled directly on
``scripts/check_complexity_baseline.py``, whose four-bucket ``compare()`` and
``has_regression`` are ported essentially unchanged because they are proven.

The gate does NOT forbid new mechanisms. It forbids UNJUSTIFIED ones: a new mechanism fails
``--check`` until an author either removes it or writes down, at the definition site, why it
had to exist. Removal is always allowed (it buckets as ``stale``), which is the asymmetry
that makes it a ratchet rather than a freeze.

The seven kinds
---------------
``lock``, ``env_var``, ``config_key``, ``feature_flag``, ``ci_gate``, ``autouse_fixture``,
``test_helper``. They PARTITION the surface: every definition site yields exactly ONE
``(kind, name)`` entry. Two consequences are load-bearing rather than incidental:

  * ``feature_flag`` claims the boolean-coerced ``_SECTIONS`` entries and ``config_key``
    claims only the non-boolean remainder. Counting a boolean key as both would demand two
    justifications for one definition site, which a per-kind marker cannot express.
  * ``config_key``/``feature_flag`` names are SECTION-QUALIFIED (``<section>.<key>``), never
    the bare key. ``_SECTIONS`` repeats key names across sections — ``allow_insecure`` in
    both ``reconciler`` and ``jira``, ``threshold`` in both ``ticket_clarity`` and
    ``compact`` — so a bare-key baseline would silently merge four definition sites into two
    entries, and a fifth could then be added for free.

The marker
----------
::

    # mechanism-ok: <kind> <name> — <reason or ticket id>

It admits EXACTLY the ``(kind, name)`` it names, never its whole kind. A blank reason is
itself an error — an unexplained marker is indistinguishable from a rubber stamp. Placement
follows the detection shape (see ``scripts/_mechanism_delta/markers.py``).

Portability
-----------
Stdlib plus PyYAML (already a dev dep, read-only). No CI provider is required or assumed:
``--check`` runs identically from ``make lint``, a pre-commit hook, or a bare shell, so a
checkout with no CI at all still gets the gate (``project.portability``).

Exit-code contract
------------------
  * ``--check`` — prints the complexity baseline's summary shape
    ``active=<A> new=<N> increased=<I> stale=<S>``, then one detail line per unadmitted
    regression and one per admitted mechanism. Exits 0 only when every ``new``/``increased``
    mechanism carries a non-blank marker for its exact key and no marker anywhere is blank.
    ``stale>0`` alone always passes — a REMOVED mechanism is the outcome the ratchet wants.
  * ``--update-stale`` — drops baseline entries whose definition site is gone and rewrites
    canonical sorted JSON. REFUSES to write (nonzero, baseline byte-identical) while
    ``new>0`` or ``increased>0``, so it can never bless a regression into the baseline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Reach the private detector subpackage the way the sibling scripts do: an insert derived
# from this file's own ``__file__``, so the script still imports standalone with the scripts
# directory stripped from ``sys.path`` (scripts/check_import_walk.py's contract).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _mechanism_delta import (  # noqa: E402
    KINDS,
    Counters,
    MarkerMap,
    SchemaError,
    compare,
    drain_stale,
    evaluate,
    harvest,
    parse_baseline,
    render_baseline,
    scan_sites,
)

# ``fsutil`` is a stdlib-only leaf; import the canonical atomic writer rather than
# re-implementing the temp-in-same-dir + os.replace dance (as the complexity gate does).
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
from rebar._store.fsutil import atomic_write  # noqa: E402

BASELINE_PATH = ".github/mechanism-baseline.json"
MARKER = "mechanism-ok:"

__all__ = [
    "BASELINE_PATH",
    "KINDS",
    "MARKER",
    "Counters",
    "compare",
    "detect_all",
    "evaluate",
    "main",
    "markers_for",
    "parse_baseline",
    "render_baseline",
]


def detect_all(repo_root: Path | str) -> dict[str, set[str]]:
    """Return ``{kind: {name, ...}}`` for the seven kinds, detected live from the tree."""
    return {kind: {name for name, _p, _l in sites} for kind, sites in scan_sites(repo_root).items()}


def markers_for(repo_root: Path | str) -> MarkerMap:
    """Return ``{"<kind>::<name>": reason}`` for every in-tree ``# mechanism-ok:`` marker.

    Only locations a detector actually reported are scanned, so a marker cannot be parked
    somewhere unrelated to any mechanism and still count.
    """
    markers: MarkerMap = {}
    for sites in scan_sites(repo_root).values():
        harvest(sites, markers)
    return markers


def census(repo_root: Path | str) -> dict[str, int]:
    """Flatten :func:`detect_all` into the baseline's ``{"<kind>::<name>": 1}`` shape."""
    return {f"{kind}::{name}": 1 for kind, names in detect_all(repo_root).items() for name in names}


def _scan_once(repo_root: Path) -> tuple[dict[str, int], MarkerMap]:
    """The census and the marker map from ONE detector pass.

    :func:`census` and :func:`markers_for` are each independently useful (and independently
    tested), but running both would walk the tree twice; the commands use this instead.
    """
    sites = scan_sites(repo_root)
    current = {
        f"{kind}::{name}": 1 for kind, kind_sites in sites.items() for name, _p, _l in kind_sites
    }
    markers: MarkerMap = {}
    for kind_sites in sites.values():
        harvest(kind_sites, markers)
    return current, markers


def load_baseline(path: Path) -> dict[str, int]:
    """Read and validate the committed baseline document."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SchemaError(f"baseline file not found: {path}") from exc
    return parse_baseline(raw)


def _run_check(repo_root: Path) -> int:
    try:
        baseline = load_baseline(repo_root / BASELINE_PATH)
    except SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    current, markers = _scan_once(repo_root)
    code, lines = evaluate(current, baseline, markers)
    for line in lines:
        print(line)
    if code:
        print(
            "hint: this gate bounds MECHANISM GROWTH, not code changes — remove the new "
            f"mechanism, or justify it in place with '# {MARKER} <kind> <name> — <reason>'."
        )
    return code


def _run_update_stale(repo_root: Path) -> int:
    path = repo_root / BASELINE_PATH
    try:
        baseline = load_baseline(path)
    except SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    current = census(repo_root)
    counters = compare(current, baseline)
    if counters.has_regression:
        print(
            f"refusing to update: the tree has new/increased mechanisms ({counters.summary})",
            file=sys.stderr,
        )
        return 1
    drained = drain_stale(current, baseline)
    atomic_write(path, render_baseline(drained))
    print(compare(current, drained).summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_mechanism_delta.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help=(
            "Gate mode. Detect every mechanism in the tree and compare against the "
            "committed baseline. Exit 0 only when no mechanism is new or increased without "
            "a non-blank '# mechanism-ok:' marker for its exact key. stale>0 alone is an "
            "allowed improvement (exit 0)."
        ),
    )
    group.add_argument(
        "--update-stale",
        action="store_true",
        help=(
            "Maintenance rewrite. Drop baseline entries whose definition site is gone, then "
            "atomically write canonical sorted JSON. REFUSES to write (nonzero) while any "
            "mechanism is new or increased, so it can never bless a regression."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.update_stale:
        return _run_update_stale(REPO_ROOT)
    return _run_check(REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
