"""Byte-exact help/usage/error text for the argparse CLI.

The text is the *contract* the bash dispatcher established (its hand-rolled
``_print_overview`` / ``_print_subcommand_help`` ``echo`` strings). To preserve it
across the Tier E cutover without transcription drift, the canonical strings ship
as package data under ``rebar/_cli/help/`` — byte-for-byte copies of the captured
dispatcher output — and are pinned by goldens in ``tests/golden/cli_help``. This
module only loads and renders them; it never reformats.

Streams matter (the dispatcher distinguishes them, so the goldens do too):

* no-args / ``rebar help`` / ``rebar --help`` → overview to **stdout**.
* ``rebar <known> --help`` / ``rebar help <known>`` → that help to **stdout**, exit 0.
* unknown subcommand (``rebar frobnicate``) → error to **stderr** + overview to
  **stdout**, exit 1.
* ``rebar help <unknown>`` / ``rebar <unknown> --help`` → error + blank + overview
  all to **stderr**, exit 1.
"""

from __future__ import annotations

import importlib.resources
from functools import lru_cache

# The canonical help text lives as package data so it ships in the wheel/editable
# install (hatchling includes all files under ``src/rebar``). One file per key:
# ``overview.txt`` and ``<subcommand>.txt``.
_PKG = "rebar._cli.help"


@lru_cache(maxsize=None)
def _load(name: str) -> str | None:
    """Return the raw bytes-as-text of ``help/<name>.txt``, or ``None`` if absent.

    Read as UTF-8 with no newline translation so the stored bytes (including the
    trailing newline the dispatcher's ``echo`` emitted) reproduce exactly.
    """
    try:
        res = importlib.resources.files(_PKG) / f"{name}.txt"
        return res.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None


def overview() -> str:
    """The full subcommand overview (``_print_overview`` in the dispatcher)."""
    text = _load("overview")
    # Package data is always present in a real install; fall back defensively.
    return text if text is not None else "Usage: rebar <subcommand> [args...]\n"


def subcommand_help(sub: str) -> str | None:
    """Per-subcommand usage text, or ``None`` when ``sub`` is unknown."""
    return _load(sub)


def known_subcommands() -> frozenset[str]:
    """The set of subcommands that have pinned help text (the dispatcher's arms)."""
    try:
        names = {
            p.name[:-4]
            for p in importlib.resources.files(_PKG).iterdir()
            if p.name.endswith(".txt") and p.name != "overview.txt"
        }
    except (ModuleNotFoundError, OSError):
        names = set()
    return frozenset(names)
