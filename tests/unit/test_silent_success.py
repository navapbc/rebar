"""Silent-success handling on the structured-output path (story polite-dutiful-drake,
epic jira-reb-687). Offline, no billable call.

Covers: NativeOutput stop-reason parity (a truncated/refused NativeOutput turn raises
UnretryableOutputError → INDETERMINATE, not a hollow verdict); schema-guided json-repair
with a safe fallback; and the RP-01 S2 wire projection carrying a faulty prior reply onto
the retry wire (carried whole when it fits the window, omitted whole when it does not).
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


def test_schema_guided_parse_normalizes_the_result():
    """Behavioral: schema guidance changes the parsed RESULT, not merely a forwarded
    kwarg. The primary, version-stable assertion is ``parse_structured(payload,
    <model>)`` — it returns the schema-COERCED model, turning the ambiguously-typed
    ``"3"`` into an int ``3`` — while ``tolerant_parse(payload)`` with NO schema returns
    the raw ``{"count": "3"}`` (the string).

    Version-stable and installed-library-independent: the coercion here comes from
    pydantic validation inside ``parse_structured`` (``validate_to``), not from
    json-repair's optional ``schema=`` path, so it does not depend on the installed
    json-repair release.
    """
    from pydantic import BaseModel

    class _Counts(BaseModel):
        count: int

    payload = '{"count": "3"}'

    # Schema-less parse: the value stays the raw string "3".
    schemaless = structured.tolerant_parse(payload)
    assert schemaless == {"count": "3"}
    assert isinstance(schemaless["count"], str)

    # Schema-guided parse: the model coerces "3" -> int 3 (the normalized RESULT).
    model = structured.parse_structured(payload, _Counts)
    assert model.count == 3
    assert isinstance(model.count, int)


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
def _capturing_model(texts, usages=None):
    """A FunctionModel that returns ``texts[i]`` on the i-th call (clamping to the last)
    and CAPTURES the user-visible prompt text it received on each call. ``state["i"]``
    counts model calls; ``state["prompts"][i]`` is the concatenated prompt text of call i.

    ``usages`` (optional, parallel to ``texts``) attaches an ``(input_tokens, output_tokens)``
    ``RequestUsage`` to the i-th response so the RP-01 S2 wire-projection fit rule can be
    driven from a test; a ``None`` entry (or omitting ``usages``) leaves the response's usage
    at the FunctionModel default. Offline — no network, no billable call."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    state: dict = {"i": 0, "prompts": []}

    def gen(messages, info):
        chunks = []
        for message in messages:
            for part in getattr(message, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    chunks.append(content)
        state["prompts"].append("\n".join(chunks))
        idx = min(state["i"], len(texts) - 1)
        state["i"] += 1
        kwargs: dict = {"parts": [TextPart(texts[idx])]}
        usage = usages[min(idx, len(usages) - 1)] if usages is not None else None
        if usage is not None:
            from pydantic_ai.usage import RequestUsage

            inp, out = usage
            kwargs["usage"] = RequestUsage(input_tokens=inp, output_tokens=out)
        return ModelResponse(**kwargs)

    return FunctionModel(gen), state


def _structured_req():
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=LLMConfig(repo_path="."),
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def test_reask_echoes_the_models_own_faulty_prior_reply():
    """Behavioral (RP-01 S2 contract): a turn-1 reply that fails structured parse triggers a
    SECOND model call, and because the faulty reply provably fits the candidate window it is
    carried onto the retry wire (as a projected ModelResponse), then the run recovers."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner

    # A non-JSON reply that fails the structured parse. The unique sentinel sits well past
    # the first ~120 chars so it can ONLY reach the reask via the faulty-echo, not via the
    # parse error's short input snippet — making this a genuine guard on the echo itself.
    faulty = "not valid json " * 20 + "REASK_ECHO_SENTINEL_XYZ"
    model, state = _capturing_model(
        [faulty, '{"verdict": "PASS", "findings": [], "summary": "ok"}']
    )
    out = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(_structured_req())

    # 1. A retry actually happened: the model was called TWICE.
    assert state["i"] == 2
    # 2. The turn-2 reask prompt echoes the model's own turn-1 faulty reply verbatim.
    assert faulty in state["prompts"][1]
    # 4. The overall call still returns a valid parsed result after the good turn-2.
    assert out["verdict"] == "PASS"


def test_reask_bounds_a_huge_faulty_prior_reply():
    """Behavioral (RP-01 S2 contract): an oversized faulty turn-1 reply whose projected size
    overflows the candidate window is BOUNDED by whole-omission from the retry wire — never
    carried partially — and the run still recovers on the good turn-2. S2 replaced the old
    fixed-char truncation-with-``[truncated]``-marker echo with this principled window-fit
    projection (a reply that fits is carried whole; one that does not is omitted whole)."""
    from rebar.llm import model_classes
    from rebar.llm.anthropic_model import _pai_model
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner

    cfg = LLMConfig(repo_path=".")
    window = model_classes.own_window_tokens(_pai_model(cfg))
    # The sentinel sits well past the parse-error's bounded input snippet, so it can reach the
    # retry wire ONLY via the carried faulty ModelResponse — never via the short reask error
    # text. With a projected usage that overflows the window, that whole response is omitted, so
    # the deep sentinel is absent. (With a fitting usage it would be present — the fit rule's
    # teeth.)
    sentinel = "DEEP_OMIT_SENTINEL_9Q"
    blob = "not valid json " * 400 + sentinel
    model, state = _capturing_model(
        [blob, '{"verdict": "PASS", "findings": [], "summary": "ok"}'],
        usages=[(window // 2, window), None],
    )
    out = PydanticAIRunner(cfg, model_override=model).run(_structured_req())

    assert state["i"] == 2
    reask = state["prompts"][1]
    # The oversized reply is omitted WHOLE from the retry wire (bounded by the window-fit rule),
    # never carried partially — so the deep sentinel does not survive onto the reask.
    assert sentinel not in reask
    # The call still recovers to a valid parsed result on the good turn-2.
    assert out["verdict"] == "PASS"
