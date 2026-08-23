#!/usr/bin/env python3
"""Generate the canonical ``rebar`` CLI help artifacts from the parser factories (RP-05 S2d).

The package help under ``src/rebar/_cli/help/*.txt`` is the CLI's stdout and stderr help
contract. This generator derives one artifact for each visible route that is not retired in
the immutable route registry (:mod:`rebar._cli._registry`). It resolves each route's
``parser_factory`` and formats its ``--help`` at the fixed S2a width. It also derives
``overview.txt`` from route order, visibility, and each parser's ``description``.

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
import re
import sys
from functools import partial
from pathlib import Path
from typing import cast

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

_INTERCEPT_HELP_WIDTH = 80


def _routes() -> tuple:
    from rebar._cli._registry import ROUTES

    return ROUTES


def _help_backed(route) -> bool:
    """Return whether a route is visible, not retired, and carries committed help."""
    return not route.hidden and not route.retired


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


def _unwrap_usage(text: str) -> str:
    """Join an argparse generated usage block into one stable line."""
    lines = text.split("\n")
    end = 0
    while end < len(lines) and lines[end].strip() != "":
        end += 1
    if end == 0:
        return text
    joined = " ".join([lines[0], *(line.strip() for line in lines[1:end])]).rstrip()
    return "\n".join([joined, *lines[end:]])


def _collapse_metavars(line: str) -> str:
    """Render repeated option metavars once for stable Python version output."""
    if not re.match(r"^\s+--?\S", line):
        return line
    previous = None
    while previous != line:
        previous = line
        line = re.sub(
            r"(--?[\w-]+) (\S+), (--?[\w-]+) \2(?=[,\s]|$)",
            r"\1, \3 \2",
            line,
            count=1,
        )
    return line


def _pin_intercept_width(parser: argparse.ArgumentParser) -> None:
    """Pin stdlib formatter output to the canonical help width."""
    formatter_class = cast(type[argparse.HelpFormatter], parser.formatter_class)
    parser.formatter_class = partial(formatter_class, width=_INTERCEPT_HELP_WIDTH)


def _normalize(text: str) -> str:
    """Enforce the artifact byte policy: LF line endings, exactly one trailing newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


def render_route(route) -> str:
    """Render one route's committed help artifact bytes from its parser factory."""
    factory = _resolve_factory(route.parser_factory)
    parser = factory(prog=f"rebar {route.name}")
    if route.group == "intercept":
        _pin_intercept_width(parser)
    text = _capitalize_usage(parser.format_help())
    if route.group == "intercept":
        if parser.usage is None:
            text = _unwrap_usage(text)
        text = "\n".join(_collapse_metavars(line) for line in text.split("\n"))
    return _normalize(text)


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
