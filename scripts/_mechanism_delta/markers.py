"""In-tree ``# mechanism-ok:`` marker idiom for the mechanism-delta ratchet.

Modelled on ``scripts/check_config_reads.py``'s ``# read-via:`` marker: the justification
for a new mechanism lives NEXT TO the mechanism, in the tree, where a reviewer reading the
definition sees it — not in a side file that drifts out of step with the code.

    # mechanism-ok: <kind> <name> — <reason or ticket id>

A marker admits EXACTLY the ``(kind, name)`` it names, never its whole kind, so adding a
second lock does not ride in on the first one's justification. A blank reason is itself an
error: an unexplained marker is indistinguishable from a rubber stamp.

The name may contain SPACES — a workflow step is named for its ``- name:`` text, or for its
``run:`` snippet when unnamed, so ``pytest -q tests`` is an ordinary name. The reason
therefore begins at the first separator with whitespace on BOTH sides, not at the second
word; without that rule such a name truncates at its first flag and can never be justified.

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

# A mechanism NAME may contain spaces — a workflow step is named for its `- name:` text, so
# `Run tests` is the norm, not the exception. The reason therefore starts at the first
# separator that is *surrounded by whitespace*, never at the second word. `::` inside a name
# is untouched because it carries no spaces.
_NAME_REASON_RE = re.compile(r"\s+(?:" + "|".join(re.escape(s) for s in _SEPARATORS) + r")\s+")

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
