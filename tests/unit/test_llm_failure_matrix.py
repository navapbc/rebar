"""RED baseline: the LLM-failure matrix, pinning rebar's CURRENT behavior (story
gnomish-nosophobic-arawana, epic jira-reb-687).

Injects the full matrix of provider/output failure modes through the pydantic-ai
MODEL layer — a ``FunctionModel`` that RAISES (e.g. ``ModelHTTPError(429)``,
``httpx.ReadTimeout``) or returns a CANNED ``ModelResponse`` (a truncation/
content-filter ``finish_reason``) — driven through ``PydanticAIRunner(model_override=…)``
and the plan-review gate. It asserts what rebar does *today*, so the later stories in
this epic (which add deliberate retry/timeout/classification/silent-success handling)
flip these assertions and this file is their regression net.

THE TEST IS THE SOURCE OF TRUTH for current behavior: each row pins the typed error
the runner ACTUALLY raises (discovered empirically, offline — see the epic's
``tmp/discover_matrix.py`` experiment log), not a pre-asserted guess. Every row carries
an inline ``# CURRENT(<file>:<line>): …`` marker naming the source seam it pins; the
meta-test at the bottom asserts every parametrized id has one.

Coverage boundary: ``model_override`` injects the pydantic-ai MODEL layer, ABOVE the
httpx transport where retry will live (story morbid-uncultured-arcticduck) — so
transport-layer retry is NOT exercisable here. This suite covers classification + gate
behavior + silent-success finish-reasons, not transport. Guarded by
``ALLOW_MODEL_REQUESTS = False`` so a stray real request would fail loudly.

## Verified library behavior (experiment log — pydantic-ai 1.107.0, offline)
```
[1] pydantic_ai.exceptions symbols resolve:
    ModelHTTPError      <: ['ModelAPIError', 'AgentRunError', 'RuntimeError']
    ContentFilterError  <: ['UnexpectedModelBehavior', 'AgentRunError', 'RuntimeError']
    IncompleteToolCall  <: ['UnexpectedModelBehavior', 'AgentRunError', 'RuntimeError']
    UsageLimitExceeded  <: ['AgentRunError', 'RuntimeError', 'Exception']
[2] injected ModelHTTPError propagated: status_code=429 type=ModelHTTPError
[3] canned finish_reason readable: result.response.finish_reason='length'
[4] httpx.ReadTimeout propagated unwrapped: ReadTimeout: read timed out
[5] max_iterations=6 -> request_limit=3, tool_calls_limit=8; =250 -> 125, 250
[6] runner collapses injected 429 -> LLMUnavailableError (current behavior)
```
``ContentFilterError`` / ``IncompleteToolCall`` are real, importable pydantic-ai LIBRARY
symbols (not rebar symbols) — a code-grounding pass restricted to the rebar snapshot must
not read their repo-absence as non-existence (tracked as bug succinct-formable-kite).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from pydantic_ai.exceptions import (
    ContentFilterError,
    IncompleteToolCall,
    ModelHTTPError,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm.config import LLMConfig
from rebar.llm.errors import (
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
    UnretryableOutputError,
)
from rebar.llm.runner import PydanticAIRunner, RunRequest, effective_max_iterations

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _forbid_live_model_requests():
    """A stray REAL model request on any path in this file must fail loudly, never bill.
    (The repo conftest also sets this; we assert it belt-and-suspenders.)"""
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False
    yield


# ── Offline model builders (the injection seam) ───────────────────────────────
def _raise(exc: BaseException) -> FunctionModel:
    """A FunctionModel that RAISES ``exc`` — the provider/exception-propagation path."""

    def gen(messages, info):
        raise exc

    return FunctionModel(gen)


def _canned(text: str, *, finish_reason: str | None = None) -> FunctionModel:
    """A FunctionModel that returns a CANNED response with an optional normalized
    ``finish_reason`` — the silent-success / truncation path (raises NO exception; the
    structured stack reads ``finish_reason`` via ``structured.check_stop_reason``)."""

    def gen(messages, info):
        return ModelResponse(parts=[TextPart(text)], finish_reason=finish_reason)

    return FunctionModel(gen)


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("repo_path", ".")
    return LLMConfig(**kw)


def _run(model: FunctionModel, *, mode: str = "findings", cfg: LLMConfig | None = None):
    cfg = cfg or _cfg()
    req = RunRequest(
        system_prompt="you are a reviewer",
        instructions="review this",
        config=cfg,
        reviewers=["v"],
        mode=mode,
    )
    return PydanticAIRunner(cfg, model_override=model).run(req)


# ── The runner-level classification matrix ────────────────────────────────────
@dataclass(frozen=True)
class Case:
    id: str
    make_model: Callable[[], FunctionModel]
    expects: type[BaseException]
    marker: str  # CURRENT(<file>:<line>): <one-line behavior it pins>


# CURRENT behavior, discovered empirically: EVERY provider HTTP error / network timeout /
# raised model exception collapses into the single opaque LLMUnavailableError at the
# runner's generic ``except Exception`` seam (runner.py:353-365). 429 is indistinguishable
# from 401 from a connect-timeout — the exact opacity this epic replaces. The canned
# finish_reason cases (content_filter / length) instead reach the structured stack's
# check_stop_reason and raise UnretryableOutputError; a canned non-JSON body fails output
# validation as StructuredOutputError; a model-raised UsageLimitExceeded maps to
# LLMRunnerError. Each row's `marker` names the seam; the meta-test enforces one per id.
MATRIX: list[Case] = [
    Case(
        "429-rate",
        lambda: _raise(ModelHTTPError(429, "m", body={"error": {"type": "rate_limit_error"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 429 rate-limit -> opaque LLMUnavailableError",
    ),
    Case(
        "429-insufficient_quota",
        lambda: _raise(ModelHTTPError(429, "m", body={"error": {"type": "insufficient_quota"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 429 insufficient_quota (body) -> LLMUnavailableError",
    ),
    Case(
        "529-overloaded",
        lambda: _raise(ModelHTTPError(529, "m", body={"error": {"type": "overloaded_error"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 529 overloaded -> LLMUnavailableError (not retried today)",
    ),
    Case(
        "500-server-error",
        lambda: _raise(ModelHTTPError(500, "m", body={})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 500 -> LLMUnavailableError",
    ),
    Case(
        "503-unavailable",
        lambda: _raise(ModelHTTPError(503, "m", body={})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 503 -> LLMUnavailableError",
    ),
    Case(
        "connect-timeout",
        lambda: _raise(httpx.ConnectTimeout("connect timed out")),
        LLMUnavailableError,
        "CURRENT(runner.py:365): httpx.ConnectTimeout -> LLMUnavailableError",
    ),
    Case(
        "read-timeout",
        lambda: _raise(httpx.ReadTimeout("read timed out")),
        LLMUnavailableError,
        "CURRENT(runner.py:365): httpx.ReadTimeout -> LLMUnavailableError",
    ),
    Case(
        "401-auth",
        lambda: _raise(ModelHTTPError(401, "m", body={"error": {"type": "authentication_error"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 401 auth -> LLMUnavailableError (same class as a 429)",
    ),
    Case(
        "403-permission",
        lambda: _raise(ModelHTTPError(403, "m", body={"error": {"type": "permission_error"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 403 permission -> LLMUnavailableError",
    ),
    Case(
        "400-bad-request",
        lambda: _raise(ModelHTTPError(400, "m", body={"error": {"type": "invalid_request_error"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 400 bad-request -> LLMUnavailableError",
    ),
    Case(
        "400-context-length",
        lambda: _raise(ModelHTTPError(400, "m", body={"error": {"message": "prompt is too long"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 400 context-length -> LLMUnavailableError",
    ),
    Case(
        "413-too-large",
        lambda: _raise(ModelHTTPError(413, "m", body={"error": {"type": "request_too_large"}})),
        LLMUnavailableError,
        "CURRENT(runner.py:365): 413 request-too-large -> LLMUnavailableError",
    ),
    Case(
        "content_filter-finish_reason",
        lambda: _canned('{"verdict":"PASS"}', finish_reason="content_filter"),
        UnretryableOutputError,
        "CURRENT(structured.py:243): canned finish_reason=content_filter -> UnretryableOutputError",
    ),
    Case(
        "structured-refusal-exception",
        lambda: _raise(ContentFilterError("the model refused")),
        LLMUnavailableError,
        "CURRENT(runner.py:365): raised ContentFilterError -> LLMUnavailableError",
    ),
    Case(
        "length-truncation-finish_reason",
        lambda: _canned('{"verdict":"PA', finish_reason="length"),
        UnretryableOutputError,
        "CURRENT(structured.py:243): canned length finish_reason -> UnretryableOutputError",
    ),
    Case(
        "incomplete-tool-call-exception",
        lambda: _raise(IncompleteToolCall("incomplete tool call")),
        LLMUnavailableError,
        "CURRENT(runner.py:365): raised IncompleteToolCall -> LLMUnavailableError",
    ),
    Case(
        "unparseable-output",
        lambda: _canned("this is not json at all, no verdict here"),
        StructuredOutputError,
        "CURRENT(runner.py:352): unparseable output -> StructuredOutputError (passthrough)",
    ),
    Case(
        "step-budget-usage-limit",
        lambda: _raise(UsageLimitExceeded("would exceed the request_limit of 3")),
        LLMRunnerError,
        "CURRENT(runner.py:346): UsageLimitExceeded -> LLMRunnerError (step budget)",
    ),
    Case(
        # DISTINCT failure CLASS from the step-budget row above: a UsageLimitExceeded that
        # trips on the TOOL-CALLS ceiling (tool_calls_limit=max(8,max_iterations)), not the
        # request/step budget (request_limit=ceil(max_iterations/2)). Today the runner's ONE
        # `except UsageLimitExceeded` handler collapses BOTH sub-classes into the same opaque
        # LLMRunnerError — the tool-call-runaway signal is indistinguishable from step-budget
        # exhaustion (the opacity a later story splits). Discovered empirically: LLMRunnerError.
        "tool-call-limit-usage-limit",
        lambda: _raise(
            UsageLimitExceeded("The next request would exceed the tool_calls_limit of 8")
        ),
        LLMRunnerError,
        "CURRENT(runner.py:390): tool-call-limit UsageLimitExceeded -> LLMRunnerError "
        "(same handler as step budget)",
    ),
    Case(
        # Validation-RETRY EXHAUSTION (distinct from `unparseable-output`): the body is VALID
        # JSON but the WRONG SHAPE, so it clears the tolerant parse yet fails Pydantic schema
        # validation. The PromptedOutput loop feeds the validation error back and retries
        # OUTPUT_RETRIES(2)+1 = 3 times; every attempt re-fails (the canned model is static),
        # so the loop exhausts and `raise last` surfaces the final StructuredOutputError. This
        # is the validation-retry-exhaustion → StructuredOutputError seam (gate INDETERMINATE).
        # Discovered empirically (3 model calls, then StructuredOutputError).
        "validation-retry-exhaustion",
        lambda: _canned('{"wrong_field": 1, "also_wrong": true}'),
        StructuredOutputError,
        "CURRENT(runner.py:579): valid-JSON wrong-schema exhausts the bounded output retry "
        "-> StructuredOutputError (raise last)",
    ),
    Case(
        "unknown-exception",
        lambda: _raise(RuntimeError("some novel provider failure")),
        LLMUnavailableError,
        "CURRENT(runner.py:365): unknown Exception -> LLMUnavailableError (catch-all)",
    ),
]


@pytest.mark.parametrize("case", MATRIX, ids=[c.id for c in MATRIX])
def test_runner_level_classification_pins_current_behavior(case: Case):
    """Each matrix row: injecting the failure through the model layer raises exactly the
    typed error the runner produces TODAY. Later stories flip these to distinct classes."""
    with pytest.raises(case.expects):
        _run(case.make_model())


def test_text_mode_does_not_read_finish_reason_today():
    """CURRENT(runner.py:325-328): mode='text' bypasses the structured stack, so a
    truncation finish_reason is NOT detected — the run returns OK. This is exactly the
    silent-success gap story polite-dutiful-drake closes; pinned green here."""
    out = _run(_canned("a partial answer", finish_reason="length"), mode="text")
    assert out["text"] == "a partial answer"


def test_text_mode_still_collapses_provider_error():
    """CURRENT(runner.py:365): a raised provider error collapses in text mode too."""
    with pytest.raises(LLMUnavailableError):
        _run(_raise(ModelHTTPError(429, "m", body={})), mode="text")


# ── Step-budget / tool-call-limit: the DERIVED limits (never pydantic-ai's 50) ─
@pytest.mark.parametrize(
    ("max_iterations", "exp_request_limit", "exp_tool_calls_limit"),
    [(2, 1, 8), (5, 3, 8), (6, 3, 8), (250, 125, 250)],
)
def test_runner_computes_derived_usage_limits(
    monkeypatch, max_iterations, exp_request_limit, exp_tool_calls_limit
):
    """CURRENT(runner.py:310-314): the runner derives request_limit=max(1,ceil(mi/2)) and
    tool_calls_limit=max(8,mi) from cfg.max_iterations — NOT pydantic-ai's default
    UsageLimits(request_limit=50). Observed by spying on the UsageLimits it constructs."""
    # Sanity-pin the formula itself (the numbers the later stories must not silently change).
    assert exp_request_limit == max(1, math.ceil(max_iterations / 2))
    assert exp_tool_calls_limit == max(8, max_iterations)

    captured: list[dict] = []
    import pydantic_ai.usage as _usage

    real = _usage.UsageLimits

    def _spy(**kwargs):
        captured.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(_usage, "UsageLimits", _spy)
    # A well-formed structured reply so the run reaches finalize without a spurious error.
    try:
        _run(_canned('{"analysis":"","findings":[]}'), cfg=_cfg(max_iterations=max_iterations))
    except Exception:  # noqa: BLE001 — output-shape is irrelevant; we only pin the LIMITS built
        pass
    assert captured, "runner must construct a UsageLimits"
    kw = captured[-1]
    assert kw["request_limit"] == exp_request_limit
    assert kw["tool_calls_limit"] == exp_tool_calls_limit
    assert kw["request_limit"] != 50  # not the inherited pydantic-ai default


def test_effective_max_iterations_floor_and_override():
    """CURRENT(runner.py:502): a per-call req.config may only RAISE the operator floor."""
    assert effective_max_iterations(6, None) == 6
    assert effective_max_iterations(6, 20) == 20  # request raises the floor
    assert effective_max_iterations(20, 6) == 20  # request cannot lower the floor


# ── Gate-level: two-scope SYSTEMIC failure → INDETERMINATE (unsigned) ──────────
def _plan_ctx(root: str):
    """A minimal leaf-task PlanContext rooted at ``root`` (an isolated tmp dir, so the
    code-read-root checkpoint cache never leaks into the real repo)."""
    from rebar.llm.plan_review.det_floor import PlanContext

    return PlanContext(
        ticket_id="rec-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description=(
            "## Acceptance Criteria\n- [ ] the widget is observably correct\n"
            "- [ ] unit tests cover it\n\nImplement the widget in src/rebar/foo.py."
        ),
        repo_root=root,
        tickets_root=root,
    )


def test_plan_review_gate_degrades_to_indeterminate_on_systemic_outage(tmp_path):
    """CURRENT(gate_dispatch.py:136,173,206): a SYSTEMIC provider failure (injected 429)
    mid plan-review degrades to an unsigned INDETERMINATE verdict (coverage.llm_unavailable
    =True, llm_ran=False) — never a hollow PASS, never a signature. The two-scope 'systemic'
    arm; per-criterion fail-open is pinned separately below."""
    from rebar.llm.workflow import gate_dispatch

    root = str(tmp_path)
    cfg = _cfg(repo_path=root)
    runner = PydanticAIRunner(
        cfg,
        model_override=_raise(
            ModelHTTPError(429, "m", body={"error": {"type": "rate_limit_error"}})
        ),
    )
    verdict = gate_dispatch.produce_plan_review_verdict(
        _plan_ctx(root), cfg, runner=runner, advisory_cap=20
    )
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["coverage"]["llm_unavailable"] is True
    assert verdict["coverage"]["llm_ran"] is False
    assert not verdict.get("signature")


# ── Two-scope: per-criterion (NON-systemic) failure fails OPEN ────────────────
# The fail-open decision lives in the Pass-1 size-ladder (sizing.pass1_with_ladder):
# a SYSTEMIC LLMUnavailableError is RE-RAISED (whole tier down → the caller degrades the
# whole verdict to INDETERMINATE); any other, non-context failure DROPS that finder's
# findings (returns []) so one flaky finder among N never aborts the review.
class _SystemicRunner:
    """A runner whose every call is a SYSTEMIC outage (LLMUnavailableError)."""

    name = "systemic"

    def preflight(self) -> None:  # offline-ready
        return None

    def run(self, req):
        raise LLMUnavailableError("simulated systemic outage")


class _PerChunkFailingRunner:
    """A runner that raises a NON-systemic (non-LLMUnavailableError, non-context) failure —
    the 'one flaky finder among N' case the ladder must drop, not abort on."""

    name = "flaky-finder"

    def preflight(self) -> None:
        return None

    def run(self, req):
        raise StructuredOutputError("this one finder produced nothing usable")


def _ladder(runner):
    from rebar.llm.plan_review import registry, sizing

    return sizing.pass1_with_ladder(
        runner,
        _cfg(),
        plan="Implement the widget.",
        chunk=[registry.by_id()["E2"]],
        agentic=False,
        events=[],
    )


def test_pass1_ladder_reraises_systemic_failure():
    """CURRENT(sizing.py:229-230): a SYSTEMIC finder failure is re-raised (the whole LLM
    tier is down), so run_review can degrade the verdict to INDETERMINATE — never silently
    drop findings (fuel-posse-ball)."""
    with pytest.raises(LLMUnavailableError):
        _ladder(_SystemicRunner())


def test_pass1_ladder_drops_nonsystemic_finder_failopen():
    """CURRENT(sizing.py:231-233): a NON-systemic finder failure DROPS that finder's
    findings (returns []) and never aborts — the per-criterion fail-open resilience later
    stories must preserve. Contrast the systemic case above, which re-raises."""
    out = _ladder(_PerChunkFailingRunner())
    assert out == []  # findings dropped, no raise


# ── Guard: a disabled feature NEVER enters the LLM path (model must not be called) ─
class _ExplodingRunner:
    """Asserts it is never invoked — the guard-path invariant."""

    name = "exploding"

    def preflight(self) -> None:
        return None

    def run(self, req):
        raise AssertionError("the runner MUST NOT be invoked on a disabled/skip path")


def test_disabled_code_review_never_invokes_runner():
    """CURRENT(code_review/shim.py:136-139): with code-review disabled (the default), the
    gate returns an inert empty review WITHOUT touching the runner — the feature-off path
    short-circuits before any model call. Later stories must keep failure-handling code out
    of this path entirely."""
    from rebar.llm.code_review.shim import review_code

    out = review_code(
        base="HEAD~1",
        head="HEAD",
        diff_text="--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n",
        runner=_ExplodingRunner(),  # would raise AssertionError if the guard let it through
        repo_root=".",
    )
    assert out["findings"] == []  # inert result, no LLM ran


# ── Guard: --force / --force-close SKIP the plan-review / completion gates (no LLM) ─
def test_force_claim_skips_plan_review_gate(monkeypatch):
    """CURRENT(gates.py:120): with the plan-review claim gate ENABLED, a non-empty
    ``force_reason`` (the ``claim --force="<reason>"`` bypass) short-circuits and returns
    None BEFORE the gate check runs — proving the injected gate call is NEVER invoked on the
    --force path. A non-force claim DOES reach the check (positive control), so the skip is
    attributable to --force, not to the gate being off. The claim gate is a fast LOCAL HMAC
    verify (no billable model call), so the ``claim_gate_check`` seam stands in for the gate
    work the bypass must skip."""
    from rebar._commands import gates as _gates

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    calls: list[str] = []

    def _spy_gate_check(ticket_id, *, repo_root=None):
        calls.append(ticket_id)
        return {"ok": True}  # a valid attestation, so a NON-force claim would ALSO return None

    import rebar.llm as _llm

    monkeypatch.setattr(_llm, "claim_gate_check", _spy_gate_check)

    # --force path: returns None WITHOUT ever calling the gate check.
    assert _gates.plan_review_precheck("rec-0000", ".", None, force_reason="approved") is None
    assert calls == [], "claim_gate_check MUST NOT run on the --force claim path"

    # Positive control: without --force the same enabled gate DOES invoke the check.
    assert _gates.plan_review_precheck("rec-0000", ".", None, force_reason="") is None
    assert calls == ["rec-0000"], "the gate check must run when --force is absent"


def test_force_close_skips_completion_gate(monkeypatch):
    """CURRENT(transition_close.py:114): with the completion-verification close gate ENABLED,
    a non-empty ``force_close`` (the ``--force-close="<reason>"`` bypass) short-circuits and
    returns None BEFORE the billable ``verify_completion`` LLM call — proving the model is
    NEVER invoked on the --force-close path. A non-force close DOES reach ``verify_completion``
    (positive control), so the skip is attributable to --force-close."""
    from rebar._commands import gates as _gates
    from rebar._commands import transition_close as _tc

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    # Neutralize the deterministic file-impact precheck that sits before the LLM call, so the
    # non-force control reaches verify_completion regardless of the local tracker/git state.
    from rebar._engine_support import field_reads as _fr

    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])
    calls: list[str] = []

    def _spy_verify(ticket_id, **kwargs):
        calls.append(ticket_id)
        # PASS from a "local" source so the precheck returns None right after the call
        # (no signing manifest / git read) — we only pin THAT the model ran.
        return {"verdict": "PASS", "source": "local"}

    import rebar.llm as _llm

    monkeypatch.setattr(_llm, "verify_completion", _spy_verify)

    # --force-close path: returns None WITHOUT ever calling verify_completion.
    assert (
        _tc._completion_precheck("rec-0000", "task", ".", None, reason="", force_close="approved")
        is None
    )
    assert calls == [], "verify_completion MUST NOT run on the --force-close path"

    # Positive control: without --force-close the same enabled gate DOES invoke the LLM verify.
    assert (
        _tc._completion_precheck("rec-0000", "task", ".", None, reason="", force_close="") is None
    )
    assert calls == ["rec-0000"], "verify_completion must run when --force-close is absent"


# ── Meta-test: every parametrized id carries a CURRENT(<file>:<line>) marker ───
def test_every_matrix_case_pins_a_current_source_seam():
    """Each row documents the exact seam it pins via a CURRENT(<file>:<line>) marker, and
    those markers appear inline in THIS file's source (the 'source citation' this baseline
    requires) so a later story flipping a row is forced to update the cited seam."""
    src = Path(__file__).read_text()
    for case in MATRIX:
        assert case.marker.startswith("CURRENT("), case.id
        # <file>:<line> shape — a real source anchor, not a bare description.
        anchor = case.marker[len("CURRENT(") : case.marker.index(")")]
        assert ":" in anchor and anchor.rsplit(":", 1)[1].isdigit(), case.marker
    # The inline `# CURRENT(` comments outnumber the matrix rows (rows + gate/guard seams).
    assert src.count("CURRENT(") >= len(MATRIX)
