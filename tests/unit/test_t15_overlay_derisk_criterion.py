"""Registration tests for the built-in T15 "overlay-derisk" plan-review criterion (story ea28).

Unlike `project.measurement-provenance` (story f161, which rides the `.rebar/` project overlay),
T15 ships in the DEFAULT criteria set — so it changes behaviour for every rebar client and its
registration must be complete and self-consistent.

SCOPE BOUNDARY (from the ticket): this pins the criterion ARTIFACT + REGISTRATION + regenerated
guide. Runtime routing BEHAVIOUR — does T15 actually fire on an infra plan and stay silent on an
app-only plan — is proven by the eval-fixtures story 36ab, which depends on this one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUBRIC = REPO / "src/rebar/llm/reviewers/plan_review_T15.md"
ROUTING = REPO / "src/rebar/llm/plan_review/criteria_routing.json"

# The four checks the rubric body must state, plus the S1-S3 applicability gate and the
# anti-false-positive paragraph.
RUBRIC_REQUIRED = (
    "S1",
    "S2",
    "S3",
    "RISK NAMED",
    "FAST OUT-OF-LOOP PROOF",
    "PROVE-THEN-CODIFY",
    "SCOPED CLEANUP",
    "ANTI-FP",
)


def test_rubric_exists() -> None:
    assert RUBRIC.is_file(), f"missing rubric: {RUBRIC}"


@pytest.mark.parametrize("marker", RUBRIC_REQUIRED)
def test_rubric_states_every_gate_and_check(marker: str) -> None:
    assert marker in RUBRIC.read_text(), f"rubric is missing {marker!r}"


def test_rubric_front_matter_is_tool_enabled_and_named() -> None:
    """Two front-matter fields are load-bearing:

    `execution_mode: agentic` — tooling is granted by the PROMPT's execution_mode, and the
    loader enum is single_turn|agentic ("AGENT" is the ROUTING value and is NOT valid here).
    `title:` — build_descriptor computes `name = prompt.title or cid`, so without it the
    criterion's rendered name degrades to the bare id `T15`.
    """
    lines = [ln.strip() for ln in RUBRIC.read_text().splitlines()]
    assert "execution_mode: agentic" in lines, "rubric must be agentic (tool-using)"
    assert "dimension: overlay-derisk" in lines
    title = [ln for ln in lines if ln.startswith("title:")]
    assert title and len(title[0]) > len("title:") + 1, "rubric needs a descriptive title"


def test_routing_entry_has_the_required_values() -> None:
    """The KEY existing is not enough — the values are what route the criterion."""
    entry = json.loads(ROUTING.read_text())["T15"]
    assert entry["exec"] == "AGENT"
    assert entry["facet"] == "overlay-derisk"
    assert entry["overlay_routing"] == "llm", "content-routed by the orchestrator, like T13/T14"
    assert entry["applies_at"]["suppress_types"] == ["bug"]
