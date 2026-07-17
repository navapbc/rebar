"""Structural documentation test for the project.portability dogfood subsection
(epic jira-reb-1003, task taurine-catchable-kakarikis).

Reads the real docs/plan-review-gate.md and asserts the client-facing composition
example is present, complete, and placed OUTSIDE the built-in Pass-4 move table that
`test_plan_review.py::test_move_registry_matches_docs_table` parses.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = REPO_ROOT / "docs" / "plan-review-gate.md"
_HEADING = "### Dogfooding a project portability guard"
_ANCHOR = "never hand over a solution."
_NEXT = "### The advisory cap"

_TEMPLATE = (
    "Rework {subject} so it remains portable across supported rebar client shapes; "
    "keep project-specific behavior in project configuration or an explicit extension boundary."
)


def _doc() -> str:
    return _DOC.read_text(encoding="utf-8")


def _subsection() -> str:
    text = _doc()
    start = text.index(_HEADING)
    end = text.index(_NEXT, start)
    return text[start:end]


# ── the existing project-extension paragraph documents the full move schema ──────
def test_project_move_schema():
    text = _doc()
    assert "{move_id: {name, template, applies_when?}}" in text
    # absent/empty applies_when => always applicable; non-empty => intersects triggers
    assert "always applicable" in text
    assert "intersect" in text


# ── the dogfood subsection exists with the exact heading ─────────────────────────
def test_subsection_heading():
    assert _HEADING in _doc()


# ── it shows the exact routing entry + activation list ───────────────────────────
def test_routing_example():
    sub = _subsection()
    assert '"exec": "1-TURN"' in sub
    assert '"facet": "project-invariants"' in sub
    assert '["container", "leaf"]' in sub
    assert '"default_posture": "blocking"' in sub
    assert '"block_threshold": 0.9' in sub
    assert '"activate": ["project.portability"]' in sub


# ── the prompt path + execution mode ─────────────────────────────────────────────
def test_prompt_contract():
    sub = _subsection()
    assert ".rebar/prompts/plan-review-project-portability.md" in sub
    assert "single_turn" in sub


# ── the four rubric headings + typed finding-field mapping ───────────────────────
def test_finding_contract():
    sub = _subsection()
    for heading in (
        "## Finding threshold",
        "## Required finding fields",
        "## Supported client-shape matrix",
        "## Non-findings",
    ):
        assert heading in sub, f"missing rubric heading in doc subsection: {heading!r}"
    for marker in (
        "location: str",
        "finding: str",
        "scenarios: list[str]",
        "evidence: list[str]",
        "criteria: list[str]",
    ):
        assert marker in sub, f"missing typed field marker: {marker!r}"


# ── every supported client-shape label + value ───────────────────────────────────
def test_client_shape_matrix():
    sub = _subsection()
    matrix = {
        "Harness": "Python library, CLI, remote MCP; no Claude Code or Codex dependency.",
        "Target project": "Ruby, Python, Java, Next.js, .NET, Terraform subprojects in a monorepo.",
        "Platform and venue": "macOS, Windows, Linux, BSD, CI, servers, developer workstations.",
        "Project location and access": (
            "in-checkout current working directory, explicitly located workspace, "
            "server outside the checkout, no unrestricted-local-filesystem assumption."
        ),
    }
    for label, value in matrix.items():
        assert label in sub, f"missing client-shape label: {label!r}"
        assert value in sub, f"missing client-shape value for {label!r}"


# ── both exact non-finding rules ─────────────────────────────────────────────────
def test_false_positive_boundaries():
    sub = _subsection()
    assert "Silence about portability is not a finding" in sub
    assert (
        "Project-specific behavior behind project configuration or an explicit "
        "extension boundary is allowed"
    ) in sub


# ── the coaching move id, name, trigger, and locked template ─────────────────────
def test_coaching_example():
    sub = _subsection()
    assert "project-portability" in sub
    assert "restore rebar portability" in sub
    assert "[project.portability]" in sub
    assert _TEMPLATE in sub


# ── all five exact live-calibration thresholds ───────────────────────────────────
def test_calibration_example():
    sub = _subsection()
    for threshold in (
        "recall: 1.0",
        "false_accept: 0.0",
        "agreement: 1.0",
        "stability >= 0.6666666667",
        "kappa >= 0.70",
    ):
        assert threshold in sub, f"missing calibration threshold: {threshold!r}"


# ── the four-pass responsibility split ───────────────────────────────────────────
def test_pass_separation():
    sub = _subsection()
    assert "Pass 1" in sub and "counterexample" in sub
    assert "Pass 2" in sub
    assert "Pass 3" in sub
    assert "Pass 4" in sub and "project move" in sub


# ── the subsection sits outside the parsed built-in move table ───────────────────
def test_parser_safe_placement():
    text = _doc()
    anchor_at = text.index(_ANCHOR)
    heading_at = text.index(_HEADING)
    next_at = text.index(_NEXT)
    # the dogfood subsection begins after the project-extension paragraph's last sentence
    # and ends before the advisory-cap heading — i.e. wholly between them.
    assert anchor_at < heading_at < next_at
