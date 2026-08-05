"""The structured-output reliability stack (1268): deterministic tolerant parse
(json-repair), Pydantic validators with bounds, output-mode selection, stop_reason
handling, and a structured-output validity eval — all offline (no model call).
"""

from __future__ import annotations

import json as _json
from pathlib import Path as _Path

import pytest
from pydantic import BaseModel, field_validator

from rebar.llm import structured
from rebar.llm.errors import StructuredOutputError


class _Verdict(BaseModel):
    verdict: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _bound(cls, v: float) -> float:
        # BOUNDS live in the validator (NOT the JSON Schema, to stay inside Anthropic's
        # strict-grammar subset) — they fire during validate_to.
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v


# ── Layer 2: deterministic tolerant parse (json-repair) ────────────────────────

_NEAR_MISS = {
    "strict": '{"verdict": "PASS"}',
    "markdown_fence": '```json\n{"verdict": "PASS"}\n```',
    "trailing_comma": '{"verdict": "PASS",}',
    "unclosed_brace": '{"verdict": "PASS"',
    "single_quotes": "{'verdict': 'PASS'}",
    "prose_wrapped": 'Sure! Here is the result: {"verdict": "PASS"} — hope that helps.',
}


@pytest.mark.parametrize("name", sorted(_NEAR_MISS))
def test_tolerant_parse_repairs_near_miss(name):
    parsed = structured.tolerant_parse(_NEAR_MISS[name])
    assert isinstance(parsed, dict) and parsed.get("verdict") == "PASS"


def test_tolerant_parse_first_object_wins_on_multi_object():
    # A draft-then-correction (two objects) must pick the FIRST deterministically —
    # NOT json-repair's last-wins surprise.
    parsed = structured.tolerant_parse('{"verdict": "PASS"} {"verdict": "FAIL"}')
    assert parsed == {"verdict": "PASS"}


def test_tolerant_parse_empty_is_error():
    with pytest.raises(StructuredOutputError, match="empty"):
        structured.tolerant_parse("   ")


def test_tolerant_parse_unparseable_is_error():
    with pytest.raises(StructuredOutputError):
        structured.tolerant_parse("the model declined entirely, no json here at all")


# ── Layer 3: validators (bounds in the validator, not the schema) ──────────────


def test_validate_to_accepts_valid():
    obj = structured.validate_to(_Verdict, {"verdict": "PASS", "confidence": 0.9})
    assert obj.verdict == "PASS" and obj.confidence == 0.9


def test_validate_to_enforces_validator_bounds():
    with pytest.raises(StructuredOutputError, match="confidence"):
        structured.validate_to(_Verdict, {"verdict": "PASS", "confidence": 1.5})


def test_validate_to_rejects_non_object():
    with pytest.raises(StructuredOutputError, match="object"):
        structured.validate_to(_Verdict, ["not", "a", "dict"])


def test_validate_to_unwraps_single_element_array():
    # Bug artsy-chain-hold/dash-lure-slag: the completion-verifier's model sometimes
    # emits its structured verdict wrapped in a top-level JSON array (a bare one-element
    # list) instead of the bare object, deterministically blocking a close fail-closed.
    # A single-element array carrying one object must be unwrapped, not rejected.
    obj = structured.validate_to(_Verdict, [{"verdict": "PASS", "confidence": 0.9}])
    assert obj.verdict == "PASS" and obj.confidence == 0.9


def test_parse_structured_unwraps_top_level_array():
    # End-to-end through the deterministic layers: a top-level array string parses+validates.
    obj = structured.parse_structured('[{"verdict": "FAIL"}]', _Verdict)
    assert obj.verdict == "FAIL"


def test_validate_to_still_rejects_multi_element_array():
    # A genuine multi-OBJECT array is ambiguous (which verdict?) and must stay an error —
    # unwrapping recovers a list's SOLE dict element, so two dicts remain ambiguous.
    with pytest.raises(StructuredOutputError, match="object"):
        structured.validate_to(_Verdict, [{"verdict": "PASS"}, {"verdict": "FAIL"}])


def test_validate_to_recovers_sole_dict_among_non_dict_noise():
    # Bug slit-rubble-braid: a REASONING completion-verifier model emits a valid top-level
    # JSON array that concatenates echoed intermediate arrays it reasoned over with the real
    # verdict object as the sole dict element, e.g. [["open","in_progress"], {verdict}]. This
    # parses whole via json.loads (a list), and the length-1 unwrap does not catch it, so it
    # was rejected "got list" on every bounded retry — fail-closing the completion gate and
    # blocking every close. The sole dict element must be recovered.
    obj = structured.validate_to(
        _Verdict, [["open", "in_progress"], {"verdict": "PASS", "confidence": 0.9}]
    )
    assert obj.verdict == "PASS" and obj.confidence == 0.9


def test_parse_structured_recovers_verdict_from_array_with_leading_noise():
    # End-to-end reproduction of the captured runtime payload: a valid JSON-array string with
    # leading non-dict elements and the verdict object last parses + validates to the verdict.
    obj = structured.parse_structured(
        '[["idea","open","in_progress"], ["open","in_progress"], {"verdict": "FAIL"}]', _Verdict
    )
    assert obj.verdict == "FAIL"


def test_tolerant_parse_skips_nonjson_brace_before_object():
    # Bug 67ee / messianic-wild-dassie: the completion-verifier model prepended a prose
    # preamble whose GitHub Actions expression ``${{ github.repository }}`` puts a NON-JSON
    # '{' BEFORE the real trailing verdict object. _first_json_object anchored on that first
    # '{', failed to parse the ``{{ … }}`` region, and gave up — so json-repair then mangled
    # the whole text into a list of prose fragments (zero dicts), and validate_to rejected it
    # "got list", fail-closing every close of that ticket. The scan must ADVANCE past the
    # non-JSON brace to the first '{' that actually parses.
    text = (
        "The fix adds `GH_REPO: ${{ github.repository }}` to the release step, resolving "
        '"fatal: not a git repository".\n\n'
        '{"verdict": "PASS", "confidence": 0.9}'
    )
    parsed = structured.tolerant_parse(text)
    assert isinstance(parsed, dict) and parsed.get("verdict") == "PASS"


def test_completion_verdict_recovered_when_prose_holds_github_actions_expr():
    # Faithful end-to-end reproduction of the CAPTURED 67ee runtime payload: prose containing
    # ``${{ github.repository }}`` before the verdict object, whose own ``summary`` also holds
    # that expression (the '{{' inside a JSON string must be string-aware, not a brace). Must
    # recover + validate to a real CompletionVerdict, not fail-close "got list".
    from rebar.llm.contracts import completion_verdict_response_model

    model = completion_verdict_response_model()
    text = (
        "The fix is clearly present. The `GH_REPO: ${{ github.repository }}` env var was "
        'added to the release step, resolving "fatal: not a git repository".\n\n'
        '{"verdict": "PASS", "findings": [], '
        '"summary": "Fixed by adding GH_REPO: ${{ github.repository }} at line 99."}'
    )
    obj = structured.validate_to(model, structured.tolerant_parse(text))
    assert obj.verdict == "PASS" and obj.findings == []


def test_parse_structured_combines_layers():
    # A near-miss (fenced + trailing comma) that nonetheless yields a valid verdict.
    obj = structured.parse_structured(
        '```json\n{"verdict": "PASS", "confidence": 0.5,}\n```', _Verdict
    )
    assert obj.verdict == "PASS" and obj.confidence == 0.5


# ── Layer 1: output-mode selection ─────────────────────────────────────────────


def test_output_mode_native_for_enforcing_providers():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import NativeOutput, PromptedOutput

    from rebar.llm.capabilities import capabilities_for

    assert isinstance(
        structured.output_mode(_Verdict, capabilities_for("openai:gpt-4o")), NativeOutput
    )
    assert isinstance(
        structured.output_mode(_Verdict, capabilities_for("google-gla:gemini-2.5-flash")),
        NativeOutput,
    )
    # Anthropic (and unknown providers) -> PromptedOutput (safe, thinking-compatible).
    assert isinstance(
        structured.output_mode(_Verdict, capabilities_for("anthropic:claude-opus-4-8")),
        PromptedOutput,
    )


def test_output_mode_forces_prompted_under_thinking():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import PromptedOutput

    from rebar.llm.capabilities import capabilities_for

    # Even a native-capable provider must NOT use forced/native constraint with extended
    # thinking (the documented Anthropic 400 / provider incompatibility).
    assert isinstance(
        structured.output_mode(_Verdict, capabilities_for("openai:gpt-4o"), thinking=True),
        PromptedOutput,
    )


# ── stop_reason handling ───────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["refusal", "max_tokens", "length", "content_filter", "error"])
def test_check_stop_reason_raises_on_bad(reason):
    # Both raw Anthropic stop reasons AND Pydantic AI normalized finish reasons.
    with pytest.raises(StructuredOutputError):
        structured.check_stop_reason(reason)


@pytest.mark.parametrize("reason", ["end_turn", "tool_use", "stop", "tool_call", None])
def test_check_stop_reason_passes_on_normal(reason):
    structured.check_stop_reason(reason)  # no raise


# ── Bounds in the REAL contract models (not just the toy) ──────────────────────


def test_real_completion_verdict_normalizes_garbled_to_fail():
    # M2: the production CompletionVerdict's validator (bounds in the validator, not the
    # schema) coerces a garbled/truncated verdict to FAIL — fail-safe, never silent PASS.
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    assert Model(verdict="pass").verdict == "PASS"  # normalized
    assert Model(verdict="PA").verdict == "FAIL"  # garbled/truncated -> fail-safe
    assert Model(verdict="anything").verdict == "FAIL"


def test_real_finding_clamps_out_of_range_confidence():
    from rebar.llm.findings import finding_model

    Finding = finding_model()
    f = Finding(severity="high", dimension="x", detail="d", confidence=1.5)
    assert f.confidence == 1.0  # clamped into [0, 1] by the validator


# ── The structured-output validity eval (the gate) ─────────────────────────────


def test_structured_validity_eval_meets_threshold():
    # A corpus of realistic near-miss model outputs; the deterministic stack must
    # recover a valid structured verdict from >= 99% of them (the recall/false-accept
    # gate that authorizes retiring the second-interpreter LLM). Only genuinely
    # content-empty output (no recoverable JSON) is allowed to fail.
    recoverable = list(_NEAR_MISS.values()) + [
        '{"verdict":"FAIL","confidence":0.2}',
        'Result:\n```\n{"verdict": "PASS"}\n```',
        '{"verdict": "PASS"  "confidence": 0.7}',  # missing comma
        '{\n  "verdict": "PASS",\n  "confidence": 1.0\n}',
    ]
    # Include a GENUINELY unrecoverable element so `validity` is a real fraction (not a
    # tautological 1.0): the stack must recover EVERY recoverable item AND reject the
    # unrecoverable one, so validity over the recoverable subset is exactly 1.0 while
    # the unrecoverable one raises.
    corpus = recoverable + ["the model declined; there is no json anywhere here"]
    recovered = sum(1 for text in recoverable if _recovers(text))
    assert recovered == len(recoverable), "a recoverable near-miss was not recovered"
    assert not _recovers(corpus[-1]), "the unrecoverable item must NOT be fabricated into a value"
    validity = recovered / len(recoverable)
    assert validity >= 0.99, f"structured validity {validity:.2%} below the 99% gate"


def _recovers(text: str) -> bool:
    try:
        structured.validate_to(_Verdict, structured.tolerant_parse(text))
        return True
    except StructuredOutputError:
        return False


def test_structured_validity_eval_false_accept_arm():
    # The other half of the gate (the AC says recall AND false-accept): output the stack
    # must NOT silently turn into a wrong-but-valid object. Each case must EITHER raise
    # OR recover the CORRECT value — never fabricate. Uses the REAL CompletionVerdict so
    # its fail-safe verdict validator participates.
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    cases = [
        # (input, expected_verdict_or_None)  — None means "must raise, not fabricate".
        ('{"verdict":"PASS"} {"verdict":"FAIL"}', "PASS"),  # first-wins, not last
        ('{"verdict": "PA', "FAIL"),  # truncated -> json-repair -> fail-safe FAIL
        ("the model refused, no json", None),  # nothing recoverable -> must raise
    ]
    false_accepts = 0
    for text, expected in cases:
        # Only a parse/validation rejection is an expected "did not recover" — narrow the
        # except so an UNEXPECTED error (a real bug) surfaces instead of being miscounted.
        try:
            obj = Model(**structured.tolerant_parse(text))
            if expected is None or obj.verdict != expected:
                false_accepts += 1  # fabricated a wrong/unexpected value
        except StructuredOutputError:
            if expected is not None:
                false_accepts += 1  # should have recovered, didn't
    assert false_accepts == 0, f"{false_accepts} false-accept(s): garbage produced wrong output"


def test_no_second_interpreter_model_call_for_a_repairable_response():
    # BEHAVIORAL proof (not a source grep): a near-miss reply that the DETERMINISTIC
    # stack can repair must be finalized WITHOUT a second model call. We count how many
    # times the model is invoked for a structured request whose first reply is a
    # repairable near-miss — it must be exactly ONE (json-repair + validators do the
    # rest; no extract-via-second-LLM step).
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    calls = {"n": 0}

    def gen(messages, info):
        calls["n"] += 1
        # A fenced + trailing-comma near-miss the tolerant parse recovers deterministically.
        return ModelResponse(parts=[TextPart('```json\n{"verdict": "PASS", "findings": [],}\n```')])

    cfg = LLMConfig(model="anthropic:claude-opus-4-8", repo_path=".")
    out = PydanticAIRunner(cfg, model_override=FunctionModel(gen)).run(
        RunRequest(
            system_prompt="x",
            instructions="y",
            config=cfg,
            reviewers=["v"],
            mode="structured",
            output_schema="completion_verdict",
        )
    )
    assert out["verdict"] == "PASS"
    assert calls["n"] == 1, f"a repairable reply triggered {calls['n']} model calls (expected 1)"


def test_citation_model_coerces_out_of_enum_kind() -> None:
    # A model may emit a citation kind outside the closed enum (e.g. 'code'); the shared
    # Citation model must coerce it (path→file, url→url, else source) so it doesn't fail the
    # whole structured output — which falsely blocked the completion close gate (dogfood bug).
    from rebar.llm.findings import citation_model

    C = citation_model()
    assert C(kind="code", path="a.py", line_start=1).kind == "file"
    assert C(kind="code", url="http://x").kind == "url"
    assert C(kind="bogus", description="evidence").kind == "source"
    assert C(kind="file", path="a.py").kind == "file"  # valid kinds untouched


def test_completion_verdict_accepts_coerced_citation_kind() -> None:
    # End-to-end: a completion_verdict whose finding cites kind='code' validates (coerced to
    # 'file') instead of raising 'code is not one of [file, url, source]'.
    from rebar import schemas
    from rebar.llm import contracts

    model = contracts.response_model_for("completion_verdict")
    obj = model.model_validate(
        {
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "X",
                    "detail": "missing",
                    "citations": [{"kind": "code", "path": "a.py", "line_start": 1, "line_end": 2}],
                }
            ],
        }
    )
    dumped = obj.model_dump(exclude_none=True)
    schemas.validator(schemas.COMPLETION_VERDICT).validate(dumped)
    assert dumped["findings"][0]["citations"][0]["kind"] == "file"


# ── df3a: schema-filtered candidate selection (last-valid-wins) ─────────────────
# The completion verifier's agentic transcript quotes dependency-link records from
# show_ticket tool results; today's schema-BLIND first-parseable-object selection lets a
# quoted record win over the real verdict that appears later, fail-closing a legitimate
# close. parse_structured now enumerates candidate JSON objects, screens each by top-level
# key-overlap against the target model's fields, validates the survivors, and takes the
# LAST valid one. Corpus fixtures replay the captured b586 reply shape.

_CORPUS_DIR = _Path(__file__).parent.parent / "fixtures" / "structured_reply_corpus"


def _load_corpus(name: str) -> dict:
    return _json.loads((_CORPUS_DIR / f"{name}.json").read_text())


def _all_corpus() -> list[dict]:
    return [_json.loads(p.read_text()) for p in sorted(_CORPUS_DIR.glob("v*.json"))]


def test_selection_recovers_verdict_over_quoted_dep_record():
    # HAPPY PATH: a completion-verifier reply that quotes a dependency-link record (0 shared
    # top-level keys with CompletionVerdict) before the real verdict object must parse to the
    # VERDICT, not the dep record. Today the first parseable object (the dep record) wins and
    # fails validation ("verdict Field required"), blocking the close.
    from rebar.llm.contracts import completion_verdict_response_model

    model = completion_verdict_response_model()
    reply = _load_corpus("v1_bare_dep_before_verdict")["reply"]
    obj = structured.parse_structured(reply, model)
    assert obj.verdict == "PASS"


@pytest.mark.parametrize("variant", _all_corpus(), ids=lambda v: v["id"])
def test_selection_recovers_verdict_all_corpus_variants(variant):
    # The captured b586 reply in 4 recorded layout variants (bare-dep-first, fenced-dep-first,
    # both-fenced-verdict-last, and the verdict-first control that already passes today). All
    # four must recover the REAL verdict after the fix; 3 of 4 fail on today's first-wins parser.
    from rebar.llm.contracts import completion_verdict_response_model

    model = completion_verdict_response_model()
    obj = structured.parse_structured(variant["reply"], model)
    assert obj.verdict == variant["expected_verdict"]


def test_selection_last_valid_wins_and_warns_when_two_validate(caplog):
    # When >=2 candidates validate and last != first, last-valid-wins takes the LAST and logs a
    # warning naming both (a model that emits a draft verdict then a correction). The corrected
    # (last) verdict is authoritative; the warning surfaces the ambiguity.
    import logging

    reply = (
        'Draft assessment: {"verdict": "FAIL", "summary": "initial read, criteria unclear"}\n\n'
        "On closer inspection every criterion is met, so my final verdict is:\n"
        '{"verdict": "PASS", "summary": "final: all acceptance criteria demonstrably met"}'
    )
    with caplog.at_level(logging.WARNING):
        obj = structured.parse_structured(reply, _Verdict)
    assert obj.verdict == "PASS"
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_selection_falls_back_byte_for_byte_when_no_candidate():
    # AC3: a reply with NO schema-valid candidate falls back to today's pipeline byte-for-byte
    # (same exception type AND message). The declined-entirely fixture has no parseable object,
    # so selection finds nothing to screen and defers to tolerant_parse+validate_to unchanged.
    text = "the model declined entirely, no json here at all"
    with pytest.raises(StructuredOutputError) as new_exc:
        structured.parse_structured(text, _Verdict)
    # Byte-for-byte: identical to running the deterministic layers directly.
    with pytest.raises(StructuredOutputError) as old_exc:
        structured.validate_to(_Verdict, structured.tolerant_parse(text, schema=_Verdict))
    assert str(new_exc.value) == str(old_exc.value)


def test_selection_property_payload_last_recovers_verdict():
    # Property: prose interleaved with zero-overlap JSON decoys (dep-link / tool-result shapes)
    # with the REAL verdict payload rendered LAST always parses to the verdict. seed=20250804,
    # n=500 (the E3 prototype ran 500/500).
    import random

    from rebar.llm.contracts import completion_verdict_response_model

    model = completion_verdict_response_model()
    rng = random.Random(20250804)
    decoy_templates = [
        '{{"relation": "{r}", "target_id": "{t}"}}',
        '{{"link_uuid": "{u}", "target_id": "{t}", "relation": "{r}"}}',
        '{{"criterion": "{c}", "detail": "quoted from a prior finding"}}',
        '{{"id": "{t}", "status": "{s}"}}',
        '{{"path": "src/rebar/llm/{c}.py", "line_start": {n}, "line_end": {n}}}',
    ]
    relations = ["depends_on", "blocks", "relates_to", "discovered_from", "caused_by"]
    statuses = ["open", "in_progress", "closed", "idea"]
    for _ in range(500):
        parts = ["I reviewed the ticket graph and its acceptance criteria."]
        for _ in range(rng.randint(1, 4)):
            tmpl = rng.choice(decoy_templates)
            decoy = tmpl.format(
                r=rng.choice(relations),
                t=f"{rng.randrange(16**4):04x}-{rng.randrange(16**4):04x}",
                u=f"{rng.randrange(16**8):08x}",
                c=rng.choice(["structured", "capabilities", "contracts"]),
                s=rng.choice(statuses),
                n=rng.randint(1, 400),
            )
            if rng.random() < 0.5:
                decoy = f"```json\n{decoy}\n```"
            parts.append(f"For context: {decoy}")
        want = rng.choice(["PASS", "FAIL"])
        payload = _json.dumps(
            {"verdict": want, "findings": [], "summary": "final verdict rendered last"}
        )
        parts.append(f"My final verdict is:\n{payload}")
        reply = "\n\n".join(parts)
        obj = structured.parse_structured(reply, model)
        assert obj.verdict == want, reply
