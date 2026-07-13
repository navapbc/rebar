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


def test_schema_guided_parse_normalizes_the_result():
    """Behavioral: a schema threaded into tolerant_parse changes the parsed RESULT — an
    ambiguously-typed field (``"3"``) comes back as a normalized int (``3``) under an
    integer schema, versus the raw string on the schema-less path. This exercises real
    (unmonkeypatched) json-repair, so it proves the schema actually steers the VALUE, not
    merely that a kwarg was forwarded.

    Installed-library-independent: json-repair's ``schema=`` coercion is version-dependent
    (some releases ignore it). We probe the installed lib first and ``skip`` with a clear
    reason if it does not coerce, rather than asserting a behavior it cannot provide.
    """
    from pydantic import BaseModel

    class _Counts(BaseModel):
        count: int
        name: str

    # Malformed enough that strict ``json.loads`` AND the balanced-object scan both fail
    # (a trailing comma does it), so tolerant_parse is forced down to the json-repair
    # layer — the only layer that consults the schema.
    malformed = '{"count": "3", "name": "x",}'

    # Probe: does the installed json_repair honor schema= coercion at all?
    import json_repair

    probe = json_repair.repair_json(malformed, return_objects=True, schema=_Counts)
    coerces = (
        isinstance(probe, dict) and probe.get("count") == 3 and isinstance(probe["count"], int)
    )
    if not coerces:
        pytest.skip(
            "installed json_repair does not honor schema= coercion "
            f"(probe returned {probe!r}); schema-guided parsing is a no-op here"
        )

    schemaless = structured.tolerant_parse(malformed)
    guided = structured.tolerant_parse(malformed, schema=_Counts)

    # The schema-guided VALUE is normalized to the schema-typed int...
    assert guided["count"] == 3
    assert isinstance(guided["count"], int)
    # ...and genuinely DIFFERS from the schema-less parse (which leaves it the string "3").
    assert schemaless["count"] != guided["count"]
    assert schemaless["count"] == "3"


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
def _capturing_model(texts):
    """A FunctionModel that returns ``texts[i]`` on the i-th call (clamping to the last)
    and CAPTURES the user-visible prompt text it received on each call. ``state["i"]``
    counts model calls; ``state["prompts"][i]`` is the concatenated prompt text of call i.
    Offline — no network, no billable call."""
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
        return ModelResponse(parts=[TextPart(texts[idx])])

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
    """Behavioral: a turn-1 reply that fails structured parse triggers a SECOND model call
    whose prompt echoes the model's OWN faulty prior reply, and the run then recovers."""
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
    """Behavioral: an oversized turn-1 faulty reply is echoed BOUNDED (truncated) into the
    reask — the full blob is not passed back whole, and a ``[truncated]`` marker appears."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner

    # A distinctive, clearly-non-JSON blob far longer than the echo bound, using a rare
    # char so we can count how much of it survives into the reask.
    huge = "Z" * 5000
    model, state = _capturing_model([huge, '{"verdict": "PASS", "findings": [], "summary": "ok"}'])
    out = PydanticAIRunner(LLMConfig(repo_path="."), model_override=model).run(_structured_req())

    assert state["i"] == 2
    reask = state["prompts"][1]
    # 3. The echo is BOUNDED: the full 5000-char blob is NOT passed back whole, the echoed
    # portion is capped (~2000 chars + marker), and a truncation marker is present.
    assert huge not in reask
    assert reask.count("Z") <= 2500  # bounded near the ~2000 cap, nowhere near the 5000 blob
    assert "[truncated]" in reask
    # 4. The call still recovers to a valid parsed result on the good turn-2.
    assert out["verdict"] == "PASS"
