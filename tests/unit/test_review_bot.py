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
import dataclasses
import datetime
import json
import logging
import pathlib
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest
from _healthcheck_oracles import assert_socket_healthcheck_semantics, healthcheck_test_argv

from rebar.review_bot import adapter, reconcile, voter
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


def test_candidate_events_skips_closed_changes():
    # Bug c943: the backfill reconciler re-voted MERGED/ABANDONED changes, drawing a 409
    # "change is closed" that the voter records as a (non-actionable) voter_error and — since
    # no dedup row is written on failure — re-attempts forever. _candidate_events must skip
    # changes Gerrit considers CLOSED. Open (NEW) and status-ABSENT changes MUST still be
    # candidates (fail-open: never drop a live change on missing metadata, which would risk
    # skipping a real open change and stalling the LLM-Review gate).
    def ev(cid, status):
        e = _event(change_id=cid, revision="r_" + cid)
        if status is not None:
            e["change"]["status"] = status
        return e

    events = [
        ev("c-new", "NEW"),
        ev("c-merged", "MERGED"),
        ev("c-abandoned", "ABANDONED"),
        ev("c-nostatus", None),
    ]
    candidates = reconcile._candidate_events(events, "rebar")
    assert set(candidates) == {"c-new", "c-nostatus"}


class FakeGerrit:
    """Records vote/clone/diff/has-vote calls; no network. ``parents=1`` (default) is a
    NON-merge revision (the get_patch path); ``parents>=2`` routes the voter through the
    merge-change path (get_merge_files / get_file_diff / get_mergelist), epic 88ab / S2."""

    # mirror the real client's magic-pseudo-path set so merge tests can reference it
    MAGIC_PATHS = frozenset({"/COMMIT_MSG", "/MERGE_LIST"})

    def __init__(
        self,
        *,
        has_vote=False,
        post_status=200,
        raise_on_post=False,
        parents=1,
        merge_files=None,
        file_diffs=None,
        mergelist=None,
        raise_on=None,
    ):
        self._has_vote = has_vote
        self._post_status = post_status
        self._raise_on_post = raise_on_post
        self._parents = parents
        self._merge_files = merge_files or {}
        self._file_diffs = file_diffs or {}
        self._mergelist = mergelist or []
        # name of a merge-path method that should raise GerritError (fail-closed tests)
        self._raise_on = raise_on
        self.votes: list[tuple] = []
        self.has_vote_calls = 0
        self.get_patch_calls = 0

    def has_llm_review_vote(self, change_id, revision="current"):
        self.has_vote_calls += 1
        return self._has_vote

    def clone_change_ref(self, change_number, revision_ref, dest):
        return dest

    def get_patch(self, change_id, revision="current"):
        self.get_patch_calls += 1
        return "diff --git a/x.py b/x.py\n+pass\n"

    # ── merge-change path (S2) ──────────────────────────────────────────────
    def get_commit(self, change_id, revision="current"):
        if self._raise_on == "get_commit":
            raise GerritError("commit fetch failed", status=500)
        return {"parents": [{"commit": f"p{i}"} for i in range(self._parents)]}

    def get_merge_files(self, change_id, revision="current"):
        if self._raise_on == "get_merge_files":
            raise GerritError("files fetch failed", status=500)
        return dict(self._merge_files)

    def get_file_diff(self, change_id, file_path, revision="current"):
        if self._raise_on == "get_file_diff":
            raise GerritError("diff fetch failed", status=500)
        return self._file_diffs.get(file_path, {"content": []})

    def get_mergelist(self, change_id, revision="current"):
        if self._raise_on == "get_mergelist":
            raise GerritError("mergelist fetch failed", status=500)
        return list(self._mergelist)

    def post_vote(self, change_id, revision, value, message, robot_comments=None, comments=None):
        if self._raise_on_post:
            raise GerritError("post failed", status=self._post_status)
        self.votes.append((change_id, revision, value, message))
        return self._post_status


def _verdict_from_findings(findings):
    """Build a four-pass ``code_review_verdict`` from ``{severity,dimension,detail}`` findings: any
    critical/high finding → a blocking entry (verdict BLOCK), the rest advisory (PASS if none)."""
    blocking = [f for f in findings if str(f.get("severity", "")).lower() in ("critical", "high")]
    advisory = [f for f in findings if f not in blocking]

    def _entry(f, sev):
        return {
            "finding": f.get("detail", ""),
            "criteria": [f.get("dimension", "general")],
            "severity": sev,
        }

    return {
        "verdict": "BLOCK" if blocking else "PASS",
        "blocking": [_entry(f, "critical") for f in blocking],
        "advisory": [_entry(f, "minor") for f in advisory],
        "coverage": {"llm_ran": True},
    }


def _patch_verdict(monkeypatch, verdict):
    """Stub the four-pass gate the adapter now calls (WS6)."""
    import rebar.llm.workflow.gate_dispatch as gd

    monkeypatch.setattr(gd, "produce_code_review_verdict", lambda request: verdict, raising=True)


def _patch_review(monkeypatch, findings):
    """Back-compat helper for the voter tests: stub the gate to a verdict derived from findings."""
    _patch_verdict(monkeypatch, _verdict_from_findings(findings))


# ── adapter (four-pass verdict → decision; WS6) ──────────────────────────────
def test_adapter_clean_is_pass(monkeypatch, tmp_path):
    _patch_verdict(
        monkeypatch,
        {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {"llm_ran": True}},
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "PASS" and out["coverage_gap"] is False
    assert out["message"].startswith("[LLM-Review: PASS]")


def test_adapter_threads_change_id_into_gate_request(monkeypatch, tmp_path):
    """Gerrit change-keying (epic super-path-bag): ``code_review_decision`` forwards ``change_id``
    into the ``CodeReviewRequest``, so the region-gated novelty floor uses the ``change:<id>``
    keyspace for Gerrit finding-memory — the analogue of the local ``session:<id>`` key. This is the
    end-to-end wiring that makes 'Gerrit review memory is keyed on the Gerrit change' live."""
    import rebar.llm.workflow.gate_dispatch as gd

    captured: dict = {}

    def _capture(request):
        captured["change_id"] = request.change_id
        return {"verdict": "PASS", "blocking": [], "advisory": [], "coverage": {"llm_ran": True}}

    monkeypatch.setattr(gd, "produce_code_review_verdict", _capture, raising=True)
    adapter.code_review_decision("diff", str(tmp_path), "ref", change_id="Ideadbeef")
    assert captured["change_id"] == "Ideadbeef"
    # default (no change_id supplied) is "" — a bare/non-Gerrit invocation stays unkeyed
    adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert captured["change_id"] == ""


def test_adapter_blocking_finding_is_block(monkeypatch, tmp_path):
    _patch_verdict(
        monkeypatch,
        {
            "verdict": "BLOCK",
            "blocking": [{"finding": "rce", "criteria": ["security"], "location": "a.py:1"}],
            "coverage": {},
        },
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is False
    assert out["message"].startswith("[LLM-Review: BLOCK — finding]")
    assert any(f["detail"] == "rce" for f in out["findings"])


@pytest.mark.parametrize(
    ("finding_count", "expected_overflow"),
    [
        (10, None),
        (11, "1 additional blocking finding omitted from this summary."),
        (12, "2 additional blocking findings omitted from this summary."),
    ],
)
def test_adapter_discloses_truncated_blocking_summary(
    monkeypatch, tmp_path, finding_count, expected_overflow
):
    long_detail = "actionable finding detail " * 20
    blocking = [
        {
            "finding": f"{long_detail}{i}",
            "criteria": ["quality"],
            "location": f"x.py:{i + 1}",
        }
        for i in range(finding_count)
    ]
    assert len(long_detail) > 240
    assert len(blocking) == finding_count
    assert len(blocking) >= 10
    _patch_verdict(
        monkeypatch,
        {
            "verdict": "BLOCK",
            "blocking": blocking,
            "advisory": [],
            "coverage": {},
        },
    )

    out = adapter.code_review_decision("diff", str(tmp_path), "ref")

    bullets = [line for line in out["message"].splitlines() if line.startswith("- ")]
    assert len(bullets) == 10
    rendered_detail = bullets[0].split(") ", 1)[1].rsplit(" [", 1)[0]
    expected_detail = f"{long_detail}0"
    assert rendered_detail == f"{expected_detail[:239]}…"
    assert len(rendered_detail) == 240
    if expected_overflow is None:
        assert "omitted" not in out["message"]
    else:
        overflow = out["message"].splitlines()[-1]
        assert overflow == expected_overflow
    assert len(out["findings"]) == finding_count
    assert out["findings"][0]["detail"] == expected_detail


def test_adapter_preserves_blocking_detail_at_summary_limit(monkeypatch, tmp_path):
    boundary_detail = "x" * 240
    _patch_verdict(
        monkeypatch,
        {
            "verdict": "BLOCK",
            "blocking": [{"finding": boundary_detail, "criteria": ["quality"]}],
            "advisory": [],
            "coverage": {},
        },
    )

    out = adapter.code_review_decision("diff", str(tmp_path), "ref")

    bullet = next(line for line in out["message"].splitlines() if line.startswith("- "))
    rendered_detail = bullet.split(") ", 1)[1]
    assert rendered_detail == boundary_detail
    assert "…" not in rendered_detail
    assert "omitted" not in out["message"]


def test_adapter_indeterminate_is_coverage_gap_block(monkeypatch, tmp_path):
    _patch_verdict(
        monkeypatch,
        {"verdict": "INDETERMINATE", "coverage": {"llm_unavailable": True, "llm_error": "outage"}},
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is True
    assert "coverage-gap (llm-unavailable)" in out["message"]


def test_adapter_indeterminate_no_gap_no_findings_is_coverage_gap_not_finding(
    monkeypatch, tmp_path
):
    # Bug spy-luge-wool (expanded scope): a non-PASS (INDETERMINATE) verdict with ZERO blocking
    # findings and NO detected coverage gap was mapped to _block("finding"), rendering the
    # misleading "[LLM-Review: BLOCK — finding] rebar code review found 0 blocking issue(s):"
    # (the false -1 observed on change 223). It must be a coverage-gap/INDETERMINATE BLOCK —
    # never a "finding" BLOCK with no findings.
    _patch_verdict(
        monkeypatch,
        {"verdict": "INDETERMINATE", "blocking": [], "advisory": [], "coverage": {"llm_ran": True}},
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is True
    assert "BLOCK — finding" not in out["message"]
    assert "coverage-gap (indeterminate)" in out["message"]
    assert "0 blocking issue(s)" not in out["message"]


def test_adapter_inert_disabled_verdict_never_passes(monkeypatch, tmp_path):
    # a PASS-but-disabled (inert) verdict must NEVER become a submittable PASS (defense-in-depth).
    _patch_verdict(
        monkeypatch, {"verdict": "PASS", "coverage": {"enabled": False, "llm_ran": False}}
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and "coverage-gap (gate-disabled)" in out["message"]


def test_adapter_scanner_abstain_is_coverage_gap(monkeypatch, tmp_path):
    _patch_verdict(
        monkeypatch,
        {
            "verdict": "BLOCK",
            "coverage": {
                "security_detectors": [
                    {
                        "criterion": "secret-detection",
                        "reason": "fail-closed-abstain",
                        "abstain_reasons": ["no_tool"],
                    }
                ]
            },
        },
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is True
    assert "coverage-gap (scanner)" in out["message"]


def test_adapter_scanner_MATCH_is_a_real_finding(monkeypatch, tmp_path):
    # a detector-finding (a real secret) is a finding BLOCK, NOT a coverage gap.
    _patch_verdict(
        monkeypatch,
        {
            "verdict": "BLOCK",
            "blocking": [{"finding": "secret", "criteria": ["secret-detection"]}],
            "coverage": {"security_detectors": [{"reason": "detector-finding"}]},
        },
    )
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is False
    assert "BLOCK — finding" in out["message"]


def test_adapter_renders_named_finding_for_detector_match_block(monkeypatch, tmp_path):
    # Regression (bug f367): a fail-closed DET detector MATCH forces verdict=BLOCK via
    # `apply_failclosed`; the adapter must render "found N blocking issue(s)" and NAME the match —
    # never "found 0 blocking issue(s)" with no finding (which hid a real secret from the author).
    # Drive the REAL apply_failclosed output (not a hand-built verdict) through the REAL adapter so
    # the seam is exercised end-to-end: on the pre-fix code apply_failclosed leaves blocking=[]
    # and this fails at the `verdict["blocking"]` assertion.
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {
            "high-critical-security": {
                "abstained": [],
                "matches": [
                    {
                        "detector_id": "rebar.builtin.security.python-eval-exec-injection",
                        "location": {"file": "app.py"},
                    }
                ],
            }
        },
    )
    verdict = detectors.apply_failclosed(
        {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": [], "coverage": {}},
        changed_files=["app.py"],
        repo_root=None,
    )
    assert verdict["verdict"] == "BLOCK" and verdict["blocking"]  # the fix populated `blocking`
    _patch_verdict(monkeypatch, verdict)

    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and out["coverage_gap"] is False
    assert out["message"].startswith("[LLM-Review: BLOCK — finding]")
    assert "found 1 blocking issue(s):" in out["message"]
    assert "found 0 blocking issue(s)" not in out["message"]
    # the criterion + matched file are named (an actionable, not empty, block)
    assert "high-critical-security" in out["message"] and "app.py" in out["message"]
    assert any(f["dimension"] == "high-critical-security" for f in out["findings"])


def test_adapter_forces_gate_enabled(monkeypatch, tmp_path):
    calls = {}
    import rebar.llm.workflow.gate_dispatch as gd

    def fake(request):
        calls["enabled"] = request.enabled
        return {"verdict": "PASS", "coverage": {"llm_ran": True}}

    monkeypatch.setattr(gd, "produce_code_review_verdict", fake, raising=True)
    adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert calls.get("enabled") is True  # voter activation is the authoritative gate (ADR 0013)


def test_adapter_error_is_block_fail_closed(monkeypatch, tmp_path):
    import rebar.llm.workflow.gate_dispatch as gd

    def boom(request):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(gd, "produce_code_review_verdict", boom, raising=True)
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK" and "coverage-gap (review-error)" in out["message"]


def test_adapter_unparseable_result_is_block(monkeypatch, tmp_path):
    _patch_verdict(monkeypatch, "not a dict")
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["decision"] == "BLOCK"


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


def test_clear_voted_deletes_only_the_exact_change_revision(tmp_path):
    store = DedupStore(str(tmp_path / "voted.db"))
    store.record_vote("c1", "r1", "patchset-created", -1)
    store.record_vote("c1", "r2", "patchset-created", 1)
    store.record_vote("c2", "r1", "patchset-created", 1)

    store.clear_voted("c1", "r1")

    assert store.already_voted("c1", "r1") is False
    assert store.already_voted("c1", "r2") is True
    assert store.already_voted("c2", "r1") is True


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


def test_artifact_store_failure_after_vote_keeps_vote_and_returns_normally(monkeypatch, tmp_path):
    """A post-vote store outage is observable but cannot undo or fail the Gerrit vote."""
    import rebar
    from rebar.review_bot import artifact_emit

    _patch_review(monkeypatch, [])
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    order: list[str] = []

    original_post_vote = g.post_vote

    def _record_vote(*args, **kwargs):
        result = original_post_vote(*args, **kwargs)
        order.append("vote")
        return result

    def _fail_store_write(*args, **kwargs):
        assert g.votes
        order.append("artifact_failure")
        raise RuntimeError("injected tickets-store write failure")

    monkeypatch.setattr(g, "post_vote", _record_vote)
    monkeypatch.setattr(rebar, "list_tickets", lambda *args, **kwargs: [])
    monkeypatch.setattr(rebar, "create_ticket", _fail_store_write)
    monkeypatch.setattr(artifact_emit, "_publish_artifact_emit_error_metric", lambda: None)

    result = asyncio.run(
        voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )

    assert order == ["vote", "artifact_failure"]
    assert result["status"] == "voted"
    assert len(g.votes) == 1
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

        def post_vote(
            self, change_id, revision, value, message, robot_comments=None, comments=None
        ):
            order.append("post")
            return super().post_vote(change_id, revision, value, message, robot_comments, comments)

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
    assert cfg.gerrit_base_url == "http://gerrit:8080"
    assert cfg.reconcile_interval_seconds == 300
    # WEBHOOK_TOKEN defaults to the bot token (ADR-0014)
    assert cfg.webhook_token == "secret-tok"


# ── reconcile (backfill) ──────────────────────────────────────────────────────
def _events_log_event(change_id, revision, number=1, project="rebar", created_on=1_700_000_000):
    """A Gerrit events-log ``patchset-created`` event (epoch ``eventCreatedOn``)."""
    return {
        "type": "patchset-created",
        "eventCreatedOn": created_on,
        "change": {"id": change_id, "number": number, "project": project},
        "patchSet": {
            "number": 1,
            "revision": revision,
            "ref": f"refs/changes/{number}/{number}/1",
        },
    }


class ReconcileGerrit(FakeGerrit):
    """FakeGerrit that also serves events-log events + per-revision vote state, recording
    every ``list_events`` ``since`` arg so the cursor windowing can be asserted."""

    def __init__(self, *, events=None, voted_revisions=(), list_raises=False, **kw):
        super().__init__(**kw)
        self._events = list(events or [])
        self._voted = set(voted_revisions)
        self._list_raises = list_raises
        self.list_since_calls: list = []

    def list_events(self, since=None):
        self.list_since_calls.append(since)
        if self._list_raises:
            raise GerritError("events-log unreachable", status=503)
        # HONOUR ``since`` the way the live events-log ``?t1=`` does — an INCLUSIVE,
        # SERVER-SIDE lower bound (bug 9f63; verified against the live plugin:
        # ``t1=13:00:00`` returned ``oldest=13:00:18``, i.e. earlier events are simply
        # not in the response). The double previously recorded ``since`` and then
        # returned the full list regardless, so the production cursor filter was never
        # exercised and the cursor-skip defect shipped behind a green suite.
        if not since:
            return list(self._events)
        cut = (
            datetime.datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
        return [e for e in self._events if int(e.get("eventCreatedOn") or 0) >= cut]

    def has_llm_review_vote(self, change_id, revision="current"):
        self.has_vote_calls += 1
        return revision in self._voted

    def post_vote(self, change_id, revision, value, message, robot_comments=None, comments=None):
        status = super().post_vote(change_id, revision, value, message, robot_comments, comments)
        self._voted.add(revision)
        return status


def test_reconcile_once_reviews_only_the_gap_change_and_persists_cursor(monkeypatch, tmp_path):
    """One change already voted, one vote-less (the gap): only the gap is reviewed, and
    the cursor is persisted + advanced to the newest event time."""
    _patch_review(monkeypatch, [])  # clean → PASS for the gap change
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    g = ReconcileGerrit(
        events=[
            _events_log_event("rebar~main~Ialready", "rev-voted", number=10, created_on=1000),
            _events_log_event("rebar~main~Igap", "rev-gap", number=11, created_on=2000),
        ],
        voted_revisions={"rev-voted"},  # the already-voted change's current revision
    )

    res = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    # Only the gap change was reviewed + voted.
    assert res == {"scanned": 2, "reviewed": 1}
    assert [v[0] for v in g.votes] == ["rebar~main~Igap"]
    # First pass had no cursor (None), and the cursor file is now persisted + advanced.
    assert g.list_since_calls == [None]
    from pathlib import Path

    cursor_file = Path(cfg.cursor_path)
    assert cursor_file.exists()
    persisted = cursor_file.read_text(encoding="utf-8").strip()
    assert persisted  # a yyyy-MM-dd HH:mm:ss t1 string (newest event = created_on 2000)


def test_reconcile_once_second_pass_is_idempotent_via_dedup_and_cursor(monkeypatch, tmp_path):
    """A second pass over the same events does nothing new: the gap change is now in the
    dedup ledger (idempotent) and the cursor is carried into the next ``since`` window."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    g = ReconcileGerrit(
        events=[_events_log_event("rebar~main~Igap", "rev-gap", number=11, created_on=2000)],
    )

    first = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    assert first == {"scanned": 1, "reviewed": 1}
    assert len(g.votes) == 1

    second = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    # Idempotent: scanned again but reviewed nothing new (dedup row present).
    assert second["reviewed"] == 0
    assert len(g.votes) == 1  # no second vote
    # The 2nd pass passed the persisted cursor (not None) as the since window.
    assert g.list_since_calls[0] is None
    assert g.list_since_calls[1] is not None


def test_reconcile_clears_stale_local_vote_after_definitive_gerrit_no_vote(monkeypatch, tmp_path):
    """A reset-emitted event recovers after restart even when SQLite retained the old vote."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    store.record_vote("rebar~main~Istale", "rev-stale", "patchset-created", -1)
    store.record_vote("rebar~main~Iother", "rev-other", "patchset-created", 1)
    g = ReconcileGerrit(
        events=[_events_log_event("rebar~main~Istale", "rev-stale", number=12, created_on=2_000)]
    )

    result = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert result == {"scanned": 1, "reviewed": 1}
    assert [vote[:3] for vote in g.votes] == [("rebar~main~Istale", "rev-stale", 1)]
    assert store.already_voted("rebar~main~Istale", "rev-stale") is True
    assert store.already_voted("rebar~main~Iother", "rev-other") is True
    # Reconciler checks once, then the ordinary voter re-checks under its per-revision lock.
    assert g.has_vote_calls == 2


def test_reconcile_preserves_stale_local_vote_when_gerrit_still_has_nonzero_vote(
    monkeypatch, tmp_path
):
    """Contrast: Gerrit's nonzero vote remains authoritative and local dedup is retained."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    store.record_vote("rebar~main~Ivoted", "rev-voted", "patchset-created", -1)
    g = ReconcileGerrit(
        events=[_events_log_event("rebar~main~Ivoted", "rev-voted", created_on=2_000)],
        voted_revisions={"rev-voted"},
    )

    result = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert result == {"scanned": 1, "reviewed": 0}
    assert store.already_voted("rebar~main~Ivoted", "rev-voted") is True
    assert g.has_vote_calls == 1
    assert g.votes == []


def test_reconcile_vote_read_error_preserves_dedup_and_holds_cursor(monkeypatch, tmp_path, caplog):
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    store.record_vote("rebar~main~Ierror", "rev-error", "patchset-created", -1)

    class VoteReadErrorGerrit(ReconcileGerrit):
        def has_llm_review_vote(self, change_id, revision="current"):
            raise GerritError("vote read unavailable", status=503)

    g = VoteReadErrorGerrit(
        events=[_events_log_event("rebar~main~Ierror", "rev-error", created_on=2_000)]
    )

    with caplog.at_level(logging.INFO, logger="rebar.review_bot.reconcile"):
        result = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert result == {"scanned": 1, "reviewed": 0}
    assert store.already_voted("rebar~main~Ierror", "rev-error") is True
    assert g.votes == []
    assert "reconcile_check_error" in caplog.text
    assert "rebar~main~Ierror" in caplog.text and "rev-error" in caplog.text


def test_accepted_rerun_survives_restart_via_gerrit_reconcile(monkeypatch, tmp_path):
    """Production-shaped restart path: Gerrit, not the discarded queue, preserves work."""
    pytest.importorskip("fastapi")
    from pathlib import Path

    from starlette.testclient import TestClient

    from rebar.review_bot import app as appmod
    from rebar.review_bot import gerrit_client as gcmod

    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    change_id = "rebar~main~Irestart"
    revision = "rev-restart"
    store.record_vote(change_id, revision, "patchset-created", -1)
    store.record_vote("rebar~main~Iunrelated", "rev-other", "patchset-created", 1)
    for _ in range(cfg.retryable_gap_max_attempts):
        store.record_attempt(change_id, revision)
    assert store.attempt_count(change_id, revision) == cfg.retryable_gap_max_attempts

    # The original patchset event is already behind the persisted window.
    Path(cfg.cursor_path).write_text(reconcile._to_t1(1_500), encoding="utf-8")

    class DurableRerunGerrit(ReconcileGerrit):
        def get_change_event(self, requested_change):
            return {
                "type": "manual-rerun",
                "change": {"id": change_id, "number": 42, "project": "rebar"},
                "patchSet": {
                    "number": 2,
                    "revision": revision,
                    "ref": "refs/changes/42/42/2",
                },
            }

        def reset_llm_review_vote(self, reset_change, reset_revision):
            assert (reset_change, reset_revision) == (change_id, revision)
            self._voted.discard(reset_revision)
            self._events.append(
                {
                    "type": "comment-added",
                    "eventCreatedOn": 2_000,
                    "change": {"id": change_id, "number": 42, "project": "rebar"},
                    "patchSet": {
                        "number": 2,
                        "revision": revision,
                        "ref": "refs/changes/42/42/2",
                    },
                    "comment": "Patch Set 2: LLM-Review0",
                }
            )
            return 200

    g = DurableRerunGerrit(
        events=[_events_log_event(change_id, revision, number=42, created_on=1_000)],
        voted_revisions={revision},
    )

    async def _idle_worker(queue, cfg):
        await asyncio.Event().wait()

    async def _idle_loop(*, config):
        await asyncio.Event().wait()

    monkeypatch.setattr(appmod, "_worker", _idle_worker, raising=True)
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _idle_loop, raising=True)
    monkeypatch.setattr(appmod.app.state, "config", cfg, raising=False)
    monkeypatch.setattr(gcmod, "GerritClient", lambda _cfg: g)
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")

    with TestClient(appmod.app) as client:
        response = client.post("/rerun?token=tok&change=42")
        assert response.status_code == 202
        assert appmod.app.state.queue.qsize() == 1
        assert store.attempt_count(change_id, revision) == 0

    # Simulate replacement-process state: the accepted live queue is gone.
    replacement_queue: asyncio.Queue = asyncio.Queue()
    assert replacement_queue.empty()
    reset_event = g._events[-1]
    assert reset_event["type"] == "comment-added"
    assert reset_event["eventCreatedOn"] > 1_500
    assert reset_event["patchSet"]["revision"] == revision

    result = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert result == {"scanned": 1, "reviewed": 1}
    assert [vote[:3] for vote in g.votes] == [(change_id, revision, 1)]
    assert len(g.votes) == 1
    assert store.already_voted(change_id, revision) is True
    assert store.already_voted("rebar~main~Iunrelated", "rev-other") is True


def test_reconcile_once_events_log_error_does_not_crash_or_vote(monkeypatch, tmp_path):
    """events-log error → degraded fallback: no crash, no vote, no cursor advance."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    g = ReconcileGerrit(list_raises=True)

    res = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert res == {"scanned": 0, "reviewed": 0}
    assert g.votes == []  # NEVER casts a vote on a degraded pass (fail-closed)
    from pathlib import Path

    assert not Path(cfg.cursor_path).exists()  # cursor NOT advanced on error


def test_reconcile_once_malformed_events_body_does_not_vote(monkeypatch, tmp_path):
    """events-log returns a non-list (malformed) body → degraded, no crash, no vote."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)

    class MalformedGerrit(ReconcileGerrit):
        def list_events(self, since=None):
            self.list_since_calls.append(since)
            return {"not": "a list"}  # malformed

    g = MalformedGerrit()
    res = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    assert res == {"scanned": 0, "reviewed": 0}
    assert g.votes == []


# ── 9ec0: the reconciler's cooperative shutdown (runs in the DEFAULT tier) ────
#
# These sit alongside the lifespan tests further down deliberately. Those exercise the whole
# shutdown end to end but need the ``reviewbot`` extra, and CI's pytest lane installs only
# ``[dev]`` — so every fastapi test SKIPS there. ``reconcile`` imports without fastapi, so
# pinning the mechanism here is what gives this fix real coverage in CI rather than a skip.
@pytest.fixture(autouse=True)
def _reset_reconciler_stop_flag():
    """``reconcile``'s stop flag is module state; leaking it would make later reconcile passes
    decline all work (and under xdist that lands in a different test)."""
    reconcile.clear_stop()
    yield
    reconcile.clear_stop()


async def _await_probe(probe, *, cap=3.0, what="the probe"):
    """Wait until ``probe`` (a list the code under test appends to) is non-empty.

    Polls OFF-LOOP in a single ``to_thread`` hop rather than in a
    ``while ...: await asyncio.sleep(...)`` loop, which is what ASYNC110 forbids. Its
    suggested ``asyncio.Event`` is not usable here: the two probes this serves are appended
    from WORKER THREADS — the review gate and the events-log fetch both run off-loop via
    ``to_thread`` — and an ``asyncio.Event`` may not be set from another thread. Polling in
    the thread keeps the event loop free, which is the property the rule protects, without
    threading a cross-thread signalling contract through every fake.
    """

    def _poll():
        deadline = time.monotonic() + cap
        while not probe and time.monotonic() < deadline:
            time.sleep(0.01)

    await asyncio.to_thread(_poll)
    assert probe, f"{what} never fired within {cap}s — the probe is not wired"


def _slow_review_probe(monkeypatch, seconds=0.3):
    """Patch ``review_and_vote`` with a slow stub; returns (started, completed) transcripts."""
    started: list[str] = []
    completed: list[str] = []

    async def _slow(event, *, config=None, gerrit=None, dedup=None, force=False):
        change_id = event["change"]["id"]
        started.append(change_id)
        await asyncio.sleep(seconds)
        completed.append(change_id)
        return {"status": "voted", "change_id": change_id}

    monkeypatch.setattr(voter, "review_and_vote", _slow, raising=True)
    return started, completed


def test_reconcile_once_takes_no_new_candidate_once_a_stop_is_requested(monkeypatch, tmp_path):
    """AC2 at the reconcile level: a stop requested mid-review lets THAT review finish but
    stops the pass taking the next candidate — otherwise the drain re-extends per candidate."""
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    started, completed = _slow_review_probe(monkeypatch)
    g = ReconcileGerrit(
        events=[
            _events_log_event("rebar~main~Ione", "rev-one", number=1),
            _events_log_event("rebar~main~Itwo", "rev-two", number=2),
        ]
    )

    async def _run():
        task = asyncio.create_task(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
        # wait until the FIRST review is genuinely in flight
        await _await_probe(started, what="the first review")
        reconcile.request_stop()
        return await asyncio.wait_for(task, timeout=10)

    res = asyncio.run(_run())

    assert len(started) == 1, f"a new candidate was started after the stop request: {started}"
    assert len(completed) == 1, f"the in-flight review must still finish: {completed}"
    assert res["reviewed"] == 1
    # The skipped candidate stays vote-less (fail-closed) — never marked as handled.
    assert not store.already_voted("rebar~main~Itwo", "rev-two")


def test_a_candidate_skipped_by_the_stop_is_held_back_in_the_cursor(monkeypatch, tmp_path):
    """The stop path must obey the low-water-mark contract (bug 9f63).

    Declining a candidate at shutdown is only fail-closed if that candidate is still inside
    the NEXT pass's window. The cursor advances to ``newest`` over the whole fetched window,
    so a candidate merely ``break``-ed past — without being recorded in ``held_back`` — falls
    outside every subsequent inclusive ``?t1=`` window and is unreachable forever. That would
    make shutdown the one path that silently drops a gap patchset, which is the precise defect
    9f63 closed; this pins that the 9ec0 stop cannot reintroduce it.

    Shaped like the 9f63 oracle: the skipped candidate is NOT the newest event, so unrelated
    chatter drags ``newest`` past it — the only shape in which the defect can be expressed.
    """
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    started, _completed = _slow_review_probe(monkeypatch)
    # Timestamps sit close together so the hold-back stays well inside
    # ``reconcile_max_holdback_seconds`` and the clamp ceiling is not what is under test.
    first = _events_log_event("rebar~main~Ione", "rev-one", number=1, created_on=2800)
    skipped = _events_log_event("rebar~main~Itwo", "rev-two", number=2, created_on=2900)
    chatter = {
        "type": "comment-added",
        "eventCreatedOn": 3000,
        "change": {"id": "rebar~main~Ichat", "number": 3, "project": "rebar"},
        "patchSet": {},  # not a candidate, but it DOES drag ``newest`` past the skipped one
    }
    g = ReconcileGerrit(events=[first, skipped, chatter])

    async def _run():
        task = asyncio.create_task(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
        # the FIRST review is genuinely in flight
        await _await_probe(started, what="the first review")
        reconcile.request_stop()
        return await asyncio.wait_for(task, timeout=10)

    asyncio.run(_run())
    assert started == ["rebar~main~Ione"], f"the stop did not skip the second candidate: {started}"

    # THE CONTRACT: the next pass can still see the candidate the shutdown declined.
    reconcile.clear_stop()
    second = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))

    assert "rebar~main~Itwo" in started, (
        "the candidate declined at shutdown fell outside the next window — the cursor "
        "advanced past an event no pass ever voted, so backfill can never recover it (9f63)"
    )
    assert second["scanned"] >= 1


@pytest.mark.real_reconcile_loop
def test_reconcile_loop_returns_after_finishing_the_review_in_flight(monkeypatch, tmp_path):
    """AC1's mechanism: on a stop request the loop RETURNS once the in-flight review lands.

    That return is what makes the loop's task awaitable as a drain by the app lifespan — the
    alternative is cancelling it, which is precisely what abandoned the backfill review."""
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    started, completed = _slow_review_probe(monkeypatch)
    g = ReconcileGerrit(events=[_events_log_event("rebar~main~Igap", "rev-gap", number=7)])

    async def _run():
        task = asyncio.create_task(
            reconcile.reconcile_loop(interval=3600, config=cfg, gerrit=g, dedup=store)
        )
        await _await_probe(started, what="the first review")
        reconcile.request_stop()
        # No cancel: the loop must come back on its own, well inside a real drain budget.
        await asyncio.wait_for(task, timeout=10)
        return task

    task = asyncio.run(_run())

    assert completed == ["rebar~main~Igap"], (
        "the loop must let the review in flight complete before returning; got "
        f"{completed} (this is the review a shutdown cancels today)"
    )
    assert task.done() and not task.cancelled()


@pytest.mark.real_reconcile_loop
def test_reconcile_loop_stop_is_prompt_while_idle_between_passes(monkeypatch, tmp_path):
    """An IDLE reconciler parked between passes must notice the stop promptly rather than
    holding shutdown open for a whole reconcile interval (default 300s)."""
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    _patch_review(monkeypatch, [])
    g = ReconcileGerrit(events=[])  # nothing to do → the loop parks in its inter-pass wait

    async def _run():
        task = asyncio.create_task(
            reconcile.reconcile_loop(interval=3600, config=cfg, gerrit=g, dedup=store)
        )
        # first pass done → now parked
        await _await_probe(g.list_since_calls, what="the first reconcile pass")
        await asyncio.sleep(0.05)
        start = time.monotonic()
        reconcile.request_stop()
        await asyncio.wait_for(task, timeout=10)
        return time.monotonic() - start

    elapsed = asyncio.run(_run())

    # The failure mode is the loop waiting out its whole 3600s inter-pass interval, and the
    # enclosing wait_for(10) already caps the run, so the ceiling only has to sit between them.
    # timing: hang-guard — 5s dwarfs the ~0.25s poll granularity this actually needs
    assert elapsed < 5, (
        f"an idle reconciler took {elapsed:.2f}s to honour the stop against a 3600s interval; "
        "the inter-pass wait must be interruptible or shutdown waits out the whole interval"
    )


# ── force / rerun recovery ──────────────────────────────────────────────────────
def test_voter_force_re_reviews_despite_existing_vote_and_dedup(monkeypatch, tmp_path):
    """force=True (a manual /rerun) re-reviews + re-casts even when the change ALREADY
    carries a Gerrit vote AND has a dedup row — proving /rerun recovers a stuck vote."""
    _patch_review(monkeypatch, [])  # clean → PASS
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    store.record_vote("rebar~main~Iabc", "rev1", "patchset-created", -1)  # stuck -1 row
    g = FakeGerrit(has_vote=True)  # Gerrit reports an existing vote too

    res = asyncio.run(
        voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store, force=True)
    )

    assert res["status"] == "voted"  # did NOT skip
    assert g.votes and g.votes[0][2] == 1  # re-cast a fresh verdict
    assert g.has_vote_calls == 0  # force skips the Gerrit existing-vote check entirely


def test_voter_force_false_still_skips_when_already_voted(monkeypatch, tmp_path):
    """Contrast: force=False skips when a dedup row is already present (no re-review)."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    store.record_vote("rebar~main~Iabc", "rev1", "patchset-created", -1)
    g = FakeGerrit(has_vote=True)

    res = asyncio.run(
        voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store, force=False)
    )

    assert res["status"] == "skipped"
    assert res["reason"] == "dedup"
    assert g.votes == []


# ── get_patch decode paths (offline, captured payloads) ──────────────────────
def _client(tmp_path):
    from rebar.review_bot.gerrit_client import GerritClient

    return GerritClient(_cfg(tmp_path))


def test_get_change_event_prefers_canonical_change_id(tmp_path, monkeypatch):
    gc = _client(tmp_path)
    revision = "d51c056d0b4859d1a5fcb311b311c4e531919078"
    monkeypatch.setattr(
        gc,
        "_get_json",
        lambda _path: {
            "id": "rebar~986",
            "change_id": "I969fce55bf212e539f67009ff42447fb234068df",
            "_number": 986,
            "project": "rebar",
            "current_revision": revision,
            "revisions": {
                revision: {
                    "_number": 1,
                    "ref": "refs/changes/86/986/1",
                }
            },
        },
    )

    event = gc.get_change_event("986")

    assert event is not None
    assert event["change"]["id"] == "I969fce55bf212e539f67009ff42447fb234068df"
    assert event["patchSet"]["revision"] == revision


_SAMPLE_DIFF = (
    "From 0123456789abcdef Mon Sep 17 00:00:00 2001\n"
    "From: Dev <dev@example.com>\n"
    "Subject: [PATCH] add a line\n\n"
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n pass\n+# new\n"
)


def test_get_patch_decodes_captured_base64(tmp_path, monkeypatch):
    """The /patch text/plain form is base64 (a captured payload). get_patch must
    decode it to the unified diff text passed to the reviewer."""
    import base64

    captured = base64.b64encode(_SAMPLE_DIFF.encode()).decode("ascii")
    gc = _client(tmp_path)
    monkeypatch.setattr(gc, "_request", lambda *a, **k: (200, captured))
    out = gc.get_patch("rebar~main~Iabc", "rev1")
    assert out == _SAMPLE_DIFF
    assert out.startswith("From ") and "diff --git" in out


def test_get_patch_decodes_xssi_json_string(tmp_path, monkeypatch):
    """The /patch Accept: application/json form is an XSSI-guarded JSON string of the
    raw patch (the live shape). get_patch must strip XSSI + JSON-decode to the diff."""
    body = ")]}'\n" + _json.dumps(_SAMPLE_DIFF)
    gc = _client(tmp_path)
    monkeypatch.setattr(gc, "_request", lambda *a, **k: (200, body))
    assert gc.get_patch("rebar~main~Iabc", "rev1") == _SAMPLE_DIFF


def test_get_patch_rejects_non_decodable_body(tmp_path, monkeypatch):
    """A body that is neither JSON nor base64 fails closed with GerritError."""
    gc = _client(tmp_path)
    monkeypatch.setattr(gc, "_request", lambda *a, **k: (200, "!!! not base64 !!!"))
    with pytest.raises(GerritError):
        gc.get_patch("rebar~main~Iabc", "rev1")


# ── merge-change review path (epic 88ab / S2) ────────────────────────────────
import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from rebar.llm.code_review.assemble import (  # noqa: E402
    MERGELIST_MAX_COMMITS,
    assemble_merge_change_context,
)

_FIXTURES = _Path(__file__).resolve().parents[1] / "fixtures" / "review_bot_merge"


def _diff_info(added, removed=()):
    """Build a Gerrit DiffInfo with one changed segment (the shape get_file_diff returns)."""
    seg = {}
    if removed:
        seg["a"] = list(removed)
    if added:
        seg["b"] = list(added)
    return {"content": [{"ab": ["ctx1", "ctx2"]}, seg]}


def _merge_event(change_id="rebar~main~Imerge", revision="mrev", project="rebar"):
    return {
        "type": "patchset-created",
        "change": {"id": change_id, "number": 77, "project": project},
        "patchSet": {"number": 1, "revision": revision, "ref": "refs/changes/77/77/1"},
    }


def test_merge_files_fixture_proves_auto_merge_default(tmp_path, monkeypatch):
    """AC#1 (riskiest assumption): the LIVE-captured Gerrit 3.14.1 fixture proves that
    GET .../revisions/{rev}/files with NO base/parent param returns the AUTO-MERGE-BASE file
    map for a merge commit (it does NOT 409 like /patch). Per Gerrit REST
    rest-api-changes.html#list-files: for a merge with neither base nor parent set, the file
    list is computed against the auto-merge. A clean merge yields only the magic pseudo-paths.
    The client parses the fixture body identically to a live response."""
    body = (_FIXTURES / "merge_files_clean.json").read_text(encoding="utf-8")
    gc = _client(tmp_path)
    monkeypatch.setattr(gc, "_request", lambda *a, **k: (200, body))
    files = gc.get_merge_files("rebar~main~Imerge", "mrev")
    # magic pseudo-paths present; a clean merge has NO real conflict file
    assert set(files) == {"/COMMIT_MSG", "/MERGE_LIST"}
    assert all(p in gc.MAGIC_PATHS for p in files)
    # commit fixture has 2 parents => merge detection
    commit = _json.loads((_FIXTURES / "merge_commit_clean.json").read_text())
    assert len(commit["parents"]) >= 2


def test_assemble_merge_context_format_and_real_files():
    """assemble_merge_change_context: ## Merge context (integrated subjects) + ## Auto-merge
    diff (real files only, magic paths excluded)."""
    merge_files = {"/COMMIT_MSG": {}, "/MERGE_LIST": {}, "src/x.py": {"status": "M"}}
    file_diffs = {"src/x.py": "-old\n+new"}
    mergelist = [{"commit": "a1b2c3d4e5f6", "subject": "feat: story one"}]
    out = assemble_merge_change_context(merge_files, file_diffs, mergelist)
    assert "## Merge context (1 integrated commit(s))" in out
    assert "a1b2c3d4e5 feat: story one" in out
    assert "## Auto-merge diff" in out
    assert "### src/x.py" in out and "+new" in out
    # magic pseudo-paths never appear as reviewed files
    assert "/COMMIT_MSG" not in out and "/MERGE_LIST" not in out


def test_assemble_merge_context_empty_diff_clean_merge():
    """A clean merge (only magic paths, no real file diffs) → explicit empty-delta notice;
    review proceeds on the mergelist context alone."""
    merge_files = {"/COMMIT_MSG": {}, "/MERGE_LIST": {}}
    out = assemble_merge_change_context(merge_files, {}, [{"commit": "deadbeef00", "subject": "s"}])
    assert "## Merge context (1 integrated commit(s))" in out
    assert "empty" in out.lower() and "clean merge" in out.lower()


def test_assemble_merge_context_mergelist_count_cap():
    """MERGELIST_MAX_COMMITS bounds the integrated-commit list with a truncation notice."""
    big = [
        {"commit": f"{i:040x}", "subject": f"commit {i}"} for i in range(MERGELIST_MAX_COMMITS + 5)
    ]
    out = assemble_merge_change_context({}, {}, big)
    assert f"## Merge context ({MERGELIST_MAX_COMMITS + 5} integrated commit(s))" in out
    assert "5 more integrated commit(s) omitted" in out
    # only MERGELIST_MAX_COMMITS subject lines rendered
    assert out.count("- ") <= MERGELIST_MAX_COMMITS + 1  # +1 tolerance for notice bullet shapes


def test_assemble_merge_context_diff_truncated_last_under_combined_cap():
    """The combined string is bounded by diff_char_cap: the merge context is laid down first,
    the auto-merge diff is truncated last."""
    merge_files = {"big.py": {"status": "M"}}
    file_diffs = {"big.py": "+x" * 10000}
    out = assemble_merge_change_context(
        merge_files, file_diffs, [{"commit": "c0ffee", "subject": "s"}], diff_char_cap=500
    )
    assert len(out) <= 700  # cap + notice slack
    assert "truncated" in out
    assert "## Merge context" in out  # context survives (laid down first)


def test_voter_merge_change_casts_vote_with_merge_tag(monkeypatch, tmp_path):
    """A merge revision (parents>=2) is reviewed on its auto-merge delta and the robot
    comment carries the merge-change tag variant with the integrated-commit count."""
    _patch_review(monkeypatch, [])  # PASS
    g = FakeGerrit(
        parents=2,
        merge_files={"/COMMIT_MSG": {}, "/MERGE_LIST": {}, "src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["new line"])},
        mergelist=[
            {"commit": "aaa111bbb222", "subject": "s1"},
            {"commit": "ccc333", "subject": "s2"},
        ],
    )
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_merge_event(), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert res["status"] == "voted" and res["vote_value"] == 1
    msg = g.votes[0][3]
    assert "(merge-change, 2 integrated commit(s))" in msg
    # NEVER used the bare /patch on a merge (would 409)
    assert g.get_patch_calls == 0


def test_voter_non_merge_uses_get_patch_no_merge_tag(monkeypatch, tmp_path):
    """A normal (1-parent) change still uses get_patch and carries NO merge-change tag."""
    _patch_review(monkeypatch, [])
    g = FakeGerrit(parents=1)
    res = asyncio.run(
        voter.review_and_vote(
            _event(), config=_cfg(tmp_path), gerrit=g, dedup=DedupStore(str(tmp_path / "v.db"))
        )
    )
    assert res["status"] == "voted"
    assert g.get_patch_calls == 1
    assert "merge-change" not in g.votes[0][3]


def test_voter_merge_empty_auto_diff_still_reviews(monkeypatch, tmp_path):
    """A CLEAN merge (only magic paths, empty auto-merge delta) is still reviewed (on the
    mergelist context) and votes — it does not error or skip."""
    _patch_review(monkeypatch, [])
    g = FakeGerrit(
        parents=2,
        merge_files={"/COMMIT_MSG": {}, "/MERGE_LIST": {}},
        mergelist=[{"commit": "d00d", "subject": "s"}],
    )
    res = asyncio.run(
        voter.review_and_vote(
            _merge_event(),
            config=_cfg(tmp_path),
            gerrit=g,
            dedup=DedupStore(str(tmp_path / "v.db")),
        )
    )
    assert res["status"] == "voted"
    assert g.get_patch_calls == 0
    assert "(merge-change, 1 integrated commit(s))" in g.votes[0][3]


@pytest.mark.parametrize(
    "raise_on", ["get_commit", "get_merge_files", "get_mergelist", "get_file_diff"]
)
def test_voter_merge_path_rest_failure_votes_block_coverage_gap(monkeypatch, tmp_path, raise_on):
    """EVERY merge-path REST failure (commit/files/mergelist/diff) fails closed as a -1
    COVERAGE-GAP vote (the merge change is BLOCKED and visibly flagged as an infra veto) —
    never a MAX. The bare /patch is NEVER used on the merge (409 guard holds)."""
    _patch_review(monkeypatch, [])
    g = FakeGerrit(
        parents=2,
        merge_files={"src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["x"])},
        mergelist=[{"commit": "abc", "subject": "s"}],
        raise_on=raise_on,
    )
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_merge_event(), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert res["status"] == "voted"
    assert res["vote_value"] == -1  # block value, not MAX
    assert g.votes and g.votes[0][2] == -1
    assert "coverage-gap" in g.votes[0][3]
    assert g.get_patch_calls == 0  # 409 guard: never bare /patch on a merge


def test_voter_merge_detection_via_backfill_path(monkeypatch, tmp_path):
    """Merge detection lives INSIDE review_and_vote, so the reconciler-backfill path routes a
    merge change through the SAME merge review (reconcile.py needs no change)."""
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    g = ReconcileGerrit(
        events=[_events_log_event("rebar~main~Imerge", "mrev", number=77, created_on=3000)],
        parents=2,
        merge_files={"src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["merged"])},
        mergelist=[{"commit": "aaa", "subject": "s1"}],
    )
    res = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    assert res == {"scanned": 1, "reviewed": 1}
    assert g.get_patch_calls == 0  # backfilled merge used the merge path, not /patch
    assert "(merge-change, 1 integrated commit(s))" in g.votes[0][3]


def test_voter_merge_detection_via_rerun_force_path(monkeypatch, tmp_path):
    """The /rerun (force=True) path also routes a merge through the merge review."""
    _patch_review(monkeypatch, [])
    g = FakeGerrit(
        parents=2,
        merge_files={"src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["y"])},
        mergelist=[{"commit": "b", "subject": "s"}],
    )
    res = asyncio.run(
        voter.review_and_vote(
            _merge_event(),
            config=_cfg(tmp_path),
            gerrit=g,
            dedup=DedupStore(str(tmp_path / "v.db")),
            force=True,
        )
    )
    assert res["status"] == "voted"
    assert g.get_patch_calls == 0
    assert "merge-change" in g.votes[0][3]


def test_render_diff_info_flattens_segments():
    """_render_diff_info turns a Gerrit DiffInfo into +/- unified-ish text."""
    from rebar.review_bot.voter import _render_diff_info

    text = _render_diff_info(_diff_info(added=["added"], removed=["gone"]))
    assert "+added" in text and "-gone" in text and "unchanged line(s)" in text


def test_voter_emits_merge_debug_logs(monkeypatch, tmp_path, caplog):
    """The merge path emits debuggable structured logs: merge_detection (parent_count +
    is_merge for EVERY change), merge_change_review (context stats), and voter_voted carries
    merge/parent_count. These are the fields that make a future merge-review issue diagnosable
    from logs alone (the S2 flattening incident had no such signal)."""
    import logging as _logging

    _patch_review(monkeypatch, [])
    g = FakeGerrit(
        parents=2,
        merge_files={"/COMMIT_MSG": {}, "src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["n"])},
        mergelist=[{"commit": "abc123", "subject": "s"}],
    )
    with caplog.at_level(_logging.INFO, logger="rebar.review_bot.voter"):
        asyncio.run(
            voter.review_and_vote(
                _merge_event(),
                config=_cfg(tmp_path),
                gerrit=g,
                dedup=DedupStore(str(tmp_path / "v.db")),
            )
        )
    blob = "\n".join(r.message for r in caplog.records)
    assert "merge_detection" in blob and '"parent_count": 2' in blob and '"is_merge": true' in blob
    assert "merge_change_review" in blob and '"real_files": 1' in blob
    assert '"auto_diff_empty": false' in blob and '"files_fetched": 1' in blob
    assert "voter_voted" in blob and '"merge": true' in blob


def test_voter_emits_merge_change_409_guard(monkeypatch, tmp_path, caplog):
    """The is_merge branch routes a merge through the auto-merge-delta path INSTEAD of the
    bare /patch (which 409s on a >=2-parent commit), and must emit the named
    ``merge_change_409_guard`` signal (S2 follow-up sly-sloth-bay). It fires ONLY on a merge
    — distinct from ``merge_detection`` (logged for every change) — so the otherwise-silent
    guard is visible in the logs and its firing is diagnosable."""
    import logging as _logging

    _patch_review(monkeypatch, [])
    # MERGE: the guard event MUST be present, and the bare /patch MUST NOT be called.
    gm = FakeGerrit(
        parents=2,
        merge_files={"/COMMIT_MSG": {}, "src/x.py": {"status": "M"}},
        file_diffs={"src/x.py": _diff_info(added=["n"])},
        mergelist=[{"commit": "abc123", "subject": "s"}],
    )
    with caplog.at_level(_logging.INFO, logger="rebar.review_bot.voter"):
        asyncio.run(
            voter.review_and_vote(
                _merge_event(),
                config=_cfg(tmp_path),
                gerrit=gm,
                dedup=DedupStore(str(tmp_path / "m.db")),
            )
        )
    merge_blob = "\n".join(r.message for r in caplog.records)
    assert "merge_change_409_guard" in merge_blob and '"parent_count": 2' in merge_blob
    assert gm.get_patch_calls == 0  # the guard: never the bare /patch on a merge

    caplog.clear()
    # NON-MERGE: the guard event MUST be absent (guard is merge-specific), /patch IS used.
    gn = FakeGerrit(parents=1)
    with caplog.at_level(_logging.INFO, logger="rebar.review_bot.voter"):
        asyncio.run(
            voter.review_and_vote(
                _event(),
                config=_cfg(tmp_path),
                gerrit=gn,
                dedup=DedupStore(str(tmp_path / "n.db")),
            )
        )
    nonmerge_blob = "\n".join(r.message for r in caplog.records)
    assert "merge_change_409_guard" not in nonmerge_blob
    assert gn.get_patch_calls == 1


def test_voter_treats_409_change_closed_as_terminal(monkeypatch, tmp_path):
    # Bug c943: a 409 "change is closed" (a change merged/abandoned in the race window past
    # reconcile.py's open-status filter) is TERMINAL, not a retryable failure — record it so
    # it is never retried, and do NOT emit a VOTER_ERROR / increment the voter_errors metric
    # (a closed change needs no vote, so it is not an actionable fault). A real vote failure
    # (5xx) still stays a retryable voter_error with no dedup row (unchanged).
    _patch_review(monkeypatch, [])  # clean diff → PASS verdict
    errors: list = []
    monkeypatch.setattr(voter, "_voter_error", lambda **kw: errors.append(kw))
    g = FakeGerrit(raise_on_post=True, post_status=409)
    store = DedupStore(str(tmp_path / "voted.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "skipped"  # terminal, NOT "error"
    assert errors == []  # no voter_error emitted / no voter_errors increment
    assert store.already_voted("rebar~main~Iabc", "rev1")  # recorded → never retried


# ── app lifespan: snapshot janitor wiring (incident 2731 / bug e7f4) ────────
def test_lifespan_starts_and_stops_snapshot_janitor(monkeypatch):
    """The receiver's lifespan must start the snapshot-cache janitor (the reclamation
    that incident 2731 showed was dead code in production) and signal its stop event
    on shutdown. Requires the ``reviewbot`` extra (fastapi); skipped without it."""
    pytest.importorskip("fastapi")
    import threading

    import rebar._snapshot as snap
    from rebar.review_bot import app as appmod

    stop = threading.Event()
    started: list[bool] = []

    def fake_start(**_kw):
        started.append(True)
        return threading.Thread(target=lambda: None), stop

    monkeypatch.setattr(snap, "start_background_janitor", fake_start)

    async def drive():
        async with appmod.lifespan(appmod.app):
            assert started, "janitor was not started on startup"
            assert not stop.is_set()
        assert stop.is_set(), "janitor stop event not signalled on shutdown"

    asyncio.run(drive())


# ── worker: a hung review must not stall the queue (bug 9d7c / jaguarundi) ──────
def test_worker_abandons_hung_review_and_keeps_draining(monkeypatch, tmp_path):
    """A single review that HANGS forever (clone/subprocess/LLM blocked — as when the
    disk filled mid-clone, incident 2731) must NOT wedge the single background worker.

    The worker wraps each review in a bounded timeout: the hung event is abandoned (a
    countable ``VOTER_ERROR`` timeout marker is emitted) and the worker moves on to the
    NEXT queued event. Without the timeout the worker awaits the hung review forever and
    every subsequent change silently backs up behind it — this test drives the loop under
    an outer wall-clock guard so the pre-fix (no-timeout) code fails RED rather than
    hanging the suite. Requires the ``reviewbot`` extra (fastapi); skipped without it."""
    pytest.importorskip("fastapi")
    import contextlib

    from rebar.review_bot import app as appmod

    # Short per-review timeout, injected via the documented override env var.
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "0.05")

    processed: list[str] = []

    async def fake_review_and_vote(event, *, config, force=False):
        cid = event["change"]["id"]
        processed.append(cid)
        if cid == "HANG":
            await asyncio.Event().wait()  # never returns — simulates the hung clone/LLM
        return {"status": "voted"}

    monkeypatch.setattr(appmod._voter, "review_and_vote", fake_review_and_vote)

    markers: list[dict] = []
    monkeypatch.setattr(appmod._voter, "_voter_error", lambda **f: markers.append(f))

    async def drive():
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"change": {"id": "HANG"}})
        queue.put_nowait({"change": {"id": "OK"}})
        worker = asyncio.create_task(appmod._worker(queue, _cfg(tmp_path)))
        try:
            # queue.join() returns only once BOTH events reached task_done — the hung one
            # only does so if the worker timed out and abandoned it.
            await queue.join()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    # Outer guard: pre-fix code never completes drive() (worker stuck on HANG) → RED here.
    asyncio.run(asyncio.wait_for(drive(), timeout=10))

    # (1) the hung item did not block forever AND (2) the subsequent item was processed.
    assert processed == ["HANG", "OK"]
    # (3) a countable timeout marker was emitted for the abandoned review.
    assert markers, "no VOTER_ERROR timeout marker emitted for the hung review"
    assert "timed out" in str(markers[0].get("error", ""))


# ── logging configuration (ticket c130: structured _emit INFO must reach stdout) ──
def _clear_reviewbot_log_handlers() -> None:
    """Remove any handler this fix installed on the ``rebar`` logger + restore defaults,
    so each logging test starts from a clean, uncontaminated state."""
    lg = logging.getLogger("rebar")
    for h in list(lg.handlers):
        if getattr(h, "_reviewbot_handler", False):
            lg.removeHandler(h)
    lg.propagate = True
    lg.setLevel(logging.NOTSET)


def test_configure_logging_emits_rebar_info_to_stdout(capsys):
    """A ``rebar.review_bot.*`` INFO record reaches stdout after ``configure_logging()``.

    Before the fix, rebar's loggers have no handler, so an INFO record falls through to
    Python's ``lastResort`` (WARNING+ only) and is silently dropped — the production defect.
    Imports from ``config`` (fastapi-free) so this runs in the default CI suite.
    """
    from rebar.review_bot.config import configure_logging

    _clear_reviewbot_log_handlers()
    configure_logging()
    logging.getLogger("rebar.review_bot.voter").info('{"event": "voter_voted", "probe": "c130"}')
    out = capsys.readouterr().out
    assert '"event": "voter_voted"' in out
    assert "c130" in out
    _clear_reviewbot_log_handlers()


def test_configure_logging_is_idempotent(capsys):
    """Configuring twice must not stack duplicate handlers (no double log lines)."""
    from rebar.review_bot.config import configure_logging

    _clear_reviewbot_log_handlers()
    configure_logging()
    configure_logging()
    installed = [
        h for h in logging.getLogger("rebar").handlers if getattr(h, "_reviewbot_handler", False)
    ]
    assert len(installed) == 1
    # The guarantee the removed ``propagate = False`` was claimed to provide (bug b718):
    # a record reaches stdout EXACTLY once, counted on the stream rather than inferred
    # from the handler list.
    logging.getLogger("rebar.review_bot.voter").info('{"event": "voter_voted", "probe": "b718"}')
    assert capsys.readouterr().out.count('"probe": "b718"') == 1
    _clear_reviewbot_log_handlers()


def test_configure_logging_leaves_rebar_log_propagation_intact(caplog):
    """``configure_logging()`` must not disable propagation on the shared ``rebar`` logger.

    Bug b718: ``logging.getLogger("rebar").propagate = False`` is PROCESS-GLOBAL and was never
    restored. pytest's ``caplog`` captures through a handler on the ROOT logger, so after one
    call no ``rebar.*`` record could reach ``caplog`` again for the rest of the process — every
    later log assertion silently saw zero records. Pre-fix this test fails on both assertions.
    """
    from rebar.review_bot.config import configure_logging

    _clear_reviewbot_log_handlers()
    try:
        configure_logging()
        assert logging.getLogger("rebar").propagate is True
        with caplog.at_level(logging.INFO, logger="rebar.review_bot.voter"):
            logging.getLogger("rebar.review_bot.voter").info("b718-caplog-probe")
        assert any("b718-caplog-probe" in r.getMessage() for r in caplog.records)
    finally:
        _clear_reviewbot_log_handlers()


def test_configure_logging_env_level_override(monkeypatch):
    """``REVIEW_BOT_LOG_LEVEL`` sets the level; an invalid value falls back to INFO."""
    from rebar.review_bot.config import configure_logging

    _clear_reviewbot_log_handlers()
    monkeypatch.setenv("REVIEW_BOT_LOG_LEVEL", "DEBUG")
    configure_logging()
    assert logging.getLogger("rebar").level == logging.DEBUG

    _clear_reviewbot_log_handlers()
    monkeypatch.setenv("REVIEW_BOT_LOG_LEVEL", "NOTALEVEL")
    configure_logging()
    assert logging.getLogger("rebar").level == logging.INFO
    _clear_reviewbot_log_handlers()


# ── deploy resilience (ticket 89be: drain on shutdown + reconciler timeout parity) ──
def test_reconcile_once_times_out_a_hung_review_and_continues(monkeypatch, tmp_path):
    """A backfill review that never returns must NOT freeze the reconcile loop. reconcile_once
    bounds each review with review_timeout_seconds() (parity with the live worker); on timeout it
    abandons the candidate (fail-closed) and the pass returns. Pre-fix (no timeout) this hangs."""
    import rebar.review_bot.reconcile as rec

    cfg = _cfg(tmp_path)
    ev = _event(change_id="rebar~main~Ihang", revision="rhang")

    class _GC:
        def list_events(self, cursor):
            return [ev]

        def has_llm_review_vote(self, change_id, revision="current"):
            return False

    async def _hang(*a, **k):
        await asyncio.sleep(3600)  # never returns within the test

    monkeypatch.setattr(rec._voter, "review_and_vote", _hang, raising=True)
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "0.05")
    store = DedupStore(cfg.dedup_db_path)

    # The outer wait_for is the RED guard: pre-fix reconcile_once awaits the hung review forever
    # and this raises TimeoutError; post-fix it returns quickly having abandoned the candidate.
    result = asyncio.run(
        asyncio.wait_for(rec.reconcile_once(config=cfg, gerrit=_GC(), dedup=store), timeout=5)
    )
    assert result == {"scanned": 1, "reviewed": 0}


@pytest.mark.timeout(3)
def test_lifespan_is_safe_by_default_without_per_test_stubs(monkeypatch, tmp_path):
    """The ordinary TestClient lifespan path must be prompt and use the test drain bound."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rebar.review_bot import app as appmod
    from rebar.review_bot.config import shutdown_drain_seconds

    monkeypatch.setattr(appmod.app.state, "config", _cfg(tmp_path))

    start = time.monotonic()
    with TestClient(appmod.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "in_flight": 0, "queue_depth": 0}

    # timing: hang-guard — 2s dwarfs this sub-second local lifecycle path.
    assert time.monotonic() - start < 2
    assert shutdown_drain_seconds() == 1.0


def _idle_reconcile_loop(*a, **k):
    async def _loop():
        await asyncio.Event().wait()

    return _loop()


@pytest.fixture(autouse=True)
def _safe_review_bot_lifespan_defaults(monkeypatch, request):
    """Keep review-bot lifespan tests off real background work unless requested."""
    if request.node.get_closest_marker("real_reconcile_loop") is None:
        monkeypatch.setattr(reconcile, "reconcile_loop", _idle_reconcile_loop, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )


@pytest.mark.real_reconcile_loop
def test_lifespan_can_opt_into_the_real_reconcile_loop(monkeypatch, tmp_path):
    """The marker bypasses the safe stub while keeping constructor seams offline."""
    import threading

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rebar.review_bot import app as appmod

    started = threading.Event()
    calls: list[tuple[object, object]] = []
    fake_gerrit = object()
    fake_dedup = object()

    async def _record_reconcile_once(*, config, gerrit, dedup):
        calls.append((gerrit, dedup))
        started.set()
        return {"scanned": 0, "reviewed": 0}

    monkeypatch.setattr(appmod._reconcile, "GerritClient", lambda config: fake_gerrit)
    monkeypatch.setattr(appmod._reconcile, "DedupStore", lambda path: fake_dedup)
    monkeypatch.setattr(appmod._reconcile, "reconcile_once", _record_reconcile_once)
    monkeypatch.setattr(appmod.app.state, "config", _cfg(tmp_path))

    with TestClient(appmod.app):
        assert started.wait(timeout=1), "the real reconcile loop never started"

    assert calls == [(fake_gerrit, fake_dedup)]


def test_lifespan_drains_queued_events_on_shutdown(monkeypatch, tmp_path):
    """On shutdown the still-running worker drains queued events instead of the queue being
    dropped — so a routine autodeploy restart does not abandon acknowledged (202) webhooks.
    Pre-fix the worker is cancelled immediately and the queued events are lost."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    processed: list = []

    async def _fake_review(event, *, config, force=False):
        await asyncio.sleep(0.02)
        processed.append(event)

    monkeypatch.setattr(appmod._voter, "review_and_vote", _fake_review, raising=True)
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            for i in range(5):
                fake_app.state.queue.put_nowait(_event(revision=f"d{i}"))
            # exit immediately → the shutdown drain must process all 5 before cancelling

    asyncio.run(_run())
    assert len(processed) == 5


def test_lifespan_drain_is_bounded(monkeypatch, tmp_path):
    """The drain is bounded by SHUTDOWN_DRAIN_SECONDS: a review that never returns must not hang
    shutdown — the drain times out and the worker is cancelled (the rest falls to reconcile)."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod
    from rebar.review_bot.config import shutdown_drain_seconds

    async def _slow_review(event, *, config, force=False):
        await asyncio.sleep(3600)

    monkeypatch.setattr(appmod._voter, "review_and_vote", _slow_review, raising=True)
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")
    assert shutdown_drain_seconds() == 0.1
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            fake_app.state.queue.put_nowait(_event())

    # Must return within the outer bound despite the hung review (bounded drain then cancel).
    asyncio.run(asyncio.wait_for(_run(), timeout=5))


def _compose_stop_grace_seconds() -> float:
    """The review-bot service's declared stop_grace_period, in seconds."""
    import pathlib
    import re

    yaml = pytest.importorskip("yaml")
    root = pathlib.Path(__file__).resolve().parents[2]
    d = yaml.safe_load((root / "infra/compose/docker-compose.yml").read_text())
    raw = d["services"]["review-bot"].get("stop_grace_period")
    assert raw, "the review-bot service must declare a stop_grace_period"
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|m)?\s*", str(raw))
    assert match, f"unparseable stop_grace_period {raw!r}"
    return float(match.group(1)) * (60 if match.group(2) == "m" else 1)


def test_reviewbot_healthcheck_probes_its_own_listener():
    """The review-bot service must declare a container healthcheck that probes its OWN
    listener at ``/health``.

    This is the AC4 signal for the 2026-08-28 zombie: uvicorn closed its :8000 listener at
    the start of a graceful shutdown but the process never exited, so ``docker compose ps``
    kept reporting the container ``Up`` while every request RST'd — a silent 502. A per-container
    healthcheck that hits the process's own loopback listener flips the container to
    ``unhealthy`` in exactly that state (the connection is refused), turning a silent zombie
    into an observable, alarmable signal. Without this stanza the state is indistinguishable
    from healthy at the container layer, which is what let it run for 22 minutes.
    """
    import pathlib

    yaml = pytest.importorskip("yaml")
    root = pathlib.Path(__file__).resolve().parents[2]
    d = yaml.safe_load((root / "infra/compose/docker-compose.yml").read_text())
    hc = d["services"]["review-bot"].get("healthcheck")
    assert hc, "the review-bot service must declare a healthcheck (AC4: the zombie must alarm)"
    test = hc.get("test")
    assert test, "the review-bot healthcheck must declare a `test`"
    argv = healthcheck_test_argv(test)
    probe = " ".join(argv)
    # Probes the process's OWN listener (container loopback) at the /health route, so a closed
    # listener with a live process is detected as unhealthy rather than reading green.
    assert "/health" in probe, f"the healthcheck must probe /health, got {probe!r}"
    assert "127.0.0.1:8000" in probe, (
        f"the healthcheck must hit the review-bot's own container listener on 127.0.0.1:8000, "
        f"got {probe!r}"
    )
    assert_socket_healthcheck_semantics(argv)


def test_reviewbot_stop_grace_period_covers_an_in_flight_store_write():
    """The grace period must outlast a shutdown that is still finishing a store write.

    A SIGTERM starts the lifespan drain, which waits for the in-flight review to finish.
    That review ends in ``emit_code_review_artifact`` -> ``event_append.stage_and_commit``,
    which holds the store's **mkdir** write lock. Unlike the fcntl leg, that lock dir is NOT
    released by the kernel when the process dies, so a SIGKILL landing inside that region
    orphans it — and a lock stamped by one container cannot be reclaimed by the next.

    So the grace period has to cover the drain window PLUS the write that may only be
    starting as the window closes, and the dominant term in that write is the store write
    lock's own acquisition budget. Both are read from source here rather than restated as
    literals, so raising either budget fails this test until the grace period follows.
    """
    # Deliberately read from ``config``, not ``app``: this assertion must run in the default
    # test tier, where the fastapi-laden ``app`` module is not importable.
    from rebar._store import lock as _lock
    from rebar.review_bot.config import DEFAULT_SHUTDOWN_DRAIN_SECONDS

    lock_budget = _lock._DEFAULT_TIMEOUT * _lock._DEFAULT_ATTEMPTS
    assert DEFAULT_SHUTDOWN_DRAIN_SECONDS == 1200
    floor = DEFAULT_SHUTDOWN_DRAIN_SECONDS + lock_budget
    grace = _compose_stop_grace_seconds()

    assert grace >= floor, (
        f"stop_grace_period is {grace}s but a shutdown can legitimately spend "
        f"{DEFAULT_SHUTDOWN_DRAIN_SECONDS}s draining the queue and then a further "
        f"{lock_budget}s acquiring the store write lock for the artifact write "
        f"({lock_budget}s = lock._DEFAULT_TIMEOUT x _DEFAULT_ATTEMPTS). Sizing the grace "
        f"period against the drain window alone leaves SIGKILL landing mid-write, which "
        f"orphans the mkdir lock dir; it must be at least {floor}s."
    )


def test_force_exit_deadline_stays_within_the_stop_grace_band():
    """The lifespan's hard force-exit deadline must sit in the safe band: strictly BELOW the
    container ``stop_grace_period`` (so the controlled ``os._exit`` fires before Docker's own
    SIGKILL, guaranteeing the port is released even when the recreate path's SIGKILL escalation
    is unreliable — the 2026-08-28 22-minute zombie), yet at least the drain + store-write-lock
    budget (so it NEVER preempts a legitimate in-flight store write and orphans the mkdir lock,
    exactly the hazard the stop_grace test above guards). Read from source, so raising any
    budget fails CI until the deadline follows. Runs in the default tier: ``config`` is imported
    without the fastapi-laden ``app`` module.
    """
    from rebar._store import lock as _lock
    from rebar.review_bot.config import (
        DEFAULT_SHUTDOWN_CANCEL_SECONDS,
        DEFAULT_SHUTDOWN_DRAIN_SECONDS,
        shutdown_force_exit_grace_seconds,
    )

    lock_budget = _lock._DEFAULT_TIMEOUT * _lock._DEFAULT_ATTEMPTS
    grace = shutdown_force_exit_grace_seconds()
    # The deadline is measured from the START of the graceful shutdown: the whole drain, then
    # the bounded cancel, then this post-cancel grace for an abandoned store write to release
    # its lock before the force-exit.
    deadline = DEFAULT_SHUTDOWN_DRAIN_SECONDS + DEFAULT_SHUTDOWN_CANCEL_SECONDS + grace
    stop_grace = _compose_stop_grace_seconds()

    assert grace >= lock_budget, (
        f"the force-exit grace is {grace}s but a legitimate abandoned store write needs at "
        f"least {lock_budget}s to acquire the mkdir write lock; a shorter grace force-exits "
        f"mid-write and orphans the lock dir."
    )
    assert deadline < stop_grace, (
        f"the force-exit deadline (drain {DEFAULT_SHUTDOWN_DRAIN_SECONDS}s + cancel "
        f"{DEFAULT_SHUTDOWN_CANCEL_SECONDS}s + grace {grace}s = {deadline}s) must fire BEFORE "
        f"the container SIGKILLs at stop_grace_period {stop_grace}s, otherwise Docker's own "
        f"kill wins and the controlled, logged exit never happens."
    )


def test_sigkill_during_a_store_write_orphans_the_mkdir_lock(tmp_path):
    """The control for the test below, and the reason stop_grace_period is the operative
    safeguard: nothing in-process can clean up after SIGKILL.

    ``write_lock`` releases from a ``finally`` (``_store/lock.py``), so every graceful exit
    path — including ``CancelledError`` — releases both legs. SIGKILL runs no ``finally``, and
    while the fcntl leg is released by the kernel, the mkdir dir simply stays. So the ONLY
    control over this failure is giving the shutdown enough time to finish the write.
    """
    import signal
    import subprocess
    import sys

    from rebar._store import lock as _lock

    tracker = tmp_path / "tracker"
    tracker.mkdir()
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from rebar._store import lock\n"
            "h = lock.acquire(sys.argv[1], dual_window=True)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n",
            str(tracker),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked", "child never took the lock"
        assert lock_dir.is_dir(), "precondition: the mkdir lock dir must exist while held"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:  # pragma: no cover - only on an unexpected hang
            child.kill()
            child.wait(timeout=10)

    assert lock_dir.is_dir(), (
        "SIGKILL must be shown to leave the mkdir lock dir behind — that is the hazard the "
        "grace period exists to avoid. If this ever stops holding, the lock gained some "
        "out-of-process reclamation and the grace-period sizing can be revisited."
    )


def test_shutdown_completes_an_in_flight_store_write_and_releases_the_lock(monkeypatch, tmp_path):
    """AC2: a shutdown that interrupts an in-flight store write must let it finish and
    release the write lock, leaving no lock dir behind.

    This is what makes the grace period worth having: the shutdown path genuinely drains the
    write rather than abandoning it, so the only thing that can orphan the lock is running
    out of grace. Exercised through the real ``lifespan`` shutdown (which is exactly what
    uvicorn runs on SIGTERM) against the real ``write_lock``.
    """
    import types

    pytest.importorskip("fastapi")
    from rebar._store import lock as _lock
    from rebar.review_bot import app as appmod

    tracker = tmp_path / "tracker"
    tracker.mkdir()
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    held: list[bool] = []

    async def _review_that_writes(event, *, config, force=False):
        # The shape of emit_code_review_artifact: synchronous, lock-held, no await inside.
        with _lock.write_lock(tracker, dual_window=True):
            held.append(lock_dir.is_dir())
            time.sleep(0.3)  # noqa: ASYNC251 - deliberately synchronous: models emit_code_review_artifact's no-await-inside lock-held window

    monkeypatch.setattr(appmod._voter, "review_and_vote", _review_that_writes, raising=True)
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            fake_app.state.queue.put_nowait(_event())
            await asyncio.sleep(0.05)  # let the worker enter the lock-held region
            # leaving the context = the shutdown uvicorn drives on SIGTERM

    asyncio.run(_run())

    assert held == [True], (
        f"the fixture must actually have held the mkdir write lock mid-shutdown; got {held}"
    )
    assert not lock_dir.exists(), (
        "the shutdown abandoned an in-flight store write with the mkdir lock dir still "
        "present. That dir is not kernel-released, and a lock stamped by one container "
        "cannot be reclaimed by the next, so the next container's ensure sweep burns its "
        "full lock budget and the deploy health-checks out."
    )


def test_reviewbot_compose_trusts_tickets_dir_via_safe_directory():
    """The review-bot container runs as root over a uid-1000-owned persistent tickets volume;
    without git safe.directory the dubious-ownership guard refuses every op on it, so every
    code_review artifact emission fails. Assert the compose service injects
    safe.directory=<tickets dir> via GIT_CONFIG_* (equivalent to `git -c`, HOME-independent)."""
    import pathlib

    yaml = pytest.importorskip("yaml")
    root = pathlib.Path(__file__).resolve().parents[2]
    d = yaml.safe_load((root / "infra/compose/docker-compose.yml").read_text())
    env = d["services"]["review-bot"]["environment"]
    if isinstance(env, list):  # compose allows either a dict or a list of "K=V"
        env = dict(e.split("=", 1) for e in env)
    assert str(env.get("GIT_CONFIG_COUNT")) == "1"
    assert env.get("GIT_CONFIG_KEY_0") == "safe.directory"
    assert env.get("GIT_CONFIG_VALUE_0") == "/var/gerrit/site/reviewbot-tickets"


# ── c2ba: bounded shutdown / off-loop store write ─────────────────────────────
def test_emit_code_review_artifact_runs_off_the_event_loop(monkeypatch, tmp_path):
    """AC1: the code_review artifact emission — a SYNCHRONOUS, lock-held store write — must run
    OFF the asyncio event loop (via asyncio.to_thread), so it cannot block the loop and thereby
    unenforce the drain and per-review wait_for bounds. Pre-fix voter.py called the synchronous
    emit_code_review_artifact directly on the loop thread; this asserts it is offloaded."""
    import threading

    _patch_review(monkeypatch, [])  # clean → PASS, so review_and_vote reaches the emit path
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    emit_threads: list[int] = []

    def _spy_emit(*a, **k):
        emit_threads.append(threading.get_ident())

    monkeypatch.setattr(voter, "emit_code_review_artifact", _spy_emit, raising=True)

    async def _run():
        loop_thread = threading.get_ident()
        res = await voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store)
        return loop_thread, res

    loop_thread, res = asyncio.run(_run())
    assert res["status"] == "voted", res
    assert emit_threads, "emit_code_review_artifact was never reached"
    assert emit_threads[0] != loop_thread, (
        "emit_code_review_artifact ran on the event-loop thread; it must be offloaded via "
        "asyncio.to_thread so its synchronous, lock-held store write cannot block the loop "
        "(which is what makes the drain/per-review bounds unenforceable) — c2ba."
    )


def test_lifespan_cancel_await_is_bounded_for_a_task_slow_to_cancel(monkeypatch, tmp_path):
    """AC2: the lifespan's cancel + await path must be bounded end to end, so total shutdown has
    a stateable upper bound even if a background task is slow to honor cancellation (a shielded
    cleanup, a synchronous finally, or — the c2ba insight — an orphaned to_thread worker whose
    OS thread cannot be force-cancelled). Pre-fix the unbounded `await task` hangs on exactly
    such a task; post-fix the bounded cancel/await abandons it."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    async def _idle_worker(queue, cfg):
        while True:  # a well-behaved worker: parks on the queue, honors cancellation promptly
            await queue.get()

    def _slow_reconcile_loop(*a, **k):
        async def _loop():
            cancels = 0
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # Models a task slow to honor cancellation: it swallows the FIRST cancel and
                    # keeps running (pre-fix, the lifespan's unbounded `await task` hangs here).
                    # It honors a SECOND cancel so asyncio.run's own teardown stays clean.
                    cancels += 1
                    if cancels >= 2:
                        raise

        return _loop()

    monkeypatch.setattr(appmod, "_worker", _idle_worker, raising=True)
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _slow_reconcile_loop, raising=True)
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")
    monkeypatch.setenv("SHUTDOWN_CANCEL_SECONDS", "0.3")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            # Let the worker + reconcile tasks reach their await points, so the reconcile task
            # is genuinely mid-await (not cancelled-before-start) when shutdown cancels it —
            # that is the state in which the unbounded `await task` actually hangs.
            await asyncio.sleep(0.05)

    # Pre-fix the lifespan's unbounded `await task` hangs on the slow-to-cancel reconcile task
    # (only the 10s safety net stops it); post-fix the lifespan's own bounded cancel/await
    # abandons it in ~SHUTDOWN_CANCEL_SECONDS. Assert on elapsed so a hang FAILS fast rather
    # than slow-passing at the outer cap.
    start = time.monotonic()
    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=10))
    except (asyncio.TimeoutError, TimeoutError):
        pass  # the safety net fired — elapsed asserted below turns a hang into a fast failure
    elapsed = time.monotonic() - start
    # timing: hang-guard — shutdown-hang guard; unbounded lifespan cancel is the c2ba failure mode
    assert elapsed < 2.0, (
        f"review-bot shutdown took {elapsed:.2f}s — the lifespan cancel/await is not bounded; "
        "a background task slow to honor cancellation hangs shutdown (c2ba AC2)."
    )


def test_shutdown_forces_process_exit_when_an_orphaned_to_thread_review_outlives_cancel(
    monkeypatch, tmp_path
):
    """AC1/AC2/AC5 (unoutlawed-eloquent-amphibian): the ``drain + cancel`` bound must hold at
    the PROCESS level, not merely for the asyncio await.

    Every review runs its blocking work through ``asyncio.to_thread`` on the default
    ``ThreadPoolExecutor``, whose worker threads are **non-daemon**. Cancelling a task parked in
    such an offload returns the *coroutine* at once (so the lifespan's bounded ``gather``
    succeeds) but the OS thread keeps running the abandoned review. ``asyncio.run``'s own
    teardown then JOINS the default executor (uvicorn's runner gives it a 5-minute window) and
    interpreter finalization joins the surviving non-daemon thread with NO bound — so the
    process, whose listener uvicorn already closed at the start of graceful shutdown, stays
    alive (holding its published ``:8000``) for the whole remaining review. That is the live
    2026-08-28 review-bot zombie, and it violates ADR 0067's ``total shutdown <= drain +
    cancel`` invariant, which names exactly "the orphaned OS thread of an ``asyncio.to_thread``
    offload that cannot be force-cancelled" as the hazard the bound must cover.

    The lifespan must therefore FORCE THE PROCESS DOWN once its bounded shutdown work is done,
    so the container releases the port for the replacement. This asserts the force-down ACTION
    fired (an injected seam — a real ``os._exit`` cannot be observed in-process), not a
    stopwatch, so it does not depend on the banned wall-clock timing class.
    """
    import threading
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    exits: list[int] = []
    # Held-out seam: record the force-down instead of actually terminating the test process.
    monkeypatch.setattr(
        appmod, "_force_process_exit", lambda code=0: exits.append(code), raising=False
    )
    # Short force-exit grace so the daemon deadline fires quickly under test.
    monkeypatch.setattr(appmod, "shutdown_force_exit_grace_seconds", lambda: 0.2, raising=True)

    thread_started = threading.Event()
    thread_finished = threading.Event()

    def _orphaned_blocking_review():
        thread_started.set()
        time.sleep(1.5)  # the abandoned review still running in the to_thread OS thread
        thread_finished.set()

    async def _worker_that_offloads(queue, cfg):
        # Mirror production: the review's blocking work runs via asyncio.to_thread on the
        # default (non-daemon) executor. Cancelling THIS coroutine unwinds it at once, but the
        # OS thread keeps running _orphaned_blocking_review — the un-force-cancellable case.
        await asyncio.to_thread(_orphaned_blocking_review)

    def _idle_reconcile_loop(*_a, **_k):
        async def _loop():
            await asyncio.sleep(3600)  # well-behaved: honors cancellation promptly

        return _loop()

    monkeypatch.setattr(appmod, "_worker", _worker_that_offloads, raising=True)
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _idle_reconcile_loop, raising=True)
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")
    monkeypatch.setenv("SHUTDOWN_CANCEL_SECONDS", "0.2")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            # Let the worker reach the to_thread offload so the OS thread is genuinely running
            # when shutdown cancels the task (that is the state that hangs the teardown).
            for _ in range(100):
                if thread_started.is_set():
                    break
                await asyncio.sleep(0.02)

    # asyncio.run's teardown blocks on the orphaned thread (~1.5s) even after the fix; the
    # daemon hard-shutdown deadline fires the force-down seam while it is blocked.
    asyncio.run(_run())
    for _ in range(200):  # poll for the daemon deadline to fire — no wall-clock assertion
        if exits:
            break
        time.sleep(0.02)

    assert exits == [0], (
        "the lifespan did not force the process down: an orphaned asyncio.to_thread review OS "
        "thread will keep the container (and its published :8000) alive past drain+cancel — "
        "the 2026-08-28 review-bot zombie. ADR 0067's drain+cancel bound must hold for the "
        "PROCESS, not just the asyncio await."
    )
    # Let the orphaned thread finish so it does not leak into later tests.
    thread_finished.wait(3.0)


def test_clean_shutdown_with_no_orphaned_offload_does_not_force_process_exit(monkeypatch, tmp_path):
    """The other half of the self-distinguishing contract (finding, change 2381): a normal
    lifespan shutdown whose tasks leave NO orphaned ``asyncio.to_thread`` thread must NOT
    force the process down — the hard deadline is armed ONLY when an un-force-cancellable
    offload thread is actually still running, so a clean teardown exits on its own.

    Drives a lifespan whose worker and reconciler both park in ``asyncio.sleep`` (never
    offloading to a thread) and honor cancellation promptly. With a tiny force-exit grace, an
    unconditional arm would fire the seam; the gate means it is never armed. Asserts the
    force-down seam is NOT called (the recorder stays empty), waiting past the grace.
    """
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    exits: list[int] = []
    monkeypatch.setattr(
        appmod, "_force_process_exit", lambda code=0: exits.append(code), raising=False
    )
    monkeypatch.setattr(appmod, "shutdown_force_exit_grace_seconds", lambda: 0.2, raising=True)

    async def _well_behaved_worker(queue, cfg):
        await asyncio.sleep(3600)  # parks on the loop; no to_thread offload

    def _idle_reconcile_loop(*_a, **_k):
        async def _loop():
            await asyncio.sleep(3600)

        return _loop()

    monkeypatch.setattr(appmod, "_worker", _well_behaved_worker, raising=True)
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _idle_reconcile_loop, raising=True)
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")
    monkeypatch.setenv("SHUTDOWN_CANCEL_SECONDS", "0.2")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            await asyncio.sleep(0.05)  # let the tasks reach their await points

    asyncio.run(_run())
    time.sleep(0.5)  # well past the 0.2s grace — a mistaken arm would have fired by now

    assert exits == [], (
        "a clean shutdown with no orphaned to_thread offload force-exited the process; the "
        "hard deadline must be armed only when an un-force-cancellable offload thread is "
        "actually still running (change 2381 review finding)."
    )


# ── 9ec0: the shutdown drain must cover the RECONCILER's inline review ────────
#
# ``queue.join()`` drains the WEBHOOK queue only. The reconciler awaits ``review_and_vote``
# INLINE (reconcile.reconcile_once), outside that queue, so with an empty queue ``join()``
# returns immediately and the reconcile task is cancelled within ``SHUTDOWN_CANCEL_SECONDS``
# — abandoning a backfill review that may be minutes in. The reconciler is the path that
# RETRIES a review killed by anything else, so the gap sits on the self-heal path.
#
# These tests drive the REAL ``reconcile_loop`` (a test that drives the queue would prove
# nothing here: the queue path is already protected).
def _reconciler_probe(monkeypatch, tmp_path, *, gap_count=1, review_seconds=0.4):
    """Wire the real reconcile_loop onto a fake events-log with ``gap_count`` vote-less changes.

    Slows the review by stubbing the GATE (``produce_code_review_verdict``) rather than by
    replacing ``review_and_vote``, so the REAL ``review_and_vote`` runs. That matters for the
    lifespan tests: the drain gate reads ``voter.in_flight_reviews()``, whose count is held by
    ``_counting_in_flight`` INSIDE ``review_and_vote`` — a stubbed-out review would never hold
    it, and the test would then be asserting against a count it manufactured itself.

    Returns (cfg, gerrit, started). Completion is read from ``gerrit.votes``: a cast vote is the
    real end of the pipeline, a stronger signal than a stub's own transcript.
    """
    cfg = _cfg(tmp_path)
    started: list[str] = []

    import rebar.llm.workflow.gate_dispatch as gd

    def _slow_gate(request):
        started.append(getattr(request, "change_id", "?"))
        time.sleep(review_seconds)  # the long LLM pass, off-loop via to_thread
        return _verdict_from_findings([])  # clean → PASS → a vote is cast

    gerrit = ReconcileGerrit(
        events=[
            _events_log_event(f"rebar~main~Igap{i}", f"rev-gap{i}", number=100 + i)
            for i in range(gap_count)
        ]
    )
    monkeypatch.setattr(gd, "produce_code_review_verdict", _slow_gate, raising=True)
    monkeypatch.setattr(reconcile, "GerritClient", lambda *_a, **_k: gerrit, raising=True)
    return cfg, gerrit, started


def _spy_drain_wait(monkeypatch):
    """Record the ``timeout`` handed to each ``asyncio.wait`` during the lifespan shutdown.

    The lifespan's reconciler drain is the only ``asyncio.wait`` on that path, so this is a
    STRUCTURAL proxy for two properties that would otherwise need upper-bound wall-clock
    asserts — the proven CI flake class the wall-clock lint bans:

    * WHETHER the drain ran at all (the ``in_flight_reviews()`` gate short-circuits it), and
    * WHETHER it was handed the REMAINING shared budget rather than a fresh full one.

    Reading the timeout instead of the elapsed time also fails in the SAFE direction under
    runner contention: a loaded runner leaves LESS budget remaining, never more, so a
    ``<`` assertion on the recorded value cannot flake from slowness the way a stopwatch does.
    """
    calls: list[float | None] = []
    real_wait = asyncio.wait

    async def _spy(aws, **kw):
        calls.append(kw.get("timeout"))
        return await real_wait(aws, **kw)

    monkeypatch.setattr(asyncio, "wait", _spy, raising=True)
    return calls


async def _await_first_review(started, *, cap=3.0):
    """Wait until the reconciler is genuinely INSIDE its review, so shutdown interrupts an
    in-flight review rather than racing the pass's setup."""
    await _await_probe(started, cap=cap, what="the reconciler's review")


@pytest.mark.real_reconcile_loop
def test_shutdown_drains_an_in_flight_reconciler_review(monkeypatch, tmp_path):
    """AC1: a reconciler review in flight at shutdown must be DRAINED, not cancelled.

    Pre-fix this fails: the webhook queue is empty, so ``queue.join()`` returns immediately
    and the reconcile task is cancelled straight away, so the review never completes."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    cfg, gerrit, started = _reconciler_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    # A bounded drain window that comfortably exceeds the fixture review, so a FAILURE here
    # is "the review was cancelled", never "the budget was too small".
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "5")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=cfg))

    async def _run():
        async with appmod.lifespan(fake_app):
            await _await_first_review(started)
            # leaving the context = the shutdown uvicorn drives on SIGTERM

    asyncio.run(asyncio.wait_for(_run(), timeout=20))

    assert len(started) == 1, started
    assert [v[0] for v in gerrit.votes] == ["rebar~main~Igap0"], (
        "shutdown ABANDONED an in-flight reconciler review: it started but never cast its vote. "
        "queue.join() drains the webhook queue only, so with an empty queue the reconcile "
        "task is cancelled within SHUTDOWN_CANCEL_SECONDS while its inline review_and_vote "
        "is mid-flight. The reconciler is the RETRY path, so this is the self-heal path "
        f"being killed (9ec0 AC1). votes={gerrit.votes}"
    )


@pytest.mark.real_reconcile_loop
def test_shutdown_does_not_let_the_reconciler_start_a_new_review(monkeypatch, tmp_path):
    """AC2: the drain must not extend indefinitely — once shutdown has begun the reconciler
    may finish the review in flight but must start NO new one.

    Two vote-less changes are queued in the events-log. Shutdown lands during the first
    review; the second must never start. This is the guard against 'fix' shapes that merely
    let the reconcile loop run to completion, which would drain candidate after candidate
    and could re-extend shutdown up to the full budget."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    cfg, gerrit, started = _reconciler_probe(monkeypatch, tmp_path, gap_count=2)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "5")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=cfg))

    async def _run():
        async with appmod.lifespan(fake_app):
            await _await_first_review(started)

    asyncio.run(asyncio.wait_for(_run(), timeout=20))

    assert len(gerrit.votes) == 1, (
        f"the in-flight review must be drained exactly once: votes={gerrit.votes}"
    )
    assert len(started) == 1, (
        f"shutdown let the reconciler START a new review after the drain began: {started}. "
        "Draining must stop accepting new work, or each fresh candidate re-extends shutdown "
        "(9ec0 AC2)."
    )
    # "One review, not two" is exactly what the two asserts above already pin, structurally and
    # without a clock. A wall-clock ceiling on top would add no signal and only a flake risk:
    # a drain that DID re-extend would have to start a second review to do so, which
    # ``len(started) == 1`` already catches.


@pytest.mark.real_reconcile_loop
def test_shutdown_reconciler_drain_shares_the_queue_drain_budget(monkeypatch, tmp_path):
    """The reconciler drain must run under the SAME ``shutdown_drain_seconds()`` deadline as
    the queue drain, not a second independent one.

    ``test_reviewbot_stop_grace_period_covers_an_in_flight_store_write`` sizes the container's
    ``stop_grace_period`` as ``DEFAULT_SHUTDOWN_DRAIN_SECONDS + <store lock budget>``. Two
    sequential windows of one drain budget each would make a real shutdown able to spend
    ``2 x drain`` before the store write even starts, silently invalidating that sizing —
    and overrunning the grace period means SIGKILL, which is strictly worse than a clean cancel.

    To tell the two apart the QUEUE drain must consume most of the budget first: a webhook
    review runs for 0.12s of a 0.2s test budget, and the reconciler review then outlasts
    whatever is left. The 60/40 ratio is the contract; production's 1200s budget is unchanged.
    With an empty queue the two are indistinguishable, which is exactly the vacuous shape this
    avoids."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    cfg = _cfg(tmp_path)
    started: list[str] = []
    completed: list[str] = []

    # The queue half is stubbed at review_and_vote (its drain is queue.join(), which does not
    # consult the in-flight count), while the reconciler half runs the REAL review so the
    # drain gate sees a genuine in_flight count.
    real_review_and_vote = voter.review_and_vote

    async def _review(event, *, config=None, gerrit=None, dedup=None, force=False):
        change_id = event["change"]["id"]
        if change_id.endswith("gap0"):  # the reconciler's candidate: real path, outlasts budget
            return await real_review_and_vote(
                event, config=config, gerrit=gerrit, dedup=dedup, force=force
            )
        started.append(change_id)
        await asyncio.sleep(0.12)  # the webhook's review eats most of the test budget
        completed.append(change_id)
        return {"status": "voted", "change_id": change_id}

    import rebar.llm.workflow.gate_dispatch as gd

    def _endless_gate(request):
        started.append("gap0")
        # Outlast the whole test budget; asyncio.run joins the orphaned worker at teardown.
        time.sleep(0.6)
        return _verdict_from_findings([])

    gerrit = ReconcileGerrit(events=[_events_log_event("rebar~main~Igap0", "rev-gap0", number=1)])
    monkeypatch.setattr(gd, "produce_code_review_verdict", _endless_gate, raising=True)
    monkeypatch.setattr(voter, "review_and_vote", _review, raising=True)
    monkeypatch.setattr(reconcile, "GerritClient", lambda *_a, **_k: gerrit, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.2")
    monkeypatch.setenv("SHUTDOWN_CANCEL_SECONDS", "0.03")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=cfg))

    drain_timeouts = _spy_drain_wait(monkeypatch)

    async def _run():
        async with appmod.lifespan(fake_app):
            await _await_first_review(started)  # the reconciler review is in flight
            fake_app.state.queue.put_nowait(_event())  # and a webhook review will drain first
            await asyncio.sleep(0.05)

    asyncio.run(asyncio.wait_for(_run(), timeout=60))

    assert completed == ["rebar~main~Iabc"], (
        f"the webhook review must drain and the reconciler's must outlast the budget: {completed}"
    )
    # THE CONTRACT, read structurally off the budget the drain was actually handed rather than
    # off a stopwatch. The 0.2s test budget and ~0.12s webhook review retain production's
    # important 60/40 geometry, so a SHARED deadline can hand the reconciler wait only the
    # ~0.08s that remain. Two independent windows would hand it a fresh 0.2s — which breaks the
    # stop_grace_period sizing that test_reviewbot_stop_grace_period_covers_an_in_flight_store_
    # write pins, and an overrun means SIGKILL mid-store-write.
    assert drain_timeouts, (
        "the reconciler drain never ran, so this test is no longer exercising the shared "
        "deadline at all"
    )
    assert drain_timeouts[0] is not None and drain_timeouts[0] < 0.15, (
        f"the reconciler drain was given {drain_timeouts[0]}s against a 0.2s test "
        "SHUTDOWN_DRAIN_SECONDS of which the queue drain had already spent ~0.12s. It must "
        "receive only what REMAINS of one shared deadline, not a fresh full window."
    )


@pytest.mark.real_reconcile_loop
def test_shutdown_is_prompt_when_the_reconciler_has_no_review_in_flight(monkeypatch, tmp_path):
    """The new drain must be scoped to an in-flight REVIEW, not to the reconcile task at large.

    A reconciler parked in some other slow step — here a blocking events-log fetch — has no
    review at risk, so shutdown must stay as prompt as it is today rather than waiting out the
    drain budget. Without the ``voter.in_flight_reviews()`` gate this waits for the fetch to
    return, which in production (a 1200s budget against a hung HTTP call) turns a ~10s shutdown
    into minutes and eats the grace period that the store write needs."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    cfg = _cfg(tmp_path)
    _patch_review(monkeypatch, [])

    import threading

    release_fetch = threading.Event()

    class SlowFetchGerrit(ReconcileGerrit):
        def list_events(self, since=None):
            self.list_since_calls.append(since)
            # A barrier, not a sleep: this remains genuinely blocked off-loop until shutdown
            # has proven it does not drain unrelated reconciler work. The timeout is cleanup
            # protection only, so a broken test cannot orphan the executor thread forever.
            assert release_fetch.wait(timeout=10)
            return []

    gerrit = SlowFetchGerrit()
    monkeypatch.setattr(reconcile, "GerritClient", lambda *_a, **_k: gerrit, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.2")
    monkeypatch.setenv("SHUTDOWN_CANCEL_SECONDS", "0.03")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=cfg))
    drain_timeouts = _spy_drain_wait(monkeypatch)

    async def _run():
        try:
            async with appmod.lifespan(fake_app):
                # the reconciler is inside the blocking fetch
                await _await_probe(gerrit.list_since_calls, what="the events-log fetch")
        finally:
            release_fetch.set()

    asyncio.run(asyncio.wait_for(_run(), timeout=30))

    # Read structurally rather than with a stopwatch: with no review in flight the
    # ``voter.in_flight_reviews()`` gate must short-circuit the drain ENTIRELY, so the wait is
    # never entered. That is a stronger statement than "it finished quickly" — a stopwatch
    # would also pass if the wait ran and happened to return fast — and it cannot flake under
    # runner contention the way an upper-bound elapsed assert does.
    assert drain_timeouts == [], (
        "shutdown entered the reconciler drain with NO review in flight (wait timeouts: "
        f"{drain_timeouts}). It must be gated on voter.in_flight_reviews(), or a reconciler "
        "stuck in any other slow step — here a blocking events-log fetch — holds the whole "
        "shutdown_drain_seconds() budget instead of being cancelled promptly."
    )


@pytest.mark.real_reconcile_loop
def test_shutdown_does_not_leave_the_reconciler_permanently_stopped(monkeypatch, tmp_path):
    """The stop flag must not outlive the shutdown that set it.

    ``_stop_requested`` is module state on ``reconcile``, not per-app, so a lifespan that exits
    with it still set silently neuters EVERY later ``reconcile_once`` in the same process — the
    pass declines every candidate and returns ``reviewed: 0`` while still reporting them as
    ``scanned``, which looks like "nothing was owed a vote" rather than like a fault.

    Clearing only at the next lifespan STARTUP is not enough: the reconciler is also driven
    directly, with no lifespan, by the replacement-process recovery path that re-drives a rerun
    the discarded in-memory queue lost (``test_accepted_rerun_survives_restart_via_gerrit_
    reconcile`` is the live oracle for that, and it is what caught this). A backfill that
    silently reviews nothing is the exact failure mode 9ec0 exists to prevent, so this pins the
    post-shutdown state directly rather than only through that test's side effects."""
    import types

    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    cfg = _cfg(tmp_path)
    _patch_review(monkeypatch, [])
    gerrit = ReconcileGerrit(events=[])
    monkeypatch.setattr(reconcile, "GerritClient", lambda *_a, **_k: gerrit, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "1")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=cfg))

    async def _run():
        async with appmod.lifespan(fake_app):
            assert not reconcile.stop_requested(), "startup must not begin already stopping"
        # the shutdown has now run end to end

    asyncio.run(asyncio.wait_for(_run(), timeout=30))

    assert not reconcile.stop_requested(), (
        "the reconciler is still flagged as stopping AFTER shutdown completed; every later "
        "reconcile_once in this process will decline all work and report reviewed: 0"
    )


# ── retryable coverage gaps: defer vote-less + bounded escalation (ticket 0347) ──
def _gap_verdict(reason):
    """A four-pass verdict whose coverage block yields the given retryable gap sub-reason."""
    coverage = {
        "gate-disabled": {"enabled": False},
        "llm-unavailable": {"llm_unavailable": True, "llm_error": "outage"},
        "scanner": {
            "security_detectors": [
                {"criterion": "sec", "reason": "fail-closed-abstain", "abstain_reasons": ["x"]}
            ]
        },
        "low-disk": {"low_disk": True, "low_disk_free_bytes": 1024, "low_disk_min_bytes": 2048},
    }[reason]
    return {"verdict": "BLOCK", "blocking": [], "advisory": [], "coverage": coverage}


def _patch_gap(monkeypatch, reason):
    """Stub the gate so the adapter produces a coverage-gap decision with ``gap_reason``."""
    if reason == "review-error":
        _patch_verdict(monkeypatch, "not a dict")  # unparseable result → review-error
    else:
        _patch_verdict(monkeypatch, _gap_verdict(reason))


def test_adapter_decision_carries_gap_reason(monkeypatch, tmp_path):
    """The machine-readable ``gap_reason``: the retryable sub-reason on a coverage gap, and
    None on both a PASS and a real finding (the voter's defer-vs-vote discriminator)."""
    _patch_gap(monkeypatch, "llm-unavailable")
    out = adapter.code_review_decision("diff", str(tmp_path), "ref")
    assert out["gap_reason"] == "llm-unavailable"

    _patch_review(monkeypatch, [])
    assert adapter.code_review_decision("diff", str(tmp_path), "ref")["gap_reason"] is None

    _patch_review(monkeypatch, [{"severity": "critical", "dimension": "sec", "detail": "rce"}])
    assert adapter.code_review_decision("diff", str(tmp_path), "ref")["gap_reason"] is None


def test_dedup_attempt_budget_record_count_reset(tmp_path):
    store = DedupStore(str(tmp_path / "v.db"))
    assert store.attempt_count("c1", "r1") == 0
    assert store.record_attempt("c1", "r1") == 1
    assert store.record_attempt("c1", "r1") == 2
    assert store.attempt_count("c1", "r1") == 2
    assert store.attempt_count("c1", "r2") == 0  # per-revision
    store.reset_attempts("c1", "r1")
    assert store.attempt_count("c1", "r1") == 0  # DELETE: as if never attempted


@pytest.mark.parametrize(
    "reason", ["review-error", "llm-unavailable", "scanner", "gate-disabled", "low-disk"]
)
def test_voter_defers_voteless_on_retryable_gap(monkeypatch, tmp_path, caplog, reason):
    """AC2: a retryable coverage gap casts NO vote, posts nothing, records one attempt in
    the budget ledger (NOT the voted ledger), and emits the REVIEW_RETRY_DEFERRED marker."""
    _patch_gap(monkeypatch, reason)
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    with caplog.at_level(logging.WARNING, logger="rebar.review_bot.voter"):
        res = asyncio.run(
            voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store)
        )
    assert res["status"] == "deferred"
    assert res["gap_reason"] == reason
    assert g.votes == []  # genuinely vote-less: nothing posted to the change
    assert store.already_voted("rebar~main~Iabc", "rev1") is False
    assert store.attempt_count("rebar~main~Iabc", "rev1") == 1
    assert "REVIEW_RETRY_DEFERRED" in caplog.text


def test_adapter_low_disk_coverage_is_distinct_retryable_gap(monkeypatch, tmp_path):
    _patch_gap(monkeypatch, "low-disk")

    out = adapter.code_review_decision("diff", str(tmp_path), "ref")

    assert out["decision"] == "BLOCK"
    assert out["coverage_gap"] is True
    assert out["gap_reason"] == "low-disk"
    assert out["message"].startswith("[LLM-Review: BLOCK — coverage-gap (low-disk)]")
    assert "low-disk" in adapter.RETRYABLE_GAP_REASONS


def test_low_disk_retry_budget_exhaustion_remains_voteless(monkeypatch, tmp_path, capsys):
    _patch_gap(monkeypatch, "low-disk")
    cfg = dataclasses.replace(_cfg(tmp_path), retryable_gap_max_attempts=1)
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))

    res = asyncio.run(voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store))

    assert res["status"] == "deferred-exhausted"
    assert res["gap_reason"] == "low-disk"
    assert g.votes == []
    assert store.already_voted("rebar~main~Iabc", "rev1") is False
    assert "VOTER_ERROR" not in capsys.readouterr().err


def test_review_bot_low_disk_admission_defers_before_clone(monkeypatch, tmp_path):
    _patch_review(monkeypatch, [])
    cfg = _cfg(tmp_path)
    store = DedupStore(str(tmp_path / "v.db"))

    class CloneCountingGerrit(FakeGerrit):
        def __init__(self):
            super().__init__()
            self.clone_calls = 0

        def clone_change_ref(self, change_number, revision_ref, dest):
            self.clone_calls += 1
            raise AssertionError("clone must not start below the hard free-space floor")

    g = CloneCountingGerrit()
    # The free-space seam moved from a voter alias into low_disk.pre_clone_refusal, which is
    # now the single owner of both pre-clone host-disk conditions (bug 1ef8). Patch the owner.
    from rebar.review_bot import low_disk

    monkeypatch.setattr(low_disk, "review_clone_has_room", lambda _cfg: False)

    res = asyncio.run(voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store))

    assert res["status"] == "deferred"
    assert res["gap_reason"] == "low-disk"
    assert g.clone_calls == 0
    assert g.votes == []


def test_voter_indeterminate_is_terminal_and_votes(monkeypatch, tmp_path):
    """AC3: indeterminate ran to completion — a result, not an interruption — so it still
    casts the fail-closed -1 immediately (PASS/finding covered by the vote tests above)."""
    _patch_verdict(
        monkeypatch,
        {"verdict": "INDETERMINATE", "blocking": [], "advisory": [], "coverage": {}},
    )
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=_cfg(tmp_path), gerrit=g, dedup=store))
    assert res["status"] == "voted" and res["vote_value"] == -1
    assert g.votes and "coverage-gap (indeterminate)" in g.votes[0][3]
    assert store.attempt_count("rebar~main~Iabc", "rev1") == 0  # no budget burned


def test_voter_escalates_to_block_when_budget_exhausted(monkeypatch, tmp_path, capsys):
    """AC4: the Nth (default 3rd) retryable failure on the same (change, revision) casts the
    fail-closed -1 with the retries-exhausted note and emits VOTER_ERROR; the vote clears
    the budget row."""
    _patch_gap(monkeypatch, "llm-unavailable")
    cfg = _cfg(tmp_path)
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    for expected_attempt in (1, 2):
        res = asyncio.run(voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store))
        assert res["status"] == "deferred" and res["attempt"] == expected_attempt
    assert g.votes == []

    res = asyncio.run(voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store))
    assert res["status"] == "voted" and res["vote_value"] == -1
    message = g.votes[0][3]
    assert message.startswith("[LLM-Review: BLOCK — coverage-gap (llm-unavailable)]")  # tag intact
    assert "Automatic retries exhausted (3 attempt(s))" in message
    assert "VOTER_ERROR" in capsys.readouterr().err  # the escalation hits the alarm surface
    assert store.attempt_count("rebar~main~Iabc", "rev1") == 0  # budget cleared with the vote


def test_retryable_gap_max_attempts_is_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("RETRYABLE_GAP_MAX_ATTEMPTS", "1")
    cfg = ReceiverConfig.from_env()
    assert cfg.retryable_gap_max_attempts == 1
    # cap 1 → the FIRST retryable failure escalates straight to the -1
    _patch_gap(monkeypatch, "review-error")
    cfg = ReceiverConfig(
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token="tok",
        webhook_token="tok",
        project="rebar",
        retryable_gap_max_attempts=1,
    )
    g = FakeGerrit()
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(voter.review_and_vote(_event(), config=cfg, gerrit=g, dedup=store))
    assert res["status"] == "voted" and res["vote_value"] == -1


def test_deferred_change_stays_eligible_for_reconciler(monkeypatch, tmp_path):
    """AC5: a deferred change has no ``voted`` row and no Gerrit vote, so the backfill
    reconciler re-drives it on the next pass; the review_attempts row does NOT suppress
    the re-drive (it accumulates toward escalation instead)."""
    _patch_gap(monkeypatch, "llm-unavailable")
    cfg = _cfg(tmp_path)
    store = DedupStore(cfg.dedup_db_path)
    g = ReconcileGerrit(
        events=[_events_log_event("rebar~main~Igap", "rev-gap", number=11, created_on=2000)],
    )

    asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    assert g.votes == []  # deferred, not voted
    assert store.already_voted("rebar~main~Igap", "rev-gap") is False
    assert store.attempt_count("rebar~main~Igap", "rev-gap") == 1

    # Next pass: still vote-less → re-driven (the attempts row did not suppress it).
    asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    assert store.attempt_count("rebar~main~Igap", "rev-gap") == 2


# ── cursor low-water mark (bug 9f63) ─────────────────────────────────────────
#
# THE DEFECT. ``reconcile_once`` wrote its cursor to ``newest`` — the max event time over
# the WHOLE fetched window — unconditionally at the end of every pass, with no low-water
# mark for candidates it had failed to vote. Because the events-log ``?t1=`` window is an
# inclusive SERVER-SIDE lower bound, any candidate the pass abandoned (check-error,
# review-timeout, or a voter ``deferred``/``error`` return) fell outside every subsequent
# window — permanently. Since Gerrit's ``webhooks`` plugin is at-MOST-once and the
# receiver 202-ACKs into an in-memory queue a container recreation discards, the
# reconciler is the ONLY recovery path, so the change simply never got a vote until a
# human minted a fresh event (a ``rerun-llm-review`` comment or a no-op re-push).
#
# This contradicted three written contracts: ``reconcile.py``'s module docstring ("This
# poller closes that loop"), its in-loop comment ("is retried next pass"), and
# ``docs/adr/0009-review-bot-pipe.md`` ("the reconciler is what recovers a dropped
# webhook").
#
# It shipped because ``ReconcileGerrit.list_events`` recorded ``since`` and ignored it,
# and because every guarding test used a SINGLE-event window — where ``newest`` IS the
# failed candidate, so the inclusive re-fetch masked the bug. These tests use a
# MULTI-event window with the failure on a NON-newest event, which is the only shape that
# can express the defect.


def test_reconcile_cursor_holds_back_an_abandoned_non_newest_candidate(monkeypatch, tmp_path):
    """Bug 9f63 — the regression oracle.

    A window holds an OLD candidate whose review times out, a NEWER candidate that votes,
    and later unrelated chatter that drags ``newest`` forward. The abandoned candidate
    must still be inside the NEXT pass's window and must be re-driven, per
    ``reconcile.py``'s "is retried next pass" contract. Pre-fix the cursor jumped to the
    chatter's timestamp and the candidate was never seen again (``scanned`` fell to 0).
    """
    cfg = _cfg(tmp_path)
    # Bound BOTH sides of the abandon path. The timeout is patched tiny so the pass gives
    # up fast, and the fake review's own wait is short too — so if this patch ever failed
    # to bind (a refactor moving how reconcile resolves the timeout), the test fails in
    # under a second instead of blocking on the 1200s production default and wedging the
    # whole xdist worker.
    monkeypatch.setattr(reconcile, "review_timeout_seconds", lambda: 0.05)

    stale = _events_log_event("rebar~main~Istale", "rev-stale", number=91, created_on=1000)
    fresh = _events_log_event("rebar~main~Ifresh", "rev-fresh", number=92, created_on=2000)
    chatter = {
        "type": "comment-added",
        "eventCreatedOn": 3000,
        "change": {"id": "rebar~main~Ichat", "number": 93, "project": "rebar"},
        "patchSet": {},  # no revision/ref → not a candidate, but it DOES move ``newest``
    }
    g = ReconcileGerrit(events=[stale, fresh, chatter])
    store = DedupStore(cfg.dedup_db_path)

    driven: list[str] = []
    real = reconcile._voter.review_and_vote

    async def _review(event, **kw):
        change_id = event["change"]["id"]
        driven.append(change_id)
        if change_id == "rebar~main~Istale":
            await asyncio.sleep(2)  # outlives the 0.05s bound → wait_for cancels it
        return {"status": "voted", "change_id": change_id}

    monkeypatch.setattr(reconcile._voter, "review_and_vote", _review)
    try:
        first = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
        assert driven == ["rebar~main~Istale", "rebar~main~Ifresh"]
        assert first["reviewed"] == 1  # only the fresh one actually voted

        driven.clear()
        second = asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    finally:
        monkeypatch.setattr(reconcile._voter, "review_and_vote", real)

    # THE CONTRACT: the abandoned candidate is re-driven on the next pass.
    assert "rebar~main~Istale" in driven, (
        "the abandoned candidate fell outside the next window — the cursor advanced past "
        "an event the pass never voted, so backfill can never recover it"
    )
    assert second["scanned"] >= 1

    # And the cursor did NOT run ahead of the abandoned candidate's event time.
    cursor = (tmp_path / "reconcile_cursor").read_text(encoding="utf-8").strip()
    assert cursor <= reconcile._to_t1(1000)


def test_reconcile_cursor_advances_past_a_terminal_outcome(monkeypatch, tmp_path):
    """The hold-back is for RETRYABLE outcomes only.

    A change that merged/abandoned mid-review returns ``skipped``/``post_vote_closed``
    (voter.py) — terminal and unvotable. Re-driving it forever would 409 (bug c943) and
    would pin the fetch window open, so the cursor must advance past it exactly as it
    does for a ``voted`` candidate. This is the negative control for the test above.
    """
    cfg = _cfg(tmp_path)

    closed = _events_log_event("rebar~main~Iclosed", "rev-closed", number=94, created_on=1000)
    chatter = _events_log_event("rebar~main~Ilater", "rev-later", number=95, created_on=3000)
    g = ReconcileGerrit(events=[closed, chatter])
    store = DedupStore(cfg.dedup_db_path)

    real = reconcile._voter.review_and_vote

    async def _review(event, **kw):
        change_id = event["change"]["id"]
        if change_id == "rebar~main~Iclosed":
            return {"status": "skipped", "change_id": change_id, "stage": "post_vote_closed"}
        return {"status": "voted", "change_id": change_id}

    monkeypatch.setattr(reconcile._voter, "review_and_vote", _review)
    try:
        asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    finally:
        monkeypatch.setattr(reconcile._voter, "review_and_vote", real)

    cursor = (tmp_path / "reconcile_cursor").read_text(encoding="utf-8").strip()
    assert cursor == reconcile._to_t1(3000), (
        "a terminal skip must not pin the cursor — only retryable outcomes hold it back"
    )


def test_reconcile_holdback_is_bounded_so_a_poison_pill_cannot_pin_the_window(
    monkeypatch, tmp_path
):
    """A candidate that fails on EVERY pass must not hold the cursor back forever.

    Without a ceiling the fetch window grows without bound and the same doomed change is
    re-driven every 5 minutes in silence. Past ``RECONCILE_MAX_HOLDBACK_SECONDS`` the
    cursor advances and the change is surfaced as ``holdback_expired``.
    """
    cfg = _cfg(tmp_path)
    cfg = dataclasses.replace(cfg, reconcile_max_holdback_seconds=100)

    doomed = _events_log_event("rebar~main~Idoom", "rev-doom", number=96, created_on=1000)
    recent = _events_log_event("rebar~main~Inow", "rev-now", number=97, created_on=5000)
    g = ReconcileGerrit(events=[doomed, recent])
    store = DedupStore(cfg.dedup_db_path)

    real = reconcile._voter.review_and_vote

    async def _review(event, **kw):
        change_id = event["change"]["id"]
        if change_id == "rebar~main~Idoom":
            return {"status": "error", "change_id": change_id, "stage": "review_setup"}
        return {"status": "voted", "change_id": change_id}

    monkeypatch.setattr(reconcile._voter, "review_and_vote", _review)
    try:
        asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    finally:
        monkeypatch.setattr(reconcile._voter, "review_and_vote", real)

    # newest=5000, ceiling=100 → the cursor may not be held back before 4900.
    cursor = (tmp_path / "reconcile_cursor").read_text(encoding="utf-8").strip()
    assert cursor >= reconcile._to_t1(4900), (
        "a permanently-failing candidate pinned the cursor past the hold-back ceiling"
    )


def test_reconcile_done_reports_the_carried_backlog(monkeypatch, tmp_path, caplog):
    """AC: backfill's carried backlog is OBSERVABLE.

    ``reconcile_done`` reported only ``scanned``/``reviewed``, so "backfill is carrying N
    stalled changes" was invisible — the ambiguity that let an agent read a queued review
    as an outage. The pass must report how many candidates it left un-voted.
    """
    cfg = _cfg(tmp_path)

    stuck = _events_log_event("rebar~main~Istuck", "rev-stuck", number=98, created_on=1000)
    ok = _events_log_event("rebar~main~Iok", "rev-ok", number=99, created_on=2000)
    g = ReconcileGerrit(events=[stuck, ok])
    store = DedupStore(cfg.dedup_db_path)

    real = reconcile._voter.review_and_vote

    async def _review(event, **kw):
        change_id = event["change"]["id"]
        if change_id == "rebar~main~Istuck":
            return {"status": "deferred", "change_id": change_id}
        return {"status": "voted", "change_id": change_id}

    monkeypatch.setattr(reconcile._voter, "review_and_vote", _review)
    try:
        with caplog.at_level(logging.INFO, logger="rebar.review_bot.reconcile"):
            asyncio.run(reconcile.reconcile_once(config=cfg, gerrit=g, dedup=store))
    finally:
        monkeypatch.setattr(reconcile._voter, "review_and_vote", real)

    done = [
        json.loads(r.message) for r in caplog.records if '"reconcile_done"' in (r.message or "")
    ]
    assert done, "reconcile_done was not emitted"
    assert done[-1]["held_back"] == 1
    assert done[-1]["cursor"]


# ── tree↔vote binding (ticket da31-f9d1) ─────────────────────────────────────
def _one_commit_repo(path, filename="f.txt") -> str:
    """A real one-commit git repo at ``path``; returns its full HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / filename).write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            filename,
        ],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


class CloningGerrit(FakeGerrit):
    """A ``FakeGerrit`` whose ``clone_change_ref`` materializes a REAL checkout at ``dest``.

    It reproduces the shape of the production clone: the change tree is checked out FIRST, and
    a second fetch (the tickets branch, which the real ``clone_change_ref`` pulls from the
    mirror) then lands on top — so ``dest``'s ``FETCH_HEAD`` points at the TICKETS commit while
    its ``HEAD`` stays on the change. Any tree↔vote binding that read ``FETCH_HEAD`` would
    therefore mismatch here even though nothing is wrong."""

    def __init__(self, change_repo, tickets_repo, **kwargs):
        super().__init__(**kwargs)
        self._change_repo = change_repo
        self._tickets_repo = tickets_repo

    def clone_change_ref(self, change_number, revision_ref, dest):
        shutil.copytree(str(self._change_repo), dest, dirs_exist_ok=True)
        subprocess.run(
            ["git", "-C", dest, "fetch", "-q", str(self._tickets_repo), "HEAD"],
            check=True,
            capture_output=True,
        )
        return dest


def test_voter_votes_when_the_cloned_tree_is_the_voted_revision(monkeypatch, tmp_path):
    """Happy path for the tree↔vote binding: the tree the reviewer was handed IS the revision
    the vote attaches to, so the review proceeds and the vote is cast exactly as before.

    This is the false-mismatch guard the binding must not trip: the clone leaves a divergent
    ``FETCH_HEAD`` behind (the tickets fetch), the change sha is a full 40-hex name, and the
    review must still reach Gerrit."""
    change_sha = _one_commit_repo(tmp_path / "change")
    _one_commit_repo(tmp_path / "tickets", filename="tickets.txt")
    _patch_review(monkeypatch, [])  # clean → PASS
    g = CloningGerrit(tmp_path / "change", tmp_path / "tickets")
    store = DedupStore(str(tmp_path / "v.db"))

    res = asyncio.run(
        voter.review_and_vote(
            _event(revision=change_sha), config=_cfg(tmp_path), gerrit=g, dedup=store
        )
    )

    assert res["status"] == "voted"
    assert res["vote_value"] == 1
    assert g.votes and g.votes[0][1] == change_sha and g.votes[0][2] == 1


# ── the queue must not spend a review on a superseded patchset (oozy-darkish-merganser) ──
def _drive_worker(appmod, events, cfg, timeout=10):
    """Run `_worker` over `events` until the queue drains, then cancel it."""
    import contextlib

    async def drive():
        queue: asyncio.Queue = asyncio.Queue()
        for ev in events:
            queue.put_nowait(ev)
        worker = asyncio.create_task(appmod._worker(queue, cfg))
        try:
            await queue.join()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    asyncio.run(asyncio.wait_for(drive(), timeout=timeout))


def test_worker_discards_a_superseded_revision_before_reviewing_it(monkeypatch, tmp_path):
    """A queued event whose revision is no longer current must be dropped BEFORE the review.

    The worker is serial (WORKER_COUNT = 1) and a review takes tens of minutes, so a queued
    event is routinely obsolete by the time it is dequeued: the bot clones, runs the LLM, and
    votes on a tree the author already replaced. Observed on changes 2226/2231/2232 — the bot
    voted consistently one patchset behind, e.g. PS7 uploaded 05:59 and the vote landed on PS6
    at 06:10. The cost is not just the wasted review: on ONE worker that time is stolen from
    current work, and the `-1` that lands cites findings the author already fixed, prompting a
    re-push that enqueues yet another review. The failure amplifies itself.

    Discarding is safe: the newer patchset fired its own webhook, so an event for the current
    revision is already queued behind this one.
    """
    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    reviewed: list[str] = []

    async def fake_review_and_vote(event, *, config, force=False):
        reviewed.append(event["patchSet"]["revision"])
        return {"status": "voted"}

    monkeypatch.setattr(appmod._voter, "review_and_vote", fake_review_and_vote)
    markers: list[dict] = []
    monkeypatch.setattr(appmod._voter, "_voter_error", lambda **f: markers.append(f))
    # The change has moved on to "new_rev"; the queued event still describes "old_rev".
    monkeypatch.setattr(appmod, "_current_revision", lambda event, cfg: "new_rev", raising=False)

    _drive_worker(appmod, [_event(revision="old_rev")], _cfg(tmp_path))

    assert reviewed == [], (
        "a superseded revision must be discarded BEFORE the clone/LLM work, not reviewed"
    )
    assert markers, "the discard must emit a countable marker, not vanish silently"


def test_worker_reviews_the_current_revision(monkeypatch, tmp_path):
    """The discard must not swallow live work: a current revision is reviewed normally."""
    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    reviewed: list[str] = []

    async def fake_review_and_vote(event, *, config, force=False):
        reviewed.append(event["patchSet"]["revision"])
        return {"status": "voted"}

    monkeypatch.setattr(appmod._voter, "review_and_vote", fake_review_and_vote)
    monkeypatch.setattr(appmod._voter, "_voter_error", lambda **f: None)
    monkeypatch.setattr(appmod, "_current_revision", lambda event, cfg: "cur", raising=False)

    _drive_worker(appmod, [_event(revision="cur")], _cfg(tmp_path))

    assert reviewed == ["cur"], "the current revision must still be reviewed"


def test_worker_fails_open_when_the_current_revision_cannot_be_read(monkeypatch, tmp_path):
    """A Gerrit read error must never silently swallow a review.

    Failing CLOSED here would be worse than the bug: a transient blip would drop reviews with
    no vote and no retry signal. Unknown-current means review it.
    """
    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    reviewed: list[str] = []

    async def fake_review_and_vote(event, *, config, force=False):
        reviewed.append(event["patchSet"]["revision"])
        return {"status": "voted"}

    monkeypatch.setattr(appmod._voter, "review_and_vote", fake_review_and_vote)
    monkeypatch.setattr(appmod._voter, "_voter_error", lambda **f: None)
    monkeypatch.setattr(appmod, "_current_revision", lambda event, cfg: None, raising=False)

    _drive_worker(appmod, [_event(revision="whatever")], _cfg(tmp_path))

    assert reviewed == ["whatever"], (
        "an unreadable current revision must FAIL OPEN (review it), never drop the event"
    )


def test_worker_does_not_discard_a_forced_rerun(monkeypatch, tmp_path):
    """A manual rerun is built from the current revision by construction — never drop it."""
    pytest.importorskip("fastapi")
    from rebar.review_bot import app as appmod

    reviewed: list[bool] = []

    async def fake_review_and_vote(event, *, config, force=False):
        reviewed.append(force)
        return {"status": "voted"}

    monkeypatch.setattr(appmod._voter, "review_and_vote", fake_review_and_vote)
    monkeypatch.setattr(appmod._voter, "_voter_error", lambda **f: None)
    # Even with the check reporting the event as stale, a forced rerun must run.
    monkeypatch.setattr(appmod, "_current_revision", lambda event, cfg: "other", raising=False)

    ev = _event(revision="old_rev")
    ev["_rebar_force"] = True
    _drive_worker(appmod, [ev], _cfg(tmp_path))

    assert reviewed == [True], "a forced rerun must bypass the staleness check"


# ── the review-bot clone path shares the gate-scratch refusal (bug 1ef8) ─────
#
# S1 (story aa40, change 2620) put the unreachable-scratch refusal in gate_admission(),
# which wraps plan-review and completion-verifier. The review-bot's per-review clone does
# NOT pass through it: it is a plain tempfile.TemporaryDirectory(prefix="reviewbot-")
# following TMPDIR. So on a declared-but-unmounted scratch volume the gates refused loudly
# while the review-bot kept cloning onto the root filesystem, silently — and a partial
# refusal is worse than none, because the loud half creates confidence the protection is in
# force. These tests pin the clone path onto the SAME predicate, with the SAME ADR 0069
# deferral treatment the low-disk floor already gets.


@pytest.fixture
def scratch_host(tmp_path, monkeypatch):
    """An isolated 'host', modelled on tests/unit/test_gate_scratch_volume_aa40.py.

    ``tmp_path/var`` stands in for the durable ROOT filesystem (it always exists) and
    ``tmp_path/var/gate-scratch`` is the mount point. Mounting is simulated by writing the
    proof marker inside it; unmounting, by never writing it — which is exactly what an
    unmount does to a file that lived on the volume.

    BOTH env vars are pointed at the mount point because the two consumers read two
    different names: ``REBAR_GATE_TMPDIR`` moves the snapshot store (and is what the shared
    predicate derives its markers from), while ``TMPDIR`` is what the ``reviewbot-*`` clone
    follows. That pairing is the deployed shape (infra/compose/docker-compose.yml).
    """
    from rebar import _config_sources
    from rebar.llm import gate_admission as ga

    parent = tmp_path / "var"
    base = parent / "gate-scratch"
    base.mkdir(parents=True)
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    monkeypatch.setenv("TMPDIR", str(base))
    # ``tempfile.gettempdir()`` MEMOISES its answer in ``tempfile.tempdir`` on first use, so
    # in a process that has already made a temp file the env var alone is inert and the clone
    # would keep landing on the real system temp — which would make the AC3 absence assertion
    # pass vacuously. Setting the module attribute is what actually points the clone here.
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    monkeypatch.setattr(_config_sources, "user_config_path", lambda: tmp_path / "absent.toml")

    def declare() -> None:
        (parent / ga._SCRATCH_REQUIRED_MARKER).write_text("")

    def mount() -> None:
        (base / ga._SCRATCH_MOUNTED_MARKER).write_text("")

    return SimpleNamespace(base=base, parent=parent, declare=declare, mount=mount)


class _CloneWitnessGerrit(FakeGerrit):
    """A FakeGerrit that records whether the clone ran and WHAT it put on the mount point.

    The during-call capture is not belt-and-braces: ``tempfile.TemporaryDirectory`` removes
    its tree on ``__exit__``, so a post-call listing alone would be satisfied even by a run
    that did clone onto the unmounted mount point. Recording the live listing at clone time
    is what makes the absence assertion non-vacuous.
    """

    def __init__(self, base):
        super().__init__()
        self._base = base
        self.clone_calls = 0
        self.seen_during_clone: list[str] = []

    def clone_change_ref(self, change_number, revision_ref, dest):
        self.clone_calls += 1
        self.seen_during_clone.extend(sorted(p.name for p in self._base.glob("reviewbot-*")))


def _run_review(gerrit, tmp_path, cfg=None):
    store = DedupStore(str(tmp_path / "v.db"))
    return (
        asyncio.run(
            voter.review_and_vote(
                _event(), config=cfg or _cfg(tmp_path), gerrit=gerrit, dedup=store
            )
        ),
        store,
    )


def test_unmounted_scratch_refuses_the_review_bot_clone(scratch_host, tmp_path, monkeypatch):
    """AC1: declaration present + proof absent → the clone path REFUSES, vote-lessly.

    Built from the real two-marker files rather than by monkeypatching the predicate, so it
    proves the wiring and not just the branch.
    """
    _patch_review(monkeypatch, [])
    scratch_host.declare()
    g = _CloneWitnessGerrit(scratch_host.base)

    res, _ = _run_review(g, tmp_path)

    assert res["status"] == "deferred"
    assert res["gap_reason"] == "low-disk"
    assert g.clone_calls == 0
    assert g.votes == []


def test_the_refusal_creates_no_clone_on_the_underlying_directory(
    scratch_host, tmp_path, monkeypatch
):
    """AC3: the ABSENCE assertion — nothing is created on the root filesystem.

    Modelled on S1's ``test_the_refusal_creates_no_store_on_the_underlying_directory``: the
    point is not that an error was raised but that the bytes never landed. Before the guard
    existed this test showed a ``reviewbot-*`` directory materialising on the unmounted mount
    point — on the very disk ADR 0112 provisioned the volume to protect.
    """
    _patch_review(monkeypatch, [])
    scratch_host.declare()
    g = _CloneWitnessGerrit(scratch_host.base)

    _run_review(g, tmp_path)

    assert g.seen_during_clone == [], "a clone directory was created on the unmounted path"
    assert list(scratch_host.base.glob("reviewbot-*")) == []
    assert sorted(p.name for p in scratch_host.base.iterdir()) == []


def test_unmounted_scratch_defers_rather_than_voting_minus_one(
    scratch_host, tmp_path, monkeypatch, caplog
):
    """AC2 (under budget): an ADR 0069 retryable deferral — no vote, nothing posted."""
    _patch_review(monkeypatch, [])
    scratch_host.declare()
    g = _CloneWitnessGerrit(scratch_host.base)

    with caplog.at_level(logging.WARNING, logger="rebar.review_bot.voter"):
        res, store = _run_review(g, tmp_path)

    assert res["status"] == "deferred"
    assert g.votes == []  # genuinely vote-less: no LLM-Review -1, no LLM-Review +1
    assert store.already_voted("rebar~main~Iabc", "rev1") is False
    assert store.attempt_count("rebar~main~Iabc", "rev1") == 1
    assert "REVIEW_RETRY_DEFERRED" in caplog.text


def test_unmounted_scratch_exhaustion_is_terminal_no_vote_never_minus_one(
    scratch_host, tmp_path, monkeypatch, capsys
):
    """AC2 (budget spent): still NO vote — the ADR 0069 low-disk carve-out, not the -1.

    This is the criterion the whole fix turns on. Every other retryable gap reason escalates
    to the fail-closed -1 once its budget is spent; converting an unmounted disk into a
    negative code-review verdict against an innocent change is the same category error as a
    vacuous Verified +1. Reusing the ``low-disk`` reason is what makes that structural.
    """
    _patch_review(monkeypatch, [])
    scratch_host.declare()
    cfg = dataclasses.replace(_cfg(tmp_path), retryable_gap_max_attempts=1)
    g = _CloneWitnessGerrit(scratch_host.base)

    res, store = _run_review(g, tmp_path, cfg=cfg)

    assert res["status"] == "deferred-exhausted"
    assert res["gap_reason"] == "low-disk"
    assert g.votes == []
    assert store.already_voted("rebar~main~Iabc", "rev1") is False
    assert "VOTER_ERROR" not in capsys.readouterr().err


def test_no_declaration_leaves_the_clone_path_untouched(scratch_host, tmp_path, monkeypatch):
    """AC4: the no-op case — every developer machine and CI runner.

    No declaration means no dedicated volume was ever provisioned, so the guard is off and
    the review runs exactly as before. A fix that refused here would break every contributor.
    """
    _patch_review(monkeypatch, [])
    g = _CloneWitnessGerrit(scratch_host.base)

    res, _ = _run_review(g, tmp_path)

    assert g.clone_calls == 1
    assert res["status"] != "deferred"
    assert g.votes  # a normal PASS vote was cast


def test_proof_without_declaration_also_leaves_the_clone_path_untouched(
    scratch_host, tmp_path, monkeypatch
):
    """AC4, fourth quadrant: proof present, declaration absent → today's behaviour.

    Arises when the root-side write failed or an operator removed it during a recovery. The
    declaration is the only thing that arms the check, so its absence can never REFUSE.
    """
    _patch_review(monkeypatch, [])
    scratch_host.mount()
    g = _CloneWitnessGerrit(scratch_host.base)

    res, _ = _run_review(g, tmp_path)

    assert g.clone_calls == 1
    assert res["status"] != "deferred"


def test_a_mounted_scratch_volume_admits_the_clone(scratch_host, tmp_path, monkeypatch):
    """The happy path on a provisioned host: declaration AND proof present."""
    _patch_review(monkeypatch, [])
    scratch_host.declare()
    scratch_host.mount()
    g = _CloneWitnessGerrit(scratch_host.base)

    res, _ = _run_review(g, tmp_path)

    assert g.clone_calls == 1
    assert res["status"] != "deferred"


def test_the_refusal_says_UNMOUNTED_not_merely_low_disk(scratch_host, tmp_path, monkeypatch):
    """The sub-condition an operator actually needs, asserted rather than assumed.

    Routing deliberately reuses the ``low-disk`` gap reason (ADR 0069's one carve-out from
    the fail-closed -1), so the gap reason ALONE cannot tell an operator whether the disk is
    full or the volume is gone — two conditions with different remediations. The distinct
    message and the ``scratch_unavailable``/``scratch_detail`` coverage fields are what carry
    that, and an untested message is a message that silently reverts to the low-disk wording.
    """
    from rebar.review_bot import low_disk

    scratch_host.declare()
    decision = low_disk.pre_clone_refusal(_cfg(tmp_path))

    assert decision is not None
    assert decision["gap_reason"] == "low-disk"  # routing is unchanged, on purpose
    assert "not mounted" in decision["message"]
    assert "root filesystem" in decision["message"]
    assert decision["message"].startswith(low_disk.tag_line())
    cov = decision["verdict"]["coverage"]
    assert cov["low_disk"] is True  # what adapter._coverage_gap_reason routes on
    assert cov["scratch_unavailable"] is True
    assert str(scratch_host.base) in cov["scratch_detail"]

    # And the free-space floor keeps its own, distinct wording — the two are not interchangeable.
    assert "scratch_unavailable" not in low_disk.decision()["verdict"]["coverage"]
    assert "not mounted" not in low_disk.decision()["message"]


def test_the_clone_guard_shares_the_gates_predicate_rather_than_reimplementing_it():
    """AC5: one owner, so enforcement and monitoring cannot disagree.

    Two assertions, because the risk has two shapes. First, the review-bot's helper IS the
    gate's predicate (patching the owner changes the review-bot's answer) — a copy would keep
    returning None. Second, no module under ``src/rebar/review_bot`` names either marker
    literal, so a future edit cannot fork the pair by string.
    """
    from rebar.llm import gate_admission as ga
    from rebar.review_bot import low_disk

    original = ga.scratch_unavailable_detail
    try:
        ga.scratch_unavailable_detail = lambda: "sentinel"  # type: ignore[assignment]
        assert low_disk.scratch_unavailable_detail() == "sentinel"
    finally:
        ga.scratch_unavailable_detail = original  # type: ignore[assignment]

    review_bot_dir = pathlib.Path(voter.__file__).parent
    for module in sorted(review_bot_dir.glob("*.py")):
        text = module.read_text()
        assert ga._SCRATCH_MOUNTED_MARKER not in text, module
        assert ga._SCRATCH_REQUIRED_MARKER not in text, module


def test_monitoring_reads_the_same_proof_marker_as_the_clone_guard():
    """AC5: ``observability.sh`` anchors on the constant the clone guard now shares.

    S1 established this property for the gates; a probe that watched a different marker than
    the code enforces would report a healthy volume while the review-bot refused, or the
    reverse.
    """
    from rebar.llm import gate_admission as ga

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    src = (repo_root / "infra" / "scripts" / "observability.sh").read_text()
    assert ga._SCRATCH_MOUNTED_MARKER in src
