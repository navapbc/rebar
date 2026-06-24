"""Compute deterministic adjective-noun-noun aliases from ticket IDs.

Stdlib-only leaf (imports no ``rebar`` subpackage) so both ``rebar.reducer``
(``_processors`` create path) and ``rebar._engine_support.resolver`` (read-time
backfill) import it directly — breaking the former ``reducer ↔ _engine_support``
lazy-import cycle. Moved here from ``rebar/reducer/_alias.py``.

Used as a read-time fallback for tickets created before the alias feature
shipped (their CREATE event has no `data.alias`). Mirrors the algorithm in
`ticket-alias-compute.py` so legacy tickets surface the same alias they
would have been assigned at creation.
"""

from __future__ import annotations

import os
import sys

_WORDS_CACHE: tuple[list[str], list[str]] | None = None
_WARNED_MISSING: bool = False


def _wordlist_path() -> str:
    """Resolve the bundled wordlist path (self-resolved; not a user knob).

    This module lives at ``<rebar>/_alias.py`` (a top-of-tree, stdlib-only leaf so
    both ``rebar.reducer`` and ``rebar._engine_support.resolver`` can import it
    without a package cycle); the bundled wordlist ships
    with the engine at ``<rebar>/_engine/resources/ticket-wordlist.txt``. The
    in-process library and engine subprocesses resolve to this same path (engine
    subprocesses no longer receive a TICKET_WORDLIST_PATH handoff)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "_engine", "resources", "ticket-wordlist.txt"))


def _load() -> tuple[list[str], list[str]]:
    """Load and cache the (adjs, nouns) tuple. Mirrors the file-format and
    fallback behaviour of ticket-alias-compute.py so backfilled aliases match
    aliases stored at creation time byte-for-byte.

    When the wordlist file cannot be opened, emits a one-shot WARN to stderr
    (matching the shell helper's "FALLBACK" stderr signal) and returns empty
    lists — caller then falls back to the 8-hex alias. Silent fallback hides
    a real misconfiguration; the diagnostic is printed exactly once per
    process to avoid log flood under bulk invocation."""
    global _WORDS_CACHE, _WARNED_MISSING
    if _WORDS_CACHE is not None:
        return _WORDS_CACHE
    adjs: list[str] = []
    nouns: list[str] = []
    section = "adj"
    path = _wordlist_path()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line == "# NOUNS":
                    section = "noun"
                    continue
                if line.startswith("#") or not line.strip():
                    continue
                (adjs if section == "adj" else nouns).append(line.strip())
    except OSError as exc:
        if not _WARNED_MISSING:
            print(
                f"WARN: ticket-wordlist.txt unavailable at {path!r} ({exc}); "
                "falling back to 8-hex aliases (check the install ships the "
                "bundled engine resources).",
                file=sys.stderr,
            )
            _WARNED_MISSING = True
    _WORDS_CACHE = (adjs, nouns)
    return _WORDS_CACHE


def compute_alias(ticket_id: str) -> str | None:
    """Return the alias for `ticket_id`, or None if the wordlist is unavailable.

    Returns the same string `ticket-alias-compute.py` would print for the same
    ticket_id and wordlist. Falls back to the first 8 hex chars (no dash) when
    the wordlist is empty/missing — matching the shell-side fallback.
    """
    hex_id = ticket_id.replace("-", "")
    if len(hex_id) < 8:
        return None
    adjs, nouns = _load()
    if not adjs or not nouns:
        return hex_id[: min(len(hex_id), 8)]
    try:
        adj = adjs[int(hex_id[0:4], 16) % len(adjs)]
        n1 = nouns[int(hex_id[4:8], 16) % len(nouns)]
    except ValueError:
        return None
    # Legacy 8-hex tickets get a 2-word alias (adj-noun); 16-hex get adj-noun-noun.
    if len(hex_id) >= 12:
        try:
            n2 = nouns[int(hex_id[8:12], 16) % len(nouns)]
        except ValueError:
            return f"{adj}-{n1}"
        return f"{adj}-{n1}-{n2}"
    return f"{adj}-{n1}"
