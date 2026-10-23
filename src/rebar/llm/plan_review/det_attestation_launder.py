"""Detect operator-attested ACs that cite repository-resident proof.

An ``[operator-attested]`` tag is for evidence outside the snapshot. Inspect
the AC item's checkbox and continuations: tests paths, test symbols, and
pytest node IDs always count as repository proof. Other repo paths count only
when an evidence-introducing word precedes them on that line, leaving
orientation references alone. Scrub URLs first; commit hashes and Gerrit
change IDs are valid event provenance. The pre-LLM close guard consumes this
pure detector (bug 2f56; ADRs 0043 and 0016)."""

from __future__ import annotations

import re

from .det_measurement_provenance import _CHECKBOX_RE, _ac_section_bounds
from .det_operator_attested import _OPERATOR_ATTESTED_TAG_RE

# Links are external evidence: scrub them before any repo-shaped matching.
_URL_RE = re.compile(r"https?://\S+")

# A path anchored at a well-known repo root directory — fires with or without an extension
# (``src/rebar/_commands`` is as repo-resident as ``tests/unit/test_x.py``).
_REPO_ROOT_PATH_RE = re.compile(r"\b(?:src|tests|docs|scripts)/[\w.\-]+(?:/[\w.\-]+)*")

# A slash path whose final segment carries a code-shaped extension, optionally followed by a
# pytest ``::node`` id chain. The extension list is closed on purpose (precision-first).
_CODE_FILE_PATH_RE = re.compile(
    r"(?:\b[\w.\-]+/)+[\w.\-]+"
    r"\.(?:py|pyi|md|rst|json|toml|yaml|yml|sh|txt|cfg|ini)\b"
    r"(?:::\w+)*"
)

# A bare pytest symbol. Three-plus tail characters so prose fragments do not fire.
_TEST_SYMBOL_RE = re.compile(r"\btest_[A-Za-z0-9_]{3,}\b")

_CITATION_RES: tuple[re.Pattern[str], ...] = (
    _REPO_ROOT_PATH_RE,
    _CODE_FILE_PATH_RE,
    _TEST_SYMBOL_RE,
)

# Test artifacts are inherently completion EVIDENCE, never orientation: a tests/-anchored
# path, a path segment starting ``test_``/``conftest``, a pytest ``::node`` id, or a bare
# ``test_*`` symbol always counts as a citation.
_TEST_SHAPED_RE = re.compile(r"(?:^|/)tests?/|/(?:test_|conftest\.)|^test_[A-Za-z0-9_]{3,}$|::")

# A NON-test repo path counts as a citation only when the line PRESENTS it as the proof —
# an evidence-introducing word appears before it. A path in plain prose is orientation
# ("the fix shipped in src/… is deployed") and must not block a legitimately-external AC
# (false-positive class observed on tickets 7f69/5796).
_EVIDENCE_MARKER_RE = re.compile(
    r"\b(?:proxy|proof|evidence|verif\w*|cover\w*|tested|asserted|documented|recorded|see|per)\b",
    re.IGNORECASE,
)


def _citation_spans(scrubbed: str) -> list[tuple[int, int, str]]:
    """Containment-filtered citation spans over already-URL-scrubbed text, position-ordered.
    A hit fully contained in a longer hit is dropped (the ``test_x`` inside
    ``tests/unit/test_x.py`` is one citation, not two)."""
    spans: list[tuple[int, int, str]] = []
    for rx in _CITATION_RES:
        spans.extend((m.start(), m.end(), m.group(0)) for m in rx.finditer(scrubbed))
    kept: list[tuple[int, int, str]] = []
    for start, end, hit in sorted(spans, key=lambda s: (s[0] - s[1], s[0])):  # longest first
        if any(start >= k_start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, hit))
    return sorted(kept)


def repo_evidence_citations(text: str) -> list[str]:
    """Exact repo path/symbol citations in ``text``, deduplicated and position-ordered.
    Context-free extraction — the evidence-vs-orientation judgment lives in
    :func:`laundering_gaps`."""
    seen: set[str] = set()
    ordered: list[str] = []
    for _, _, hit in _citation_spans(_URL_RE.sub(" ", text)):
        if hit not in seen:
            seen.add(hit)
            ordered.append(hit)
    return ordered


def _line_evidence_citations(line: str) -> list[str]:
    """Citations on one line that read as completion EVIDENCE: test-shaped hits always,
    other repo hits only when an evidence-introducing word precedes them on the line."""
    scrubbed = _URL_RE.sub(" ", line)
    return [
        hit
        for start, _end, hit in _citation_spans(scrubbed)
        if _TEST_SHAPED_RE.search(hit) or _EVIDENCE_MARKER_RE.search(scrubbed[:start])
    ]


def laundering_gaps(plan_text: str) -> list[tuple[str, list[str]]]:
    """One ``(ac_line, citations)`` per ``[operator-attested]`` AC item whose own block —
    checkbox line plus continuation lines, bounded by the next checkbox item or the end of
    the ``## Acceptance Criteria`` section — cites exact repo path/symbol evidence."""
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
        next_boundary = (
            checkbox_indices[pos + 1] if pos + 1 < len(checkbox_indices) else section_end
        )
        citations: list[str] = []
        for block_line in lines[i:next_boundary]:
            for hit in _line_evidence_citations(block_line):
                if hit not in citations:
                    citations.append(hit)
        if citations:
            gaps.append((line, citations))
    return gaps
