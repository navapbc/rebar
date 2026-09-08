"""External check of a ``single_turn`` workflow step.

The test requires the external opt-in, agents package, and configured provider credential. A
matrix overlay selects the model through discovered configuration. The prompt makes one structured
call whose result must satisfy its declared ``outputs`` contract.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import _live_llm
import pytest

import rebar
from rebar.llm.workflow import runs

pytest.importorskip("jsonschema")
pytest.importorskip("pydantic_ai")

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_PROMPT_ID = "live-verdict"
_PROMPT_TEXT = (
    "---\nexecution_mode: single_turn\noutputs: completion_verdict\n---\n"
    "Return a PASS verdict with an empty findings list for ticket {{ticket_id}}."
)


@_live_llm.skip_without_live_llm
def test_single_turn_live_structured_output(
    rebar_repo: Path,
    project_prompt_writer: Callable[[Path, str, str], Path],
) -> None:
    r = str(rebar_repo)
    project_prompt_writer(rebar_repo, _PROMPT_ID, _PROMPT_TEXT)
    tid = rebar.create_ticket("task", "Live ST", description="body", repo_root=r)
    doc = {
        "schema_version": "1",
        "name": "live_single_turn",
        "steps": [
            {"id": "verify", "prompt": _PROMPT_ID, "with": {"ticket_id": tid}},
        ],
    }
    res = runs.run(doc, {}, repo_root=r)
    assert res["status"] == "succeeded", res
    out = res["terminal_output"]
    assert out["verdict"] in ("PASS", "FAIL")
    assert isinstance(out.get("findings"), list)
