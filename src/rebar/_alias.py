"""Deterministic legacy and genesis aliases derived from ticket IDs.

This stdlib-only leaf avoids a reducer/engine-support import cycle. Its legacy
algorithm matches ``ticket-alias-compute.py`` so tickets without a persisted CREATE
alias receive the same read-time backfill.
"""

from __future__ import annotations

import os
import sys

_WORDS_CACHE: tuple[list[str], list[str]] | None = None
_WARNED_MISSING: bool = False

# v2 (adjective-adjective-animal) genesis-alias state. Kept separate from the
# legacy (adjective-noun-noun) caches above so the legacy read-time backfill path
# is untouched: pre-existing tickets that recompute their alias on read continue
# to surface exactly the alias they were assigned. Only NEW tickets (via the
# create composer) use the v2 generator, and they persist that alias onto the
# CREATE event, so the new format is locked in at genesis.
_WORDS_V2_CACHE: tuple[list[str], list[str]] | None = None
_WARNED_MISSING_V2: bool = False


def _wordlist_path() -> str:
    """Resolve the bundled engine wordlist without a user or child-env override."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "_engine", "resources", "ticket-wordlist.txt"))


def _load() -> tuple[list[str], list[str]]:
    """Cache legacy adjective/noun sections using the creation-time file format.

    An unreadable wordlist warns once per process and returns empty lists, which
    selects the compatible eight-hex fallback without flooding bulk runs.
    """
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


def _wordlist_v2_path() -> str:
    """Absolute path to the bundled v2 (adjective-adjective-animal) wordlist."""
    return os.path.join(os.path.dirname(__file__), "_engine", "resources", "ticket-wordlist-v2.txt")


def _load_v2() -> tuple[list[str], list[str]]:
    """Load + cache the v2 wordlist as ``(adjectives, animals)``.

    Same two-section text format as the legacy loader but split on a ``# ANIMALS``
    marker (adjectives before it, animals after). Comment (``#``) and blank lines
    are skipped. On a missing/unreadable file it warns once to stderr and returns
    empty lists (callers fall back to a hex alias).
    """
    global _WORDS_V2_CACHE, _WARNED_MISSING_V2
    if _WORDS_V2_CACHE is not None:
        return _WORDS_V2_CACHE
    adjs: list[str] = []
    animals: list[str] = []
    section = "adj"
    try:
        with open(_wordlist_v2_path(), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line == "# ANIMALS":
                    section = "animals"
                    continue
                if not line or line.startswith("#"):
                    continue
                (adjs if section == "adj" else animals).append(line)
    except OSError:
        if not _WARNED_MISSING_V2:
            print(
                "WARN: ticket-wordlist-v2.txt unavailable; falling back to hex alias",
                file=sys.stderr,
            )
            _WARNED_MISSING_V2 = True
    _WORDS_V2_CACHE = (adjs, animals)
    return _WORDS_V2_CACHE


def compute_genesis_alias(ticket_id: str) -> str | None:
    """Return a persisted ``adjective-adjective-animal`` alias for a new ticket.

    The first three four-hex groups select both adjectives and the animal; a
    duplicate adjective advances once. Fewer than 12 hex characters returns
    ``None``, and an unavailable wordlist yields up to eight hex characters.
    Legacy read-time aliases continue through :func:`compute_alias`.
    """
    hex_id = ticket_id.replace("-", "")
    if len(hex_id) < 12:
        return None
    adjs, animals = _load_v2()
    if not adjs or not animals:
        return hex_id[: min(len(hex_id), 8)]
    try:
        i1 = int(hex_id[0:4], 16) % len(adjs)
        i2 = int(hex_id[4:8], 16) % len(adjs)
        ianimal = int(hex_id[8:12], 16) % len(animals)
    except ValueError:
        return None
    if i2 == i1:
        i2 = (i2 + 1) % len(adjs)
    return f"{adjs[i1]}-{adjs[i2]}-{animals[ianimal]}"
