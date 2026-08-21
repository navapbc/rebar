#!/usr/bin/env python3
"""Generate the canonical ``rebar`` CLI help artifacts from the parser factories (RP-05 S2d).

The package help under ``src/rebar/_cli/help/*.txt`` is the CLI's stdout/stderr help
contract (served at runtime by :mod:`rebar._cli._help`). Historically those files were
hand-captured; this generator makes them DERIVED — one artifact per help-backed route in
the immutable route registry (:mod:`rebar._cli._registry`), rendered by resolving that
route's ``parser_factory`` and formatting its ``--help`` at the fixed S2a width. The grouped
``overview.txt`` is likewise derived from route order/visibility plus each parser's one-line
summary (its ``description``).

Two invariants make the artifacts deterministic and machine-checkable:

* The one byte-parity invariant preserved from the hand-captured era is the CAPITALIZED
  ``Usage:`` prefix. argparse renders a lowercase ``usage:``; this generator capitalizes it,
  and ``--check`` FAILS on any artifact whose usage line is lowercase.
* Every visible canonical command gets a NON-BLANK overview one-liner (its parser summary).
  A present-but-blank summary is a hard ``--check`` failure — a command whose factory was not
  enriched is never silently emitted.

``--check`` fails on any missing / stale / stray artifact, an unresolved factory, a
non-deterministic second render, a registry/parser census mismatch, or a blank summary.

Usage:
    python scripts/gen_cli_help.py            # (re)write src/rebar/_cli/help/*.txt
    python scripts/gen_cli_help.py --check    # exit non-zero if the committed artifacts drift
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELP_DIR = REPO_ROOT / "src" / "rebar" / "_cli" / "help"

# Compatibility entrypoints whose canonical children the bridge group already advertises;
# they keep a rendered artifact but are omitted from the grouped overview (mirrors the
# ``_OVERVIEW_ALLOWLIST`` in tests/interfaces/contracts/test_help_overview_coverage.py).
_OVERVIEW_OMIT: frozenset[str] = frozenset({"bridge-fsck", "bridge-probe"})

_OVERVIEW_HEADER = (
    "Usage: rebar <subcommand> [args...]\n"
    "\n"
    "Run 'rebar <subcommand> --help' for usage of a specific subcommand.\n"
    "\n"
    "Subcommands:\n"
)


def _routes() -> tuple:
    from rebar._cli._registry import ROUTES

    return ROUTES


def _help_backed(route) -> bool:
    """Routes that carry a pinned help artifact: every live route that is not an
    intercept-only advanced command and not a hidden alias spelling."""
    return route.group != "intercept" and not route.hidden and not route.retired


def _resolve_factory(ref: str):
    """Import and return the ``"module:attr"`` parser factory referenced by a route."""
    module_name, _, attr = ref.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _capitalize_usage(text: str) -> str:
    """Capitalize argparse's lowercase ``usage:`` prefix (the one byte-parity invariant)."""
    if text.startswith("usage: "):
        return "Usage: " + text[len("usage: ") :]
    return text


def _normalize(text: str) -> str:
    """Enforce the artifact byte policy: LF line endings, exactly one trailing newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def render_route(route) -> str:
    """Render one route's committed help artifact bytes from its parser factory."""
    factory = _resolve_factory(route.parser_factory)
    parser = factory(prog=f"rebar {route.name}")
    return _normalize(_capitalize_usage(parser.format_help()))


def _summary(route) -> str:
    """The route's overview one-liner: the first non-blank line of its parser description."""
    factory = _resolve_factory(route.parser_factory)
    parser = factory(prog=f"rebar {route.name}")
    description = (parser.description or "").strip()
    if not description:
        return ""
    return " ".join(description.splitlines()[0].split())


def render_overview() -> str:
    """Render the grouped ``overview.txt`` from route order/visibility plus parser summaries."""
    listed = [r for r in _routes() if _help_backed(r) and r.name not in _OVERVIEW_OMIT]
    width = max(len(r.name) for r in listed) + 2
    lines = [_OVERVIEW_HEADER.rstrip("\n")]
    for route in listed:
        lines.append(f"  {route.name.ljust(width)}{_summary(route)}".rstrip())
    return "\n".join(lines) + "\n"


def _expected() -> dict[str, str]:
    """The full committed artifact set: ``<name>.txt`` bytes plus ``overview.txt``."""
    artifacts: dict[str, str] = {}
    for route in _routes():
        if _help_backed(route):
            artifacts[f"{route.name}.txt"] = render_route(route)
    artifacts["overview.txt"] = render_overview()
    return artifacts


def _blank_summary_failures() -> list[str]:
    """Visible commands whose parser summary (overview one-liner) is blank — a hard failure."""
    return [
        route.name
        for route in _routes()
        if _help_backed(route) and route.name not in _OVERVIEW_OMIT and not _summary(route)
    ]


def _determinism_failures(expected: dict[str, str]) -> list[str]:
    """Artifacts whose second render does not reproduce the first (non-determinism)."""
    second = _expected()
    return sorted(name for name in expected if second.get(name) != expected[name])


def _check() -> int:
    blanks = _blank_summary_failures()
    if blanks:
        sys.stderr.write(
            "blank overview one-liner(s) — enrich the factory description for: "
            f"{', '.join(sorted(blanks))}\n"
        )
        return 1

    expected = _expected()

    nondeterministic = _determinism_failures(expected)
    if nondeterministic:
        sys.stderr.write(f"non-deterministic render for: {', '.join(nondeterministic)}\n")
        return 1

    present = {p.name for p in HELP_DIR.glob("*.txt")}
    missing = sorted(set(expected) - present)
    stray = sorted(present - set(expected))
    stale = sorted(
        name
        for name in expected
        if name not in missing and (HELP_DIR / name).read_text(encoding="utf-8") != expected[name]
    )
    if missing or stray or stale:
        if missing:
            sys.stderr.write(f"missing artifacts: {', '.join(missing)}\n")
        if stray:
            sys.stderr.write(f"stray artifacts: {', '.join(stray)}\n")
        if stale:
            sys.stderr.write(f"stale artifacts: {', '.join(stale)}\n")
        sys.stderr.write("regenerate with `python scripts/gen_cli_help.py`\n")
        return 1
    return 0


def _write() -> int:
    blanks = _blank_summary_failures()
    if blanks:
        sys.stderr.write(
            "refusing to write: blank overview one-liner(s) — enrich the factory "
            f"description for: {', '.join(sorted(blanks))}\n"
        )
        return 1
    expected = _expected()
    HELP_DIR.mkdir(parents=True, exist_ok=True)
    for name in {p.name for p in HELP_DIR.glob("*.txt")} - set(expected):
        (HELP_DIR / name).unlink()
    for name, text in expected.items():
        (HELP_DIR / name).write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the canonical CLI help artifacts.")
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero if the committed artifacts drift"
    )
    args = parser.parse_args(argv)
    return _check() if args.check else _write()


if __name__ == "__main__":
    raise SystemExit(main())
