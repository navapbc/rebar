"""RP-01 S3 — HELD-OUT oracle for the usage/failure/artifact compatibility contracts
(ticket [rebar:serge-monotonous-aruanas], 558e-d9bd-e285-40a1).

Withheld from the implementer (who sees only ``test_rp01_s3_exhaustion_happy.py``). Pins the
behavior the identity happy-path cannot:

AC2 — the chain-absent fallback and the negative guard:
  * a repeated-TRUNCATION exhaustion carries NO Rebar object below its bare concision
    ``ModelRetry`` (S2's ``concision_guard`` raises it WITHOUT ``from err``), so the DEFINED
    fallback constructs a fresh ``UnretryableOutputError``;
  * an unrelated ``UnexpectedModelBehavior`` (no exhaustion marker) is left untranslated.
AC2 robustness (plan-review E6/T2/T3) — the cause-chain walk is version-resilient: it follows
  the FULL ``__cause__``/``__context__`` chain to ANY depth (never a fixed number of hops) and
  is cycle-safe, so a pydantic-ai nesting-depth change cannot silently drop the restoration.
AC1 — whole-run accounting: a successful two-attempt run is ONE operation — one durable JSONL
  row, ``requests == 2`` (the aggregate, never per-attempt rows).
AC6 — no ``candidate_requests`` (or any competing per-candidate request-budget field) leaks
  into ``_usage`` or the usage row.

Every runner-level test drives the REAL ``PydanticAIRunner`` over an offline ``FunctionModel``
(ALLOW_MODEL_REQUESTS off); the chain-walk tests call the pure helper directly with synthetic
exception chains so they are deterministic and independent of the provider library version.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMError, StructuredOutputError, UnretryableOutputError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit

_VALID = '{"verdict": "PASS", "findings": [], "summary": "ok"}'


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _scripted_model(script):
    """A ``FunctionModel`` scripted per call (clamped to the last entry). Each entry is a dict:
    ``text`` / ``finish_reason`` / ``usage`` (an ``(input_tokens, output_tokens)`` pair)."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    state = {"calls": 0}

    def gen(messages, info):
        i = state["calls"]
        state["calls"] += 1
        spec = script[min(i, len(script) - 1)]
        kwargs = {
            "parts": [TextPart(spec.get("text", ""))],
            "finish_reason": spec.get("finish_reason"),
        }
        if spec.get("usage") is not None:
            inp, out = spec["usage"]
            kwargs["usage"] = RequestUsage(input_tokens=inp, output_tokens=out)
        return ModelResponse(**kwargs)

    return FunctionModel(gen), state


def _req(cfg):
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def _run(model, *, cfg=None):
    cfg = cfg or LLMConfig(repo_path=".")
    return PydanticAIRunner(cfg, model_override=model).run(_req(cfg))


# ─────────────────────────── AC2 fallback + negative guard ───────────────────────────


def test_truncation_exhaustion_surfaces_a_freshly_constructed_unretryable_error():
    """AC2 chain-absent fallback: every turn is truncated (``finish_reason='length'``), so
    ``concision_guard`` raises a BARE ``ModelRetry`` with no Rebar object on the chain. With
    nothing to restore, the translator must surface a freshly-constructed
    ``UnretryableOutputError`` carrying the exhaustion marker."""
    model, state = _scripted_model([{"text": "x", "finish_reason": "length"}])
    with pytest.raises(UnretryableOutputError) as ei:
        _run(model)
    assert type(ei.value) is UnretryableOutputError
    assert "Exceeded maximum output retries" in str(ei.value)
    assert state["calls"] >= 2, "the budget must actually be exhausted (more than one attempt)"


def test_unrelated_unexpected_model_behavior_is_left_untranslated():
    """AC2 negative guard: an ``UnexpectedModelBehavior`` that is NOT an output-retry
    exhaustion (no marker) must not be mistranslated into a typed output error — it falls
    through to the generic provider-failure path. Proven observably: the raised error is NOT an
    ``UnretryableOutputError`` and the original glitch message survives."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        raise UnexpectedModelBehavior("some totally unrelated model glitch")

    with pytest.raises(LLMError) as ei:
        _run(FunctionModel(gen))
    assert not isinstance(ei.value, StructuredOutputError), (
        "a non-exhaustion UnexpectedModelBehavior must not be translated to an output error"
    )
    assert "some totally unrelated model glitch" in str(ei.value)


# ─────────────────────────── AC2 chain-walk robustness (E6/T2/T3) ─────────────────────


def _chain(depth_wrappers, leaf):
    """Nest ``leaf`` under ``depth_wrappers`` layers of unrelated exceptions linked by
    ``__cause__``, then one layer linked by ``__context__``, returning the outermost exception —
    a synthetic stand-in for the provider library's variable nesting."""
    cur: BaseException = leaf
    for _ in range(depth_wrappers):
        wrapper = RuntimeError("intermediate")
        wrapper.__cause__ = cur
        cur = wrapper
    outer = RuntimeError("terminal")
    outer.__context__ = cur  # reachable only via __context__, not __cause__
    return outer


def test_chain_walk_finds_the_rebar_error_at_any_depth_via_cause_or_context():
    """AC2 robustness: the walk follows BOTH ``__cause__`` and ``__context__`` to arbitrary
    depth — a fixed-hop walk would miss this, so a pydantic-ai nesting change cannot silently
    drop the restoration."""
    from rebar.llm.structured_run import _find_structured_output_error_on_chain

    soe = StructuredOutputError("deep original")
    for depth in (0, 1, 5):
        outer = _chain(depth, soe)
        assert _find_structured_output_error_on_chain(outer) is soe


def test_chain_walk_returns_none_when_no_rebar_error_is_present():
    """AC2 fallback trigger: a chain with no Rebar error yields ``None`` so the caller builds a
    fresh fallback."""
    from rebar.llm.structured_run import _find_structured_output_error_on_chain

    outer = _chain(3, ValueError("not a rebar error"))
    assert _find_structured_output_error_on_chain(outer) is None


def test_chain_walk_is_cycle_safe():
    """A cyclic cause/context graph must terminate, not hang or overflow."""
    from rebar.llm.structured_run import _find_structured_output_error_on_chain

    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__context__ = a  # cycle
    assert _find_structured_output_error_on_chain(a) is None


# ─────────────────────────── AC1 whole-run accounting ────────────────────────────────


def test_successful_two_attempt_run_is_one_operation_with_aggregate_requests(tmp_path, monkeypatch):
    """AC1: an invalid-then-valid success is ONE operation — exactly one durable JSONL row and
    ``_usage['requests'] == 2`` (the whole-run aggregate), never a per-attempt row or a
    single-attempt count. Rejected attempts are not subtracted."""
    cfg = LLMConfig(repo_path=".")
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log_path))

    model, state = _scripted_model(
        [{"text": "not json", "usage": (10, 5)}, {"text": _VALID, "usage": (10, 7)}]
    )
    result = PydanticAIRunner(cfg, model_override=model).run(_req(cfg))

    assert result["verdict"] == "PASS"
    assert state["calls"] == 2
    assert result["_usage"]["requests"] == 2, "whole-run aggregate, not a single attempt"

    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    assert len(rows) == 1, f"exactly one operation row, got {len(rows)}: {rows}"


# ─────────────────────────── AC6 no competing request-budget field ────────────────────


def test_usage_carries_no_candidate_requests_field(tmp_path, monkeypatch):
    """AC6: neither ``_usage`` nor the durable usage row exposes ``candidate_requests`` or any
    other competing per-candidate request-budget field."""
    cfg = LLMConfig(repo_path=".")
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log_path))

    model, _ = _scripted_model([{"text": _VALID, "usage": (10, 7)}])
    result = PydanticAIRunner(cfg, model_override=model).run(_req(cfg))

    assert "candidate_requests" not in result["_usage"]
    rows = [json.loads(line) for line in Path(log_path).read_text().splitlines() if line.strip()]
    assert rows and "candidate_requests" not in rows[0]
