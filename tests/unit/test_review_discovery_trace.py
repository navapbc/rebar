"""RP-06 S2 — the discovery kernel's stored trace/envelope safety contract.

The typed trace is SAFE stored diagnostic data: normalized ids, outcomes, reason codes,
usage, and lineage only. It must never carry a raw prompt, a ticket/context body, a
provider payload, or a secret — and it must not change any public review result schema.

These are held-out oracle tests (the implementation subagent does not see them).
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.review_kernel.discovery import (
    DISCOVERY_NAMESPACE_VERSION,
    DiscoveryStagePlan,
    DiscoveryUnitPlan,
    Usage,
    execute_stage,
    unit_trace,
)

pytestmark = pytest.mark.unit

_SECRET = "sk-super-secret-provider-token-DO-NOT-LEAK"
_PROMPT_BODY = "You are a reviewer. Here is the full ticket body: <<<confidential plan text>>>"
_CONTEXT_BODY = "PROVIDER RESPONSE: the model said <<<raw completion text>>>"


def _unit(unit_id: str, *, deps: tuple[str, ...] = ()) -> DiscoveryUnitPlan:
    return DiscoveryUnitPlan(
        unit_id=unit_id,
        dependencies=deps,
        prompt_id=f"prompt::{unit_id}",
        contract_id="contract::v1",
        model="test-model",
        mode="single",
        context_digest=f"ctx::{unit_id}",
        policy_digest="pol",
        blocking=True,
        budget_estimate=1.0,
    )


def _stage(units):
    return DiscoveryStagePlan(
        units=tuple(units),
        budget=None,
        material="material-fingerprint",
        code_ref="ref::abcdef",
        topology_digest="topo::v1",
    )


class _SecretRunner:
    """A model-boundary fake whose returned content embeds a raw prompt / provider payload /
    secret — exactly the material the trace must exclude."""

    def __init__(self):
        self.dispatched = []

    def __call__(self, unit):
        self.dispatched.append(unit.unit_id)
        content = {
            "prompt": _PROMPT_BODY,
            "provider_response": _CONTEXT_BODY,
            "api_key": _SECRET,
            "findings": [{"criterion": unit.unit_id, "detail": "some finding"}],
        }
        return (content, Usage(input_tokens=50, output_tokens=8, requests=1))


def _serialized(trace) -> str:
    # a trace must be JSON-serializable stored data.
    return json.dumps(trace, sort_keys=True)


def test_trace_carries_normalized_outcome_metadata_and_lineage() -> None:
    a = _unit("a")
    b = _unit("b", deps=("a",))
    result = execute_stage(_stage([a, b]), run_unit=_SecretRunner())

    b_out = next(o for o in result.outcomes if o.unit_id == "b")
    trace = unit_trace(b_out, unit_plan=b)
    # normalized identity + outcome + lineage present.
    assert trace["unit_id"] == "b"
    assert trace["kind"] == "success"
    assert trace["usage"]["requests"] == 1
    assert trace["lineage"]["dependencies"] == ["a"]
    assert trace["namespace_version"] == DISCOVERY_NAMESPACE_VERSION


def test_trace_excludes_raw_prompt_context_body_provider_payload_and_secret() -> None:
    u = _unit("u")
    result = execute_stage(_stage([u]), run_unit=_SecretRunner())
    out = result.outcomes[0]

    blob = _serialized(unit_trace(out, unit_plan=u))
    assert _SECRET not in blob
    assert _PROMPT_BODY not in blob
    assert _CONTEXT_BODY not in blob
    # even the raw content payload keys must not be dumped verbatim.
    assert "provider_response" not in blob
    assert "api_key" not in blob


def test_trace_for_a_failure_carries_reason_code_but_no_bodies() -> None:
    # a failed/skip trace records the reason code (normalized) and still no raw bodies.
    from rebar.llm.review_kernel.discovery import LocalOperationExhausted

    class _Boom:
        def __call__(self, unit):
            raise LocalOperationExhausted(f"{_SECRET} leaked into an exception message")

    u = _unit("f")
    result = execute_stage(_stage([u]), run_unit=_Boom())
    out = result.outcomes[0]
    assert out.kind == "failed"
    trace = unit_trace(out, unit_plan=u)
    blob = _serialized(trace)
    # a normalized reason code is present, but the raw secret-bearing exception text is NOT.
    assert trace["kind"] == "failed"
    assert _SECRET not in blob


def test_unit_trace_does_not_expose_envelope_content_payload() -> None:
    # the envelope's ``content`` (the provider result) is checkpoint data, not trace data.
    u = _unit("c")
    result = execute_stage(_stage([u]), run_unit=_SecretRunner())
    out = result.outcomes[0]
    trace = unit_trace(out, unit_plan=u)
    # lineage may reference the digest, but never the content itself.
    assert "content" not in _serialized(trace)
    assert trace["lineage"]["digest"] == out.envelope.digest
