"""Harvest in-tree ``# mechanism-ok:`` markers for the mechanism-delta ratchet.

The exact form is ``# mechanism-ok: <kind> <name> — <reason or ticket id>``. A marker
admits only its ``kind::name`` key. Every harvested marker needs a nonblank reason. Names
may contain spaces or ``::``. The reason begins at the first separator surrounded by
whitespace. Definition and string-literal markers use the site line or its predecessor.
Filename-glob markers use the first ``HEAD_LINES`` lines. YAML markers use the name or run
line, or its predecessor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

MARKER = "mechanism-ok:"

_MARKER_RE = re.compile(r"#\s*mechanism-ok:(.*)")

# Accepted name and reason separators. The parser requires surrounding whitespace below.
_SEPARATORS = ("—", "--", "-", ":")

# Names may contain spaces or ``::``, so only a whitespace-surrounded separator starts the reason.
_NAME_REASON_RE = re.compile(r"\s+(?:" + "|".join(re.escape(s) for s in _SEPARATORS) + r")\s+")

HEAD_LINES = 20

# Exact ``kind::name`` keys mapped to reasons, which may be blank.
MarkerMap = dict[str, str]

# ``(name, path, line)`` where ``None`` selects the first ``HEAD_LINES`` lines.
Site = tuple[str, Path, int | None]


def parse_marker(line: str) -> tuple[str, str, str] | None:
    """Parse a marker and return missing components as empty strings for downstream checks."""
    match = _MARKER_RE.search(line)
    if match is None:
        return None
    kind, _, rest = match.group(1).strip().partition(" ")
    name, reason = _split_name_reason(rest.strip())
    return kind, name, reason


def _split_name_reason(rest: str) -> tuple[str, str]:
    """Split a marker's tail into ``(name, reason)``.

    A whitespace-surrounded separator wins, so a name may contain spaces. Without one the
    reading falls back to two tokens, and a reason that is nothing but a dangling separator
    collapses to ``""`` so it is reported as the blank marker it is.
    """
    split = _NAME_REASON_RE.search(rest)
    if split is not None:
        return rest[: split.start()].strip(), rest[split.end() :].strip()
    name, _, reason = rest.partition(" ")
    reason = reason.strip()
    for sep in _SEPARATORS:
        if reason.startswith(sep):
            reason = reason[len(sep) :].strip()
            break
    return name.strip(), reason


def _record(markers: MarkerMap, parsed: tuple[str, str, str]) -> None:
    kind, name, reason = parsed
    markers[f"{kind}::{name}"] = reason


def collect_at_line(lines: list[str], lineno: int, markers: MarkerMap) -> None:
    """Harvest a marker on the 1-based ``lineno`` or the line immediately before it."""
    for idx in (lineno - 1, lineno - 2):
        if 0 <= idx < len(lines):
            parsed = parse_marker(lines[idx])
            if parsed is not None:
                _record(markers, parsed)
                return


def collect_in_head(lines: list[str], markers: MarkerMap, head: int = HEAD_LINES) -> None:
    """Harvest every marker in a file's first ``head`` lines (the filename-glob shape)."""
    for line in lines[:head]:
        parsed = parse_marker(line)
        if parsed is not None:
            _record(markers, parsed)


def read_lines(path: Path) -> list[str]:
    """Read a file's lines, returning ``[]`` for anything unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def harvest(sites: Iterable[Site], markers: MarkerMap) -> None:
    """Harvest every marker reachable from ``sites`` into ``markers``.

    The site's ``lineno`` selects the placement rule, so a detector never has to know the
    marker syntax — it reports where it found each mechanism and this does the rest.
    """
    cache: dict[Path, list[str]] = {}
    for _name, path, lineno in sites:
        key = path
        if key not in cache:
            cache[key] = read_lines(path)
        lines = cache[key]
        if lineno is None:
            collect_in_head(lines, markers)
        else:
            collect_at_line(lines, lineno, markers)
