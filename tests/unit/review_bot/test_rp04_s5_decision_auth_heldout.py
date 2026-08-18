"""HELD-OUT edge/behavioural oracle for RP-04 S5 (5851) — AC3.

The implementer does NOT see this file. It asserts the OBSERVABLE contracts that:

* decision-bearing Gerrit auth is validated BEFORE any provider/job work, and a
  blank/whitespace token fails closed with NO vote cast and NO fallback principal
  (``validate_decision_auth`` refuses; the voter casts nothing);
* the composed startup LLM runtime is forwarded provider-native into the runner the
  review actually uses (``get_runner`` is called WITH that exact runtime, not a fresh
  ambient one).

Run: copy into ``tests/unit/review_bot/`` as ``test_rp04_s5_decision_auth_heldout.py``.
Reuses the offline voter harness from ``tests/unit/test_review_bot.py`` (basename import).
"""

from __future__ import annotations

import asyncio

import pytest

# Reuse the proven offline harness (fake gerrit, PASS-verdict stub, canonical event).
from test_review_bot import FakeGerrit, _event, _patch_review  # type: ignore

from rebar.llm.auth import LLMRuntime
from rebar.review_bot import adapter, voter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore


def _cfg(tmp_path, **overrides) -> ReceiverConfig:
    base = dict(
        llm_review_max_value=1,
        llm_review_block_value=-1,
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token="tok",
        webhook_token="tok",
        project="rebar",
    )
    base.update(overrides)
    return ReceiverConfig(**base)


# ── validate_decision_auth: fail-closed on absent decision auth ──────────────
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_validate_decision_auth_refuses_blank_or_whitespace_token(tmp_path, blank) -> None:
    from rebar.review_bot.startup import DecisionAuthError, validate_decision_auth

    with pytest.raises(DecisionAuthError):
        validate_decision_auth(_cfg(tmp_path, gerrit_bot_token=blank))


def test_validate_decision_auth_accepts_a_real_token(tmp_path) -> None:
    """Negative control: a present token passes without raising (the guard distinguishes
    broken from working — it does not blanket-refuse)."""
    from rebar.review_bot.startup import validate_decision_auth

    validate_decision_auth(_cfg(tmp_path, gerrit_bot_token="real-decision-token"))


# ── the voter casts NO vote when decision auth is absent (no fallback) ───────
def test_voter_casts_no_vote_when_decision_auth_is_blank(monkeypatch, tmp_path) -> None:
    """Even on an otherwise-clean PASS path (which WOULD cast a +1), a blank decision
    token means the review fails closed BEFORE provider work: no vote reaches Gerrit and
    the status is not ``voted`` — there is no anonymous/alternate-principal fallback."""
    _patch_review(monkeypatch, [])  # clean → PASS verdict were the review to run
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))

    res = asyncio.run(
        voter.review_and_vote(
            _event(), config=_cfg(tmp_path, gerrit_bot_token=""), gerrit=g, dedup=store
        )
    )

    assert g.votes == []  # THE contract: no vote cast without decision auth
    assert res["status"] != "voted"


def test_voter_casts_the_vote_when_decision_auth_is_present(monkeypatch, tmp_path) -> None:
    """Negative control / teeth anchor: the SAME clean path WITH a token casts the +1, so
    the no-vote above is caused by the missing auth, not a broken harness."""
    _patch_review(monkeypatch, [])
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))

    res = asyncio.run(
        voter.review_and_vote(
            _event(), config=_cfg(tmp_path, gerrit_bot_token="tok"), gerrit=g, dedup=store
        )
    )

    assert res["status"] == "voted"
    assert g.votes and g.votes[0][2] == 1


# ── the composed runtime is forwarded provider-native into the runner ────────
def test_adapter_builds_runner_with_the_forwarded_runtime(tmp_path, monkeypatch) -> None:
    """When a runtime is forwarded, the adapter builds the review's runner WITH that exact
    provider-native runtime (``get_runner(cfg, runtime=<the runtime>)``) — not a fresh
    ambient runner."""
    monkeypatch.setattr(adapter, "_assert_reviewed_tree", lambda *a, **k: None)

    seen: dict = {}
    import rebar.llm.runner as runner_mod

    real_get_runner = runner_mod.get_runner

    def spy_get_runner(config, *, runtime=None, override=None):
        seen["runtime"] = runtime
        return real_get_runner(config, runtime=runtime, override=override)

    monkeypatch.setattr(runner_mod, "get_runner", spy_get_runner)

    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(
        gate_dispatch,
        "produce_code_review_verdict",
        lambda request: {"verdict": "PASS", "coverage": {}},
    )

    rt = LLMRuntime()
    adapter.code_review_decision(
        "diff --git a b", str(tmp_path), "refs/changes/1/1/1", revision="rev1", runtime=rt
    )

    assert "runtime" in seen, "adapter must build the runner via get_runner when a runtime is given"
    assert seen["runtime"] is rt  # the forwarded runtime, provider-native — no ambient swap
