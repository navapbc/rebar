"""Deterministic attestation-laundering detector (bug 2f56-313f-6175-41b1; ADR-0043 x ADR-0016).

THE ANTI-PATTERN. ``[operator-attested]`` (ADR-0043) exempts an acceptance criterion from
repository verification because its done-evidence legitimately lives OUTSIDE the snapshot —
a deploy, a vote, console output. The completion verifier classifies SOLELY from the author
tag (by design; see ``completion_verifier.md``), so tagging a criterion whose proof is
repo-resident LAUNDERS it past verification: the verifier accepts a ticket comment where an
exact path/symbol check was possible. The live instance this module answers is epic
``fb8a-7363-e406-4e36`` AC-19: retagged after a verifier step-budget failure although its
proof was ``tests/unit/test_scan_scoping.py`` — the review-side evidence-kind criterion (an
LLM judgment) missed it, and the provenance lint surfaced only as ADVISORY P6 coaching.

THE DETECTOR. A tagged AC item fires when its own text — the checkbox line plus its indented
continuation lines, up to the next checkbox item or the end of the AC section — cites exact
repository path/symbol evidence. Matching is precision-first, because a false positive here
would block ADR-0043's legitimate escape hatch:

* repo-root-anchored paths (``src/…``, ``tests/…``, ``docs/…``, ``scripts/…``), extension
  or not;
* slash paths ending in a code-shaped extension, with an optional pytest ``::node`` suffix
  (``pkg/mod.py``, ``pkg/mod.py::test_x``);
* bare pytest symbols (``test_<name>``).

URLs are scrubbed first (a link is external evidence; its path segments must not read as
repo paths). Bare commit hashes and Gerrit change numbers deliberately do NOT fire: they are
the attestation-EVENT provenance ADR-0043's contract itself demands. Dotted config keys
(``compact.trigger``) carry no slash and do not fire.

Consumed by the deterministic pre-LLM close guard
(:func:`rebar._commands.txn.ensure_attested_items_valid`). Pure stdlib (``re`` only) — no
network, no shell, mirroring its siblings ``det_operator_attested`` (the opposite-direction
lint: external evidence left UNtagged) and ``det_measurement_provenance`` (the
``provenance:`` continuation-line shape).
"""

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


def repo_evidence_citations(text: str) -> list[str]:
    """Exact repo path/symbol citations in ``text``, deduplicated and position-ordered.

    A hit fully contained in a longer hit is dropped (the ``test_x`` inside
    ``tests/unit/test_x.py`` is one citation, not two)."""
    scrubbed = _URL_RE.sub(" ", text)
    spans: list[tuple[int, int, str]] = []
    for rx in _CITATION_RES:
        spans.extend((m.start(), m.end(), m.group(0)) for m in rx.finditer(scrubbed))
    kept: list[tuple[int, int, str]] = []
    for start, end, hit in sorted(spans, key=lambda s: (s[0] - s[1], s[0])):  # longest first
        if any(start >= k_start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, hit))
    seen: set[str] = set()
    ordered: list[str] = []
    for _, _, hit in sorted(kept):
        if hit not in seen:
            seen.add(hit)
            ordered.append(hit)
    return ordered


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
        citations = repo_evidence_citations("\n".join(lines[i:next_boundary]))
        if citations:
            gaps.append((line, citations))
    return gaps
