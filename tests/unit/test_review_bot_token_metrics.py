"""Token-usage observability for the AWS code-review bot (ticket clayish-basaltine-bug).

Two seams, both offline (no live LLM, no real AWS):
- `finalize._attach_code_review_metrics` sums per-call `_usage` token fields into
  `coverage['metrics']`.
- `voter._publish_token_usage_metrics` / `_emit_token_usage` publish those counts to
  CloudWatch (best-effort boto3) and a greppable journald marker, and `review_and_vote`
  invokes them after a successful vote.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from rebar.llm.code_review.finalize import _attach_code_review_metrics
from rebar.review_bot import voter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore

pytestmark = pytest.mark.unit


# ── finalize: token aggregation into coverage.metrics ────────────────────────
def _rec(steps):
    return types.SimpleNamespace(steps=steps)


def test_finalize_sums_usage_tokens_across_steps():
    verdict: dict = {"verdict": "PASS", "blocking": [], "advisory": []}
    rec = _rec(
        [
            {
                "status": "succeeded",
                "kind": "agent",
                "step_id": "verify",
                "duration_ms": 10,
                "outputs": {
                    "_usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_tokens": 5,
                        "cache_write_tokens": 2,
                        "requests": 3,
                    }
                },
            },
            {
                "status": "succeeded",
                "kind": "agent",
                "step_id": "decide",
                "duration_ms": 5,
                "outputs": {"_usage": {"input_tokens": 50, "output_tokens": 10}},
            },
            # A non-usage batch step contributes 0 tokens.
            {
                "status": "succeeded",
                "kind": "batch",
                "step_id": "batch",
                "duration_ms": 3,
                "outputs": {"criteria_count": 4},
            },
            # A FAILED step is skipped entirely (its usage must not be counted).
            {
                "status": "failed",
                "kind": "agent",
                "step_id": "verify",
                "outputs": {"_usage": {"input_tokens": 9999}},
            },
        ]
    )
    _attach_code_review_metrics(verdict, rec, 100.0)
    m = verdict["coverage"]["metrics"]
    assert m["input_tokens"] == 150
    assert m["output_tokens"] == 30
    assert m["cache_read_tokens"] == 5
    assert m["cache_write_tokens"] == 2
    assert m["total_tokens"] == 180


def test_finalize_no_usage_yields_zero_tokens_and_never_raises():
    verdict: dict = {"verdict": "PASS", "blocking": [], "advisory": []}
    rec = _rec(
        [
            {
                "status": "succeeded",
                "kind": "batch",
                "step_id": "batch",
                "outputs": {"criteria_count": 1},
            }
        ]
    )
    _attach_code_review_metrics(verdict, rec, 12.0)
    m = verdict["coverage"]["metrics"]
    assert m["input_tokens"] == 0
    assert m["output_tokens"] == 0
    assert m["cache_read_tokens"] == 0
    assert m["cache_write_tokens"] == 0
    assert m["total_tokens"] == 0


# ── voter: CloudWatch publish ────────────────────────────────────────────────
def _install_fake_boto3(monkeypatch, sink):
    client = types.SimpleNamespace(put_metric_data=lambda **kw: sink.append(kw))
    fake = types.SimpleNamespace(client=lambda service: client)
    monkeypatch.setitem(sys.modules, "boto3", fake)


def test_publish_token_usage_metrics_emits_five_named_metrics(monkeypatch):
    sink: list = []
    _install_fake_boto3(monkeypatch, sink)
    voter._publish_token_usage_metrics(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 5,
            "cache_write_tokens": 2,
            "total_tokens": 120,
        }
    )
    assert len(sink) == 1
    call = sink[0]
    assert call["Namespace"] == "rebar/host"
    by_name = {d["MetricName"]: d for d in call["MetricData"]}
    assert by_name["review_bot_llm_input_tokens"]["Value"] == 100.0
    assert by_name["review_bot_llm_output_tokens"]["Value"] == 20.0
    assert by_name["review_bot_llm_cache_read_tokens"]["Value"] == 5.0
    assert by_name["review_bot_llm_cache_write_tokens"]["Value"] == 2.0
    assert by_name["review_bot_llm_total_tokens"]["Value"] == 120.0
    assert all(d["Unit"] == "Count" for d in call["MetricData"])


def test_publish_token_usage_metrics_all_zero_is_noop(monkeypatch):
    sink: list = []
    _install_fake_boto3(monkeypatch, sink)
    voter._publish_token_usage_metrics({"input_tokens": 0, "output_tokens": 0})
    assert sink == []  # nothing recorded ⇒ nothing published


def test_publish_token_usage_metrics_swallows_boto3_absence(monkeypatch):
    # `import boto3` raises ImportError when the module entry is None.
    monkeypatch.setitem(sys.modules, "boto3", None)
    # Must not raise despite non-zero token data.
    voter._publish_token_usage_metrics({"input_tokens": 100, "total_tokens": 100})


def test_emit_token_usage_writes_marker_and_swallows_publish_error(monkeypatch, capsys):
    def boom(_metrics):
        raise RuntimeError("cloudwatch down")

    monkeypatch.setattr(voter, "_publish_token_usage_metrics", boom)
    # Must not raise even though the publisher errors.
    voter._emit_token_usage("rebar~main~Iabc", "rev1", {"input_tokens": 7, "total_tokens": 7})
    err = capsys.readouterr().err
    assert "LLM_TOKEN_USAGE" in err  # greppable journald marker emitted


# ── integration: review_and_vote emits usage after a successful vote ─────────
class _FakeGerrit:
    """Minimal Gerrit fake for the non-merge PASS path."""

    def __init__(self):
        self.votes: list = []

    def has_llm_review_vote(self, change_id, revision="current"):
        return False

    def get_commit(self, change_id, revision="current"):
        return {"parents": [{"commit": "p0"}], "message": "subject\n\nrebar-ticket: 20cc"}

    def clone_change_ref(self, change_number, revision_ref, dest):
        return None

    def get_patch(self, change_id, revision="current"):
        return "diff --git a/x b/x\n+one\n"

    def post_vote(self, change_id, revision, value, message, robot_comments=None):
        self.votes.append((change_id, revision, value, message))
        return 200


def _cfg(tmp_path) -> ReceiverConfig:
    return ReceiverConfig(
        llm_review_max_value=1,
        llm_review_block_value=-1,
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token="tok",
        webhook_token="tok",
        project="rebar",
    )


def _event() -> dict:
    return {
        "type": "patchset-created",
        "change": {"id": "rebar~main~Iabc", "number": 42, "project": "rebar"},
        "patchSet": {"number": 1, "revision": "rev1", "ref": "refs/changes/42/42/1"},
    }


def test_review_and_vote_emits_token_usage_after_successful_vote(monkeypatch, tmp_path):
    metrics = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [],
        "coverage": {"llm_ran": True, "metrics": metrics},
    }
    # Stub the four-pass gate the adapter calls (offline; no LLM).
    import rebar.llm.workflow.gate_dispatch as gd

    monkeypatch.setattr(gd, "produce_code_review_verdict", lambda request: verdict, raising=True)
    # Avoid touching the ambient tickets store for the artifact side-effect.
    monkeypatch.setattr(voter, "emit_code_review_artifact", lambda *a, **k: None)

    captured: list = []
    monkeypatch.setattr(
        voter, "_emit_token_usage", lambda cid, rev, m: captured.append((cid, rev, m))
    )

    res = asyncio.run(
        voter.review_and_vote(
            _event(),
            config=_cfg(tmp_path),
            gerrit=_FakeGerrit(),
            dedup=DedupStore(str(tmp_path / "v.db")),
        )
    )
    assert res["status"] == "voted"
    assert captured == [("rebar~main~Iabc", "rev1", metrics)]
