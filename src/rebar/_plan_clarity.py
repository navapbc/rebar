"""Pure structural hard floors for executable ticket plans.

The legacy clarity score remains a compatibility heuristic.  This module owns
the smaller deterministic floor shared by standalone ``clarity-check`` and the
plan-review P1 check, so the two surfaces cannot drift on plan structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ACTIVE_SECTIONS = frozenset({"approach", "scope", "testing", "acceptance criteria"})
_AC_SECTION = "acceptance criteria"

_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_CHECKBOX_RE = re.compile(r"^- \[( |x|X)\](?:[ \t]+(.*))?$")
_INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

_SENTINEL = r"(?:\b(?:TBD|TODO|FIXME)\b|\?\?\?)"
_SENTINEL_VALUE_RE = re.compile(
    rf"(?:^[ \t]*(?:[-*+][ \t]+(?:\[(?: |x|X)\][ \t]*)?)?"
    rf"|[:=][ \t]*|\b(?:is|remains|left)[ \t]+)"
    rf"(?P<sentinel>{_SENTINEL})",
    re.IGNORECASE,
)
_SUPPRESSION_RE = re.compile(
    r"\b(?:no|zero|remove|delete|reject|forbid|without)\b|\bmust[ \t]+not\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SentinelAssignment:
    """One unresolved sentinel occupying a structural value position."""

    section: str
    line_number: int
    line: str
    sentinel: str


@dataclass(frozen=True)
class PlanClarityFloor:
    """Parsed evidence and verdict for the deterministic clarity floor."""

    ac_items: tuple[str, ...]
    empty_ac_items: tuple[int, ...]
    sentinel_assignments: tuple[SentinelAssignment, ...]
    unchecked_ac_lines: tuple[str, ...] = ()
    ac_item_chars: tuple[int, ...] = ()

    @property
    def passes(self) -> bool:
        """Whether the plan has a usable checklist and no structural defects."""
        return bool(self.ac_items) and not self.empty_ac_items and not self.sentinel_assignments


def _heading(line: str) -> str | None:
    match = _H2_RE.fullmatch(line)
    if match is None:
        return None
    return match.group(1).strip().casefold()


def _fence_marker(line: str) -> str | None:
    match = _FENCE_OPEN_RE.match(line)
    return match.group(1) if match else None


def _closes_fence(line: str, marker: str) -> bool:
    char = re.escape(marker[0])
    return bool(re.fullmatch(rf"[ \t]{{0,3}}{char}{{{len(marker)},}}[ \t]*", line))


def _without_inline_code(line: str) -> str:
    # Preserve a separator so syntax on opposite sides of a code span cannot
    # collapse into a new value-position match.
    return _INLINE_CODE_RE.sub(" CODE ", line)


def _sentinel_assignment(line: str, *, section: str, line_number: int) -> SentinelAssignment | None:
    visible = _without_inline_code(line)
    if _SUPPRESSION_RE.search(visible):
        return None
    match = _SENTINEL_VALUE_RE.search(visible)
    if match is None:
        return None
    return SentinelAssignment(
        section=section,
        line_number=line_number,
        line=line.strip(),
        sentinel=match.group("sentinel"),
    )


@dataclass
class _PlanClarityAccumulator:
    ac_items: list[str] = field(default_factory=list)
    ac_item_chars: list[int] = field(default_factory=list)
    empty_ac_items: list[int] = field(default_factory=list)
    sentinels: list[SentinelAssignment] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    item_lines: list[str] | None = None
    item_verbatim_lines: list[str] | None = None
    item_unchecked: bool = False
    item_verbatim: str = ""

    def finish_item(self) -> None:
        if self.item_lines is None:
            return
        body = "\n".join(self.item_lines).strip()
        self.ac_items.append(body)
        if self.item_verbatim_lines is not None:
            self.ac_item_chars.append(sum(len(line) + 1 for line in self.item_verbatim_lines))
        if not body:
            self.empty_ac_items.append(len(self.ac_items))
        if self.item_unchecked and self.item_verbatim:
            self.unchecked.append(self.item_verbatim)
        self.item_lines = None
        self.item_verbatim_lines = None
        self.item_unchecked = False
        self.item_verbatim = ""

    def start_item(self, checkbox: re.Match[str], line: str) -> None:
        self.finish_item()
        self.item_unchecked = checkbox.group(1) == " "
        self.item_verbatim = line
        self.item_lines = [checkbox.group(2) or ""]
        self.item_verbatim_lines = [line]

    def append_item_line(self, line: str, *, charged_to_item: bool) -> None:
        if self.item_lines is None:
            return
        self.item_lines.append(line)
        if charged_to_item:
            assert self.item_verbatim_lines is not None
            self.item_verbatim_lines.append(line)


def evaluate_plan_clarity(text: str) -> PlanClarityFloor:
    """Parse *text* and evaluate the shared deterministic plan floor.

    Standard checklist items are collected only from an exact
    ``## Acceptance Criteria`` section.  Their continuation lines extend until
    the next standard checklist item or H2.  Sentinel assignments are scanned
    only in the four exact active section names, outside fenced and inline code.
    """
    acc = _PlanClarityAccumulator()
    section: str | None = None
    fence: str | None = None
    text_ends_with_newline = text.endswith(("\n", "\r"))

    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = _fence_marker(line)
        if fence is not None:
            if section == _AC_SECTION:
                acc.append_item_line(line, charged_to_item=True)
            if marker is not None and marker[0] == fence[0] and _closes_fence(line, fence):
                fence = None
            continue
        if marker is not None:
            if section == _AC_SECTION:
                acc.append_item_line(line, charged_to_item=True)
            fence = marker
            continue

        heading = _heading(line)
        if heading is not None:
            acc.finish_item()
            section = heading
            continue

        if section == _AC_SECTION:
            checkbox = _CHECKBOX_RE.fullmatch(line)
            if checkbox is not None:
                acc.start_item(checkbox, line)
            else:
                acc.append_item_line(line, charged_to_item=not line.strip() or line[:1].isspace())

        if section in _ACTIVE_SECTIONS:
            sentinel = _sentinel_assignment(line, section=section, line_number=line_number)
            if sentinel is not None:
                acc.sentinels.append(sentinel)

    acc.finish_item()
    if acc.ac_item_chars and not text_ends_with_newline:
        acc.ac_item_chars[-1] = max(acc.ac_item_chars[-1] - 1, 0)
    return PlanClarityFloor(
        ac_items=tuple(acc.ac_items),
        empty_ac_items=tuple(acc.empty_ac_items),
        sentinel_assignments=tuple(acc.sentinels),
        unchecked_ac_lines=tuple(acc.unchecked),
        ac_item_chars=tuple(acc.ac_item_chars),
    )
