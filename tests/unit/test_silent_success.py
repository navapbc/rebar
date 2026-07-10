"""Silent-success handling on the structured-output path (story polite-dutiful-drake,
epic jira-reb-687). Offline, no billable call.

Covers: NativeOutput stop-reason parity (a truncated/refused NativeOutput turn raises
UnretryableOutputError → INDETERMINATE, not a hollow verdict); schema-guided json-repair
with a safe fallback; and the bounded faulty-prior-output echoed into the reask.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm import structured
from rebar.llm.errors import UnretryableOutputError

pytestmark = pytest.mark.unit


# ── NativeOutput stop-reason parity ───────────────────────────────────────────
@pytest.mark.parametrize("reason", ["length", "max_tokens", "content_filter", "refusal"])
def test_native_output_path_checks_stop_reason(reason):
    """The check drake adds to the NativeOutput branch: a bad finish_reason raises
    UnretryableOutputError (the class that degrades the gate to INDETERMINATE) — the same
    guard the PromptedOutput path already applies."""
    with pytest.raises(UnretryableOutputError):
        structured.check_stop_reason(reason)


def test_zero_finding_clean_review_passes():
    """A normal finish (no truncation/refusal) and a valid zero-finding verdict parse
    cleanly — the stop-reason guard must not false-fire on a legitimately empty review."""
    assert structured.check_stop_reason("stop") is None
    assert structured.check_stop_reason(None) is None
    parsed = structured.tolerant_parse('{"verdict":"PASS","findings":[]}')
    assert parsed == {"verdict": "PASS", "findings": []}


# ── Schema-guided json-repair with a safe fallback ────────────────────────────
def test_repair_json_receives_schema(monkeypatch):
    """When a Pydantic model is threaded through, json-repair is called WITH schema=model."""
    import rebar.llm.structured as struct_mod

    captured: dict = {}

    def _fake_repair(cand, return_objects=True, schema=None, **kw):
        captured["schema"] = schema
        return {"verdict": "PASS", "findings": []}

    import json_repair

    monkeypatch.setattr(json_repair, "repair_json", _fake_repair)

    class _Model:  # a stand-in schema object
        pass

    # Force the json-repair path: a body strict json.loads can't take but a candidate exists.
    struct_mod.tolerant_parse("prefix {'verdict': 'PASS', 'findings': []} suffix", schema=_Model)
    assert captured["schema"] is _Model


def test_repair_json_falls_back_when_schema_call_raises(monkeypatch):
    """A schema-guided repair that raises falls back to the schema-less call — never a
    regression over today's behavior."""
    import json_repair

    calls: list = []

    def _fake_repair(cand, return_objects=True, schema=None, **kw):
        calls.append(schema)
        if schema is not None:
            raise ValueError("schema-guided repair blew up")
        return {"verdict": "PASS", "findings": []}

    monkeypatch.setattr(json_repair, "repair_json", _fake_repair)

    class _Model:
        pass

    out = structured.tolerant_parse("prefix {bad json here} suffix", schema=_Model)
    assert out == {"verdict": "PASS", "findings": []}
    assert calls == [_Model, None]  # tried schema-guided, then fell back schema-less


# ── Bounded faulty prior output in the reask ──────────────────────────────────
def test_reask_prompt_includes_bounded_prior_output():
    """The runner's bounded-retry reask echoes the model's own faulty reply (truncated to
    the named constant) so it can diff its mistake."""
    from rebar.llm import runner as runner_mod

    assert runner_mod._FAULTY_OUTPUT_SNIPPET_CHARS == 2000
    import inspect

    src = inspect.getsource(runner_mod._pai_structured)
    # The reask constructs the prompt from the truncated faulty output.
    assert "_FAULTY_OUTPUT_SNIPPET_CHARS" in src
    assert "Your previous reply was:" in src
    assert "[truncated]" in src
