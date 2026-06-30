"""Offline unit tests for the review-bot proven pipe (epic d251 / S4b).

NO live network and NO live LLM: ``rebar.llm.review_code`` is monkeypatched (the
adapter imports it lazily as ``from rebar.llm import review_code``) and the Gerrit
client is a fake that records calls. Async voter coroutines run via ``asyncio.run``
(the repo does not depend on pytest-asyncio).

Covers:
- adapter: clean→PASS, blocking-finding→BLOCK, error→BLOCK (fail-closed);
- dedup: write-on-success + ``already_voted``;
- voter: skip when already voted (dedup OR Gerrit), MAX on PASS, BLOCK value on BLOCK,
  no MAX on a vote-POST failure, single-flight lock serializes same-(change, rev).
"""

from __future__ import annotations

import asyncio

import pytest

from rebar.review_bot import adapter, voter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritError


# ── helpers ─────────────────────────────────────────────────────────────────
def _cfg(tmp_path) -> ReceiverConfig:
    return ReceiverConfig(
        llm_review_max_value=1,
        llm_review_block_value=-1,
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token="tok",
        webhook_token="tok",
        project="rebar",
    )


def _event(change_id="rebar~main~Iabc", revision="rev1", project="rebar") -> dict:
    return {
        "type": "patchset-created",
        "change": {"id": change_id, "number": 42, "project": project},
        "patchSet": {"number": 1, "revision": revision, "ref": "refs/changes/42/42/1"},
    }


class FakeGerrit:
    """Records vote/clone/diff/has-vote calls; no network."""

    def __init__(self, *, has_vote=False, post_status=200, raise_on_post=False):
        self._has_vote = has_vote
        self._post_status = post_status
        self._raise_on_post = raise_on_post
        self.votes: list[tuple] = []
        self.has_vote_calls = 0

    def has_llm_review_vote(self, change_id, revision="current"):
        self.has_vote_calls += 1
        return self._has_vote

    def clone_change_ref(self, change_number, revision_ref, dest):
        return dest

    def get_patch(self, change_id, revision="current"):
        return "diff --git a/x.py b/x.py\n+pass\n"

    def post_vote(self, change_id, revision, value, message, robot_comments=None):
        if self._raise_on_post:
            raise GerritError("post failed", status=self._post_status)
        self.votes.append((change_id, revision, value, message))
        return self._post_status


def _patch_review(monkeypatch, findings):
    import rebar.llm

    def fake_review_code(**kwargs):
        assert kwargs.get("source") == "local"  # adapter must use local mode
        return {"findings": findings, "runner": "fake", "model": None, "trace_id": None}

    monkeypatch.setattr(rebar.llm, "review_code", fake_review_code, raising=False)


# ── adapter ─────────────────────────────────────────────────────────────────
def test_adapter_clean_is_pass(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [])
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=_cfg(tmp_path))
    assert out["decision"] == "PASS"
    assert out["findings"] == []


def test_adapter_low_severity_is_pass(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [{"severity": "low", "dimension": "style", "detail": "nit"}])
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=_cfg(tmp_path))
    assert out["decision"] == "PASS"


@pytest.mark.parametrize("sev", ["critical", "high"])
def test_adapter_blocking_severity_is_block(monkeypatch, tmp_path, sev):
    _patch_review(monkeypatch, [{"severity": sev, "dimension": "security", "detail": "bug"}])
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=_cfg(tmp_path))
    assert out["decision"] == "BLOCK"
    assert any(f["severity"] == sev for f in out["findings"])


def test_adapter_error_is_block_fail_closed(monkeypatch, tmp_path):
    import rebar.llm

    def boom(**kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(rebar.llm, "review_code", boom, raising=False)
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=_cfg(tmp_path))
    assert out["decision"] == "BLOCK"


def test_adapter_unparseable_result_is_block(monkeypatch, tmp_path):
    import rebar.llm

    monkeypatch.setattr(rebar.llm, "review_code", lambda **k: "not a dict", raising=False)
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=_cfg(tmp_path))
    assert out["decision"] == "BLOCK"


def test_adapter_custom_blocking_set(monkeypatch, tmp_path):
    # With only {"medium"} blocking, a high finding now PASSES (config-driven threshold).
    _patch_review(monkeypatch, [{"severity": "high", "dimension": "x", "detail": "y"}])
    cfg = ReceiverConfig(blocking_severities=frozenset({"medium"}), gerrit_bot_token="t")
    out = adapter.code_review_decision("diff", str(tmp_path), "ref", config=cfg)
    assert out["decision"] == "PASS"


# ── dedup ───────────────────────────────────────────────────────────────────
def test_dedup_write_on_success_and_already_voted(tmp_path):
    store = DedupStore(str(tmp_path / "sub" / "voted.db"))  # also exercises mkdir of parent
    assert store.already_voted("c1", "r1") is False
    store.record_vote("c1", "r1", "patchset-created", 1)
    assert store.already_voted("c1", "r1") is True
    # different revision is independent
    assert store.already_voted("c1", "r2") is False
    # idempotent upsert
    store.record_vote("c1", "r1", "patchset-created", -1)
    assert store.already_voted("c1", "r1") is True


# ── voter ───────────────────────────────────────────────────────────────────
def test_voter_skips_other_project(monkeypatch, tmp_path):
    g = FakeGerrit()
    res = asyncio.run(
        voter.review_and_vote(
            _event(project="other"),
            config=_cfg(tmp_path),
            gerrit=g,
            dedup=DedupStore(str(tmp_path / "v.db")),
        )
    )
    assert res["status"] == "skipped"
    assert res["reason"] == "other_project"
    assert g.votes == []


def test_voter_skips_when_dedup_recorded(monkeypatch, tmp_path):
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    store.record_vote("rebar~main~Iabc", "rev1", "patchset-created", 1)
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "skipped"
    assert res["reason"] == "dedup"
    assert g.has_vote_calls == 0  # short-circuited before the Gerrit check
    assert g.votes == []


def test_voter_skips_when_gerrit_already_voted(monkeypatch, tmp_path):
    g = FakeGerrit(has_vote=True)
    res = asyncio.run(
        voter.review_and_vote(
            _event(), config=_cfg(tmp_path), gerrit=g, dedup=DedupStore(str(tmp_path / "v.db"))
        )
    )
    assert res["status"] == "skipped"
    assert res["reason"] == "already_voted_gerrit"
    assert g.votes == []


def test_voter_casts_max_on_pass(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [])  # clean → PASS
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "voted"
    assert res["vote_value"] == 1
    assert g.votes and g.votes[0][2] == 1
    # write-on-success recorded
    assert store.already_voted("rebar~main~Iabc", "rev1") is True


def test_voter_casts_block_on_blocking_finding(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [{"severity": "critical", "dimension": "sec", "detail": "rce"}])
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "voted"
    assert res["vote_value"] == -1
    assert g.votes[0][2] == -1
    assert store.already_voted("rebar~main~Iabc", "rev1") is True


def test_voter_no_max_on_post_failure_and_no_dedup(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [])  # would be PASS, but the POST fails
    g = FakeGerrit(post_status=500, raise_on_post=True)
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "error"
    assert g.votes == []  # no MAX cast on failure
    # NOT recorded — a retry must re-attempt (fail-closed)
    assert store.already_voted("rebar~main~Iabc", "rev1") is False


def test_voter_dedup_check_failure_is_fail_closed(monkeypatch, tmp_path):
    class RaisingGerrit(FakeGerrit):
        def has_llm_review_vote(self, change_id, revision="current"):
            raise GerritError("gerrit unreachable", status=503)

    g = RaisingGerrit()
    res = asyncio.run(
        voter.review_and_vote(
            _event(), config=_cfg(tmp_path), gerrit=g, dedup=DedupStore(str(tmp_path / "v.db"))
        )
    )
    assert res["status"] == "error"
    assert g.votes == []


def test_voter_single_flight_serializes_same_change_rev(monkeypatch, tmp_path):
    """Two concurrent reviews of the SAME (change, rev) → exactly one vote; the second
    sees the dedup row recorded by the first inside the shared lock and skips."""
    _patch_review(monkeypatch, [])
    order: list[str] = []

    class SlowGerrit(FakeGerrit):
        async def _gap(self):
            await asyncio.sleep(0)

        def post_vote(self, change_id, revision, value, message, robot_comments=None):
            order.append("post")
            return super().post_vote(change_id, revision, value, message, robot_comments)

    g = SlowGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    cfg = _cfg(tmp_path)

    async def run_two():
        return await asyncio.gather(
            voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store),
            voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store),
        )

    results = asyncio.run(run_two())
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["skipped", "voted"]  # exactly one voted, the other skipped
    assert len(g.votes) == 1  # single-flight + dedup → one cast


def test_voter_skips_malformed_event(tmp_path):
    res = asyncio.run(
        voter.review_and_vote(
            {"type": "comment-added"},
            config=_cfg(tmp_path),
            gerrit=FakeGerrit(),
            dedup=DedupStore(str(tmp_path / "v.db")),
        )
    )
    assert res["status"] == "skipped"
    assert res["reason"] == "malformed_event"


# ── config ──────────────────────────────────────────────────────────────────
def test_config_from_env_defaults_and_token_alias(monkeypatch):
    for k in (
        "LLM_REVIEW_MAX_VALUE",
        "LLM_REVIEW_BLOCK_VALUE",
        "BLOCKING_SEVERITIES",
        "DEDUP_DB_PATH",
        "GERRIT_BASE_URL",
        "BOT_USER",
        "WEBHOOK_TOKEN",
        "RECONCILE_INTERVAL_SECONDS",
        "GERRIT_PROJECT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GERRIT_BOT_TOKEN", "secret-tok")
    cfg = ReceiverConfig.from_env()
    assert cfg.llm_review_max_value == 1
    assert cfg.llm_review_block_value == -1
    assert cfg.blocking_severities == frozenset({"critical", "high"})
    assert cfg.gerrit_base_url == "http://gerrit:8080"
    assert cfg.reconcile_interval_seconds == 300
    # WEBHOOK_TOKEN defaults to the bot token (ADR-0014)
    assert cfg.webhook_token == "secret-tok"


def test_config_blocking_severities_override(monkeypatch):
    monkeypatch.setenv("GERRIT_BOT_TOKEN", "t")
    monkeypatch.setenv("BLOCKING_SEVERITIES", "critical, high, medium")
    cfg = ReceiverConfig.from_env()
    assert cfg.blocking_severities == frozenset({"critical", "high", "medium"})
