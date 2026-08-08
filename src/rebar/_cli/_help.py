"""Byte-exact help/usage/error text for the argparse CLI.

The text is the CLI's stdout/stderr contract (historically the bash dispatcher's
hand-rolled ``_print_overview`` / ``_print_subcommand_help`` ``echo`` strings; see
``docs/bash-migration.md`` §7). The canonical strings ship as package data under
``rebar/_cli/help/`` — byte-for-byte copies of the captured output, pinned by the
golden tests. This module only loads and renders them; it never reformats.

Streams matter (the goldens pin stdout vs stderr per case):

* no-args / ``rebar help`` / ``rebar --help`` → overview to **stdout**.
* ``rebar <known> --help`` / ``rebar help <known>`` → that help to **stdout**, exit 0.
* unknown subcommand (``rebar frobnicate``) → error to **stderr** + overview to
  **stdout**, exit 1.
* ``rebar help <unknown>`` / ``rebar <unknown> --help`` → error + blank + overview
  all to **stderr**, exit 1.
"""

from __future__ import annotations

import importlib.resources
from functools import cache

# The canonical help text lives as package data so it ships in the wheel/editable
# install (hatchling includes all files under ``src/rebar``). One file per key:
# ``overview.txt`` and ``<subcommand>.txt``.
_PKG = "rebar._cli.help"


@cache
def _load(name: str) -> str | None:
    """Return the raw bytes-as-text of ``help/<name>.txt``, or ``None`` if absent.

    Read as UTF-8 with no newline translation so the stored bytes (including the
    trailing newline the captured golden ends with) reproduce exactly.
    """
    try:
        res = importlib.resources.files(_PKG) / f"{name}.txt"
        return res.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def overview() -> str:
    """The full subcommand overview."""
    text = _load("overview")
    # Package data is always present in a real install; fall back defensively.
    return text if text is not None else "Usage: rebar <subcommand> [args...]\n"


def subcommand_help(sub: str) -> str | None:
    """Per-subcommand usage text, or ``None`` when ``sub`` is unknown."""
    return _load(sub)


def known_subcommands() -> frozenset[str]:
    """The set of subcommands that have pinned help text."""
    try:
        names = {
            p.name[:-4]
            for p in importlib.resources.files(_PKG).iterdir()
            if p.name.endswith(".txt") and p.name != "overview.txt"
        }
    except (ModuleNotFoundError, OSError):
        names = set()
    return frozenset(names)
