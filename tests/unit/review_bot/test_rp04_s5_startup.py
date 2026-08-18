"""RP-04 S5 (5851) happy-path oracle — review-bot startup binding + decision-auth
validation + LLM-runtime forwarding.

These are the SPECIFICATION tests for the intended, well-formed behavior:

* ``compose_startup_binding(cfg)`` composes an immutable, non-secret startup binding
  ONCE, carrying an :class:`~rebar.llm.auth.LLMRuntime` for provider-native LLM auth.
* ``validate_decision_auth(cfg)`` accepts a present decision-bearing Gerrit token and
  raises the typed :class:`DecisionAuthError` on a blank one — the guard that must run
  before any provider/job work.
* ``adapter.code_review_decision(..., runtime=rt)`` forwards the composed runtime into a
  real runner injected on the ``CodeReviewRequest`` (the LLM-auth cutover seam), instead
  of leaving the gate to build an ambient runner.

Only observable behaviour/contracts are asserted (return types, immutability, the typed
raise, the injected runner) — never private structure.
"""

from __future__ import annotations

import dataclasses

import pytest

from rebar.llm.auth import LLMRuntime
from rebar.review_bot.config import ReceiverConfig


def _cfg(**overrides) -> ReceiverConfig:
    base = dict(gerrit_bot_token="tok", webhook_token="tok", project="rebar")
    base.update(overrides)
    return ReceiverConfig(**base)


# ── startup binding composition ──────────────────────────────────────────────
def test_compose_startup_binding_exposes_an_llm_runtime() -> None:
    from rebar.review_bot.startup import StartupBinding, compose_startup_binding

    binding = compose_startup_binding(_cfg())
    assert isinstance(binding, StartupBinding)
    assert isinstance(binding.llm_runtime, LLMRuntime)


def test_startup_binding_is_immutable() -> None:
    from rebar.review_bot.startup import compose_startup_binding

    binding = compose_startup_binding(_cfg())
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        binding.llm_runtime = LLMRuntime()  # type: ignore[misc]


# ── decision-auth validation (fail-closed, before provider work) ─────────────
def test_validate_decision_auth_accepts_a_present_token() -> None:
    from rebar.review_bot.startup import validate_decision_auth

    # A present decision-bearing token: the guard returns without raising.
    validate_decision_auth(_cfg(gerrit_bot_token="present-decision-token"))


def test_validate_decision_auth_rejects_a_blank_token() -> None:
    from rebar.review_bot.startup import DecisionAuthError, validate_decision_auth

    with pytest.raises(DecisionAuthError):
        validate_decision_auth(_cfg(gerrit_bot_token=""))


# ── LLM-runtime forwarding through the adapter seam ──────────────────────────
def test_code_review_decision_forwards_runtime_into_the_request_runner(
    tmp_path, monkeypatch
) -> None:
    """A composed runtime given to the adapter is threaded into a real runner injected on
    the ``CodeReviewRequest`` (``request.runner`` is populated), rather than left as the
    ambient ``None`` the gate would otherwise resolve itself."""
    from rebar.review_bot import adapter

    # Bind-to-revision assertion is orthogonal to this seam.
    monkeypatch.setattr(adapter, "_assert_reviewed_tree", lambda *a, **k: None)

    captured: dict = {}

    def fake_produce(request):
        captured["runner"] = request.runner
        return {"verdict": "PASS", "coverage": {}}

    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", fake_produce)

    out = adapter.code_review_decision(
        "diff --git a b",
        str(tmp_path),
        "refs/changes/1/1/1",
        revision="rev1",
        runtime=LLMRuntime(),
    )

    assert out["decision"] == "PASS"
    # The runtime forced the adapter to construct and inject a runner.
    assert captured["runner"] is not None
