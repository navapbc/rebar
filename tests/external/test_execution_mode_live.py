"""Story 4b2f: a LIVE single_turn workflow step (needs a real LLM).

Marked ``external`` → inert in the default suite (see tests/external/conftest.py);
runs only with REBAR_RUN_EXTERNAL=1 + ANTHROPIC_API_KEY + the [agents] extra. Kept
minimal: it proves a single_turn prompt drives ONE real structured model call whose
output validates against the prompt's declared ``outputs`` contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rebar
from rebar.llm.workflow import runs

pytest.importorskip("jsonschema")
pytest.importorskip("pydantic_ai")

pytestmark = pytest.mark.external


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_single_turn_live_structured_output(rebar_repo: Path) -> None:
    r = str(rebar_repo)
    pdir = Path(r) / ".rebar" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "live-verdict.md").write_text(
        "---\nexecution_mode: single_turn\noutputs: completion_verdict\n---\n"
        "Return a PASS verdict with an empty findings list for ticket {{ticket_id}}.",
        encoding="utf-8",
    )
    tid = rebar.create_ticket("task", "Live ST", description="body", repo_root=r)
    doc = {
        "schema_version": "1",
        "name": "live_single_turn",
        "steps": [
            {"id": "verify", "prompt": "live-verdict", "with": {"ticket_id": tid}},
        ],
    }
    res = runs.run(doc, {}, repo_root=r)
    assert res["status"] == "succeeded", res
    out = res["terminal_output"]
    assert out["verdict"] in ("PASS", "FAIL")
    assert isinstance(out.get("findings"), list)
