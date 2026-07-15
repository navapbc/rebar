"""Story e7e0: LIVE completion-verifier PASS emits a positive per-criterion ``criteria[]``.

Marked ``external`` → inert in the default suite (see tests/external/conftest.py); runs only
with REBAR_RUN_EXTERNAL=1 + ANTHROPIC_API_KEY + the [agents] extra. Proves the cutover
live-exercise DoD: the REAL agent path, on a fixture whose criterion is met, returns a PASS
carrying a populated ``criteria[]`` that persists to the completion sidecar.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar.llm import completion_sidecar

pytest.importorskip("pydantic_ai")

pytestmark = pytest.mark.external


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_live_completion_pass_persists_criteria(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    # A real source file the criterion can be checked against, committed to HEAD.
    (rebar_repo / "widget.py").write_text(
        "def render_widget():\n    return 'ok'\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", r, "add", "widget.py"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", r, "commit", "-q", "-m", "add widget"], check=True, capture_output=True
    )
    desc = (
        "Body describing the widget.\n\n## Acceptance Criteria\n"
        "- [ ] A `render_widget` function is defined in `widget.py`.\n"
    )
    tid = rebar.create_ticket("task", "Live completion PASS", description=desc, repo_root=r)
    rebar.transition(tid, "open", "in_progress", repo_root=r)

    # Run the REAL verifier against the local HEAD tree (no origin fetch).
    verdict = rebar.llm.verify_completion(
        tid, graph=False, ref="HEAD", source="local", fetch=False, repo_root=r
    )
    assert verdict["verdict"] == "PASS", verdict
    criteria = verdict.get("criteria")
    assert isinstance(criteria, list) and criteria, "a live PASS must carry a populated criteria[]"
    assert any("render_widget" in c.get("criterion", "") for c in criteria)

    # The PASS record persists via the real emit path and round-trips.
    verdict.setdefault("ticket_id", tid)
    assert completion_sidecar.emit(verdict, repo_root=r)
    rec = completion_sidecar.latest_pass_record(tid, repo_root=r)
    assert rec is not None and rec["schema"] == "completion_verifier_pass_v1"
    assert rec["criteria"], "the persisted PASS record carries the per-criterion array"
