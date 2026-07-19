"""Isolated Claude-Code transcript mining adapter (ticket 538c).

Mines Claude Code session transcripts (JSONL) for environment /
integration-diagnosis signatures using a **deterministic keyword classifier** —
no LLM and no network. Each transcript line is scanned for known error
signatures; the first matching rule (in priority order) wins and labels the line
with one of the closed classes in :data:`CLASSES`. Lines matching no rule (or
classified ``none``) are dropped.

This module is **isolated**: it is imported only by
``scripts/backfill_transcripts.py`` (the persistence path) and its tests. The
core ``rebar.metrics`` package and its registry never import it, and importing
it registers nothing into ``REGISTRY``.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Closed set of classifier labels. ``none`` is the sentinel for "no signature"
# and is never emitted as a record's ``kind``.
CLASSES: frozenset[str] = frozenset({"env_setup", "dependency", "integration", "tooling", "none"})

# Ordered (first-match-wins) classifier rules: (kind, compiled case-insensitive
# regex). Priority follows the ticket's rule ordering so that, e.g., a bare
# ``ImportError`` classifies as ``env_setup`` before any weaker rule can claim
# it.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "env_setup",
        re.compile(
            r"ModuleNotFoundError"
            r"|ImportError"
            r"|No module named"
            r"|No such file or directory"
            r"|command not found"
            r"|(?:virtualenv|venv)\b.*(?:activ|not found|error)"
            r"|(?:activate).*(?:No such file|not found)",
            re.IGNORECASE,
        ),
    ),
    (
        "dependency",
        re.compile(
            r"\bpip\b.*(?:error|resolut|conflict|could not)"
            r"|\buv\b.*(?:error|resolut|conflict|could not)"
            r"|version conflict"
            r"|Could not find a version"
            r"|incompatible"
            r"|ResolutionImpossible"
            r"|dependency conflict",
            re.IGNORECASE,
        ),
    ),
    (
        "integration",
        re.compile(
            r"ConnectionError"
            r"|connection refused"
            r"|timed out"
            r"|ReadTimeout"
            r"|LLMUnavailableError"
            r"|\b429\b"
            r"|\b5\d{2}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tooling",
        re.compile(
            r"\bgit\b.*(?:error|fatal|failed)"
            r"|fatal:"
            r"|\bruff\b.*(?:error|fail)"
            r"|\bmypy\b.*(?:error|fail)"
            r"|\bpytest\b.*(?:error|fail|no tests ran|usage)"
            r"|Permission denied",
            re.IGNORECASE,
        ),
    ),
]


def classify(text: str) -> str:
    """Deterministically classify a line of text into one of :data:`CLASSES`.

    Applies the ordered rules first-match-wins and returns the matching class
    name; returns ``"none"`` when no rule matches.
    """
    for kind, pattern in _RULES:
        if pattern.search(text):
            return kind
    return "none"


def _signature(text: str) -> str:
    """Return the substring that triggered classification (best-effort).

    Returns the first matching rule's matched text so records carry the concrete
    signature; falls back to the stripped line when nothing matches.
    """
    for _kind, pattern in _RULES:
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return text.strip()


def _line_text(obj: Any, raw: str) -> str:
    """Extract the scannable text for a parsed JSONL entry.

    Prefers a ``text`` or ``content`` field on a dict entry; otherwise scans the
    whole raw line.
    """
    if isinstance(obj, dict):
        for field in ("text", "content"):
            value = obj.get(field)
            if isinstance(value, str) and value:
                return value
    return raw


def _entry_ts(obj: Any) -> str:
    """Return the entry's ``ts`` if present and a string, else ``""``."""
    if isinstance(obj, dict):
        ts = obj.get("ts")
        if isinstance(ts, str):
            return ts
    return ""


def mine_transcript(path: str) -> list[dict]:
    """Mine one JSONL transcript file for classified error signatures.

    Reads ``path`` line by line (one JSON object per line). For each line the
    scannable text is taken from a ``text``/``content`` field when present, else
    the whole raw line. The text is classified deterministically; every line
    that matches a rule (i.e. classifies to something other than ``none``)
    yields one record::

        {"kind": <class, not "none">, "signature": <matched text>,
         "ts": <entry ts or "">, "source": "backfill_classified",
         "confidence": "classified"}

    Lines that match no rule are dropped.
    """
    records: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            try:
                obj: Any = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                obj = None
            text = _line_text(obj, raw)
            kind = classify(text)
            if kind == "none":
                continue
            records.append(
                {
                    "kind": kind,
                    "signature": _signature(text),
                    "ts": _entry_ts(obj),
                    "source": "backfill_classified",
                    "confidence": "classified",
                }
            )
    return records
