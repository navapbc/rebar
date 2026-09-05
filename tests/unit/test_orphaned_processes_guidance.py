"""The bounding prescription has ONE source, and the linked doc never re-forks it.

Ticket 6a9d-4792-7099-4a17. Bug 0d6c-b19b-0de7-432e corrected AGENTS.md after a bare
`timeout N <cmd> &` was proved to exit 127 on macOS with the helper never started -- the
guidance written to prevent orphaned processes produced one. But `docs/orphaned-processes.md`
carried its own COPY of that snippet, kept prescribing it, and AGENTS.md links to that
document for incident evidence and recovery, so a reader who followed the pointer landed
back on the defective pattern.

The duplicated snippet is what drifted, so the fix removed the duplicate rather than
resyncing it: AGENTS.md holds the prescription, the doc points at it. These tests pin both
halves -- the source still exists where the pointer sends a reader, and the doc has not
grown a second copy that can drift again.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"
DOC = ROOT / "docs" / "orphaned-processes.md"


def _agents() -> str:
    return AGENTS.read_text(encoding="utf-8")


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


# -- the single source still exists, where the pointer sends a reader ------------------


def test_agents_holds_the_bound_cascade() -> None:
    """AGENTS.md is the one source; a reader forwarded there must find the snippet."""
    body = _agents()
    assert "## Bound background helpers at spawn" in body
    assert "bound() {" in body, "the host-selecting cascade must live in AGENTS.md"
    for bounder in ("timeout", "gtimeout", "perl -e 'alarm shift; exec @ARGV'"):
        assert bounder in body, f"the cascade must offer {bounder!r}"
    assert "not spawning" in body, "the cascade must refuse when the host has no bounder"


def test_doc_forwards_to_the_single_source_by_section_name() -> None:
    """A bare file link would rot silently if the section were renamed."""
    body = _doc()
    assert "AGENTS.md" in body, "the doc must forward to the source, not restate it"
    assert "Bound background helpers at spawn" in body, (
        "the forward must name the section so a rename is detectable"
    )


# -- the duplicate must not come back ---------------------------------------------------


def _fenced_code(text: str) -> list[str]:
    """Lines inside ``` fences -- i.e. what a reader would COPY, not prose about it.

    The distinction matters: this document must be free to NAME `trap \'kill 0\'` in
    order to prohibit it, while never handing a reader a block containing it.
    """
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


def test_doc_does_not_prescribe_a_bare_timeout_bound() -> None:
    """`timeout N <cmd>` is exactly what no-ops on this host; it must not reappear."""
    offenders = [
        line for line in _fenced_code(_doc()) if re.search(r"(?<![\w`-])g?timeout\s+\d+\s+\S", line)
    ]
    assert not offenders, (
        "docs/orphaned-processes.md re-prescribes a bare wall-clock bounder; "
        f"the prescription belongs only in AGENTS.md: {offenders}"
    )


def test_doc_does_not_prescribe_trap_kill_zero() -> None:
    """`kill 0` signals the caller's process group: too wide and too narrow at once."""
    body = _doc()
    offenders = [line for line in _fenced_code(body) if "kill 0" in line]
    assert not offenders, f"the disproved trap must not be prescribed again: {offenders}"
    assert "signals the process group on normal exit" not in body, (
        "the claim that the trap reaps the helper was disproved (rc 143, the SCRIPT died)"
    )
    assert "does not reap what you think it reaps" in body, (
        "naming the trap is only allowed in order to prohibit it"
    )


def test_doc_does_not_re_fork_the_cascade() -> None:
    """A second copy of the snippet is the drift vector this ticket closed."""
    assert "bound() {" not in _doc(), (
        "the cascade must not be duplicated here; forward to AGENTS.md instead"
    )


# -- the corrections the doc DOES keep, as consequences rather than as a second snippet --


def test_doc_records_why_a_bare_bounder_fails_on_this_host() -> None:
    body = _doc()
    assert "127" in body, "the observed exit code of the absent bounder must survive"
    assert "neither `timeout` nor `gtimeout`" in body


def test_doc_records_the_kill_zero_scope_correction() -> None:
    body = _doc()
    assert "signals the caller's process group" in body
    assert "wrapper shell above the script is never in that group" in body


def test_doc_warns_that_pgrep_matches_text_not_identity() -> None:
    """The recovery section drives `pgrep`/`pkill`, so it must carry the caveat."""
    body = _doc()
    assert "match command-line text rather than identity" in body
    assert "ps -o pid=,ppid=,command= -p" in body


def test_doc_keeps_the_gate_carve_out_without_naming_a_dead_bounder() -> None:
    body = _doc()
    assert "## Bounded gate operations" in body
    assert "Do not apply a wall-clock bound to `review-plan`" in body


# -- the evidence AGENTS.md links here FOR is still here --------------------------------


def test_doc_retains_the_incident_evidence_and_recovery() -> None:
    """AGENTS.md forwards here for these; correcting the prescription must not drop them."""
    body = _doc()
    assert "## Incident evidence" in body
    assert "2026-08-22" in body, "the incident date anchors the evidence"
    assert "1341 percent" in body, "the measured combined CPU must survive"
    assert "## Read-only detection" in body
    assert "scripts/check_orphaned_load.py" in body
    assert "## Operator recovery" in body
