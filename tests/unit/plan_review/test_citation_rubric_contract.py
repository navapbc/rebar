"""Contract test: the G1G2/E4/E6 Pass-1 finder rubrics must carry the Layer-2
``[rebar:<id>]`` citation coverage protocol (story 266e).

Each rubric must (a) mention the machine-parseable ``[rebar:`` citation token,
(b) direct the finder to retrieve the cited ticket via the ``show_ticket``
coverage tool, and (c) state the explicit fail-closed clause (an uncited /
edge-unbacked / coverage-unconfirmed citation still fails closed / grounds as
normal). This is the source-of-truth guard; ``docs/plan-review-criteria-guide.md``
is GENERATED from these files and must not be hand-edited.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

_REVIEWERS_DIR = files("rebar.llm").joinpath("reviewers")
_RUBRICS = ("plan_review_G1G2.md", "plan_review_E4.md", "plan_review_E6.md")


@pytest.mark.parametrize("name", _RUBRICS)
def test_rubric_carries_citation_protocol(name: str) -> None:
    text = _REVIEWERS_DIR.joinpath(name).read_text(encoding="utf-8")
    low = text.lower()
    # (a) the machine-parseable citation token
    assert "[rebar:" in text, f"{name} missing the [rebar:<id>] citation token"
    # (b) the show_ticket coverage-retrieval tool
    assert "show_ticket" in text, f"{name} missing show_ticket coverage retrieval"
    # (c) the explicit fail-closed clause
    assert "fail" in low and "closed" in low, f"{name} missing the fail-closed clause"
