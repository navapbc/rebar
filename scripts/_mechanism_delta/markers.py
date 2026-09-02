"""In-tree ``# mechanism-ok:`` marker idiom for the mechanism-delta ratchet.

Modelled on ``scripts/check_config_reads.py``'s ``# read-via:`` marker: the justification
for a new mechanism lives NEXT TO the mechanism, in the tree, where a reviewer reading the
definition sees it — not in a side file that drifts out of step with the code.

    # mechanism-ok: <kind> <name> — <reason or ticket id>

A marker admits EXACTLY the ``(kind, name)`` it names, never its whole kind, so adding a
second lock does not ride in on the first one's justification. A blank reason is itself an
error: an unexplained marker is indistinguishable from a rubber stamp.

Placement follows the detection shape (each detector scans exactly its own):

===================  ======================================================
shape                where the marker may sit
===================  ======================================================
Python def line      that line, or the one before it
string literal       the literal's line, or the one before it
filename glob        the matched file's first 20 lines
YAML step            the step's ``- name:``/``run:`` line, or the one before
===================  ======================================================
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

MARKER = "mechanism-ok:"

_MARKER_RE = re.compile(r"#\s*mechanism-ok:(.*)")

# Separators an author may put between the name and the reason. The em dash is the house
# style; the ASCII forms are accepted so a marker is never rejected on typography.
_SEPARATORS = ("—", "--", "-", ":")

HEAD_LINES = 20

# ``{"<kind>::<name>": reason}``. ``reason`` may be the empty string — that is the blank
# marker the gate reports as an error rather than silently honouring.
MarkerMap = dict[str, str]

# One detected definition site: the mechanism's name, the file it lives in, and the 1-based
# line its marker may sit on (or ``None`` for the filename-glob shape, whose marker lives in
# the matched file's first :data:`HEAD_LINES` lines).
Site = tuple[str, Path, int | None]


def parse_marker(line: str) -> tuple[str, str, str] | None:
    """Parse one source line into ``(kind, name, reason)``, or ``None`` if it has no marker.

    Missing parts come back as ``""`` rather than raising, so a malformed marker surfaces
    downstream as a blank-reason error instead of vanishing.
    """
    match = _MARKER_RE.search(line)
    if match is None:
        return None
    parts = match.group(1).strip().split(None, 2)
    kind = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    reason = parts[2].strip() if len(parts) > 2 else ""
    for sep in _SEPARATORS:
        if reason.startswith(sep):
            reason = reason[len(sep) :].strip()
            break
    return kind, name, reason


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
