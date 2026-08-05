"""Measurement-provenance DET lint (story f161, epic 3147; ADR-0043 x ADR-0016).

``[operator-attested]`` (ADR-0043) is a deliberate trust escape hatch: its evidence contract
demands provenance of the ATTESTATION EVENT (change id / vote / timestamp) but nothing about
provenance of the MEASUREMENT — which environment it was taken in, under which principal, at
what privilege relative to production, and with what KIND of instrument. This module asserts
the declaration is PRESENT and well-SHAPED. It makes no judgement about whether the declared
values are correct; that is the AGENT-tier criterion's job.

Contract (see ``tests/unit/test_det_measurement_provenance.py`` for the pinned examples). An
``[operator-attested]`` AC checklist item must carry a ``provenance:`` CONTINUATION LINE,
indented under the checkbox item it belongs to::

    - [ ] [operator-attested] <criterion text>
          provenance: environment=<v>; principal=<v>; privilege_posture=<v>; instrument=<v>
          — <justification>

The provenance line is the first FOLLOWING line that is (a) more-indented than the checkbox
marker and (b) begins with the case-insensitive literal key ``provenance:``. It binds to the
nearest preceding ``- [ ]`` checklist item and only within that item's own AC-checklist scope
(bounded by the next checkbox item, or the end of the ``## Acceptance Criteria`` section).

Pure stdlib (``re`` only) — no network, no shell.
"""

from __future__ import annotations

import re

from .det_operator_attested import _OPERATOR_ATTESTED_TAG_RE

PROVENANCE_KEYS: tuple[str, ...] = (
    "environment",
    "principal",
    "privilege_posture",
    "instrument",
)

PRIVILEGE_POSTURES: tuple[str, ...] = ("production-equivalent", "broader", "narrower")

INSTRUMENTS: tuple[str, ...] = ("live-call", "simulation", "static-analysis")

# Enum-restricted provenance keys: value must be one of the given members (after placeholder
# stripping). ``environment`` and ``principal`` are free text.
_ENUM_KEYS: dict[str, tuple[str, ...]] = {
    "privilege_posture": PRIVILEGE_POSTURES,
    "instrument": INSTRUMENTS,
}

_CHECKBOX_RE = re.compile(r"^(\s*)-\s*\[[ xX]?\]\s*")
_PROVENANCE_LINE_RE = re.compile(r"^provenance:\s*(.*)$", re.IGNORECASE)
_ANGLE_BRACKET_RE = re.compile(r"^<.*>$")

# Case-insensitive placeholder tokens (after trimming). A placeholder value is treated as ABSENT.
_PLACEHOLDER_VALUES = {"tbd", "todo", "n/a", "?", "-"}


def _is_placeholder(value: str) -> bool:
    """A value is a placeholder if empty, one of the reserved tokens, or angle-bracket wrapped."""
    v = value.strip()
    if not v:
        return True
    if v.lower() in _PLACEHOLDER_VALUES:
        return True
    return bool(_ANGLE_BRACKET_RE.match(v))


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _ac_section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Line-index range ``[start, end)`` of the ``## Acceptance Criteria`` section body,
    mirroring :func:`.det_operator_attested.ac_item_lines`'s scoping. ``None`` if the
    heading is absent."""
    start: int | None = None
    for i, ln in enumerate(lines):
        if ln.lower().startswith("## acceptance criteria"):
            start = i + 1
            continue
        if start is not None and ln.startswith("## "):
            return start, i
    if start is None:
        return None
    return start, len(lines)


def _find_provenance_line(
    lines: list[str], start: int, end: int, checkbox_indent: int
) -> str | None:
    """First line in ``lines[start:end]`` that is more-indented than ``checkbox_indent`` and
    begins with the case-insensitive literal key ``provenance:``."""
    for j in range(start, end):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            continue
        if _line_indent(line) <= checkbox_indent:
            continue
        if _PROVENANCE_LINE_RE.match(stripped):
            return stripped
    return None


def _parse_provenance(rest: str) -> tuple[dict[str, str], str]:
    """Split the text following the ``provenance:`` key into its ``key=value`` fields and the
    trailing em-dash justification."""
    if "—" in rest:
        body, justification = rest.split("—", 1)
    else:
        body, justification = rest, ""
    fields: dict[str, str] = {}
    for segment in body.split(";"):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        key, _, value = segment.partition("=")
        fields[key.strip().lower()] = value.strip()
    return fields, justification.strip()


def provenance_gaps(plan_text: str) -> list[tuple[str, list[str]]]:
    """Return one ``(ac_line, reasons)`` per ``[operator-attested]`` AC item whose provenance
    declaration is absent, incomplete, placeholder-valued, or outside the allowed enum sets."""
    lines = plan_text.split("\n")
    bounds = _ac_section_bounds(lines)
    if bounds is None:
        return []
    section_start, section_end = bounds
    checkbox_indices = [
        i for i in range(section_start, section_end) if _CHECKBOX_RE.match(lines[i])
    ]
    gaps: list[tuple[str, list[str]]] = []
    for pos, i in enumerate(checkbox_indices):
        line = lines[i]
        if not _OPERATOR_ATTESTED_TAG_RE.match(line):
            continue
        checkbox_indent = len(_CHECKBOX_RE.match(line).group(1))  # type: ignore[union-attr]
        next_boundary = (
            checkbox_indices[pos + 1] if pos + 1 < len(checkbox_indices) else section_end
        )
        prov_line = _find_provenance_line(lines, i + 1, next_boundary, checkbox_indent)
        reasons: list[str] = []
        if prov_line is None:
            reasons = [
                f"{key} is missing (no provenance declaration found)" for key in PROVENANCE_KEYS
            ]
            reasons.append("justification is missing (no provenance declaration found)")
            gaps.append((line, reasons))
            continue
        match = _PROVENANCE_LINE_RE.match(prov_line)
        rest = match.group(1) if match else ""
        fields, justification = _parse_provenance(rest)
        for key in PROVENANCE_KEYS:
            value = fields.get(key)
            if value is None or _is_placeholder(value):
                reasons.append(f"{key} is missing or a placeholder value")
                continue
            enum = _ENUM_KEYS.get(key)
            if enum is not None and value not in enum:
                reasons.append(f"{key} value {value!r} is not one of: {', '.join(enum)}")
        if _is_placeholder(justification):
            reasons.append("justification is missing or a placeholder value")
        if reasons:
            gaps.append((line, reasons))
    return gaps


def provenance_issues(plan_text: str) -> list[str]:
    """Advisory coaching strings (one per gap) for ``p6_ac_quality`` to surface. Never blocks."""
    issues: list[str] = []
    for line, reasons in provenance_gaps(plan_text):
        subject = re.sub(r"^\s*-\s*\[[ xX]?\]\s*", "", line).strip()[:80]
        issues.append(
            f"AC item {subject!r} is [operator-attested] but its provenance declaration is "
            f"incomplete ({'; '.join(reasons)}); add an indented continuation line "
            "'provenance: environment=<v>; principal=<v>; "
            "privilege_posture=<production-equivalent|broader|narrower>; "
            "instrument=<live-call|simulation|static-analysis> — <justification>' "
            "under the checkbox."
        )
    return issues
