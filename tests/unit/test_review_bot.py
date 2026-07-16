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
import logging

import pytest

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

    def post_vote(self, change_id, revision, value, message, robot_comments=None):
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
        return list(self._events)

    def has_llm_review_vote(self, change_id, revision="current"):
        self.has_vote_calls += 1
        return revision in self._voted


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
    import json as _json

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

    async def fake_reconcile_loop(*, config=None):  # never touches Gerrit
        await asyncio.Event().wait()

    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", fake_reconcile_loop)

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


def test_configure_logging_is_idempotent():
    """Configuring twice must not stack duplicate handlers (no double log lines)."""
    from rebar.review_bot.config import configure_logging

    _clear_reviewbot_log_handlers()
    configure_logging()
    configure_logging()
    installed = [
        h for h in logging.getLogger("rebar").handlers if getattr(h, "_reviewbot_handler", False)
    ]
    assert len(installed) == 1
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


def _idle_reconcile_loop(*a, **k):
    async def _loop():
        while True:
            await asyncio.sleep(3600)

    return _loop()


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
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _idle_reconcile_loop, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
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

    async def _slow_review(event, *, config, force=False):
        await asyncio.sleep(3600)

    monkeypatch.setattr(appmod._voter, "review_and_vote", _slow_review, raising=True)
    monkeypatch.setattr(appmod._reconcile, "reconcile_loop", _idle_reconcile_loop, raising=True)
    monkeypatch.setattr(
        "rebar._snapshot.start_background_janitor", lambda: (None, None), raising=False
    )
    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "0.1")
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(config=_cfg(tmp_path)))

    async def _run():
        async with appmod.lifespan(fake_app):
            fake_app.state.queue.put_nowait(_event())

    # Must return within the outer bound despite the hung review (bounded drain then cancel).
    asyncio.run(asyncio.wait_for(_run(), timeout=5))


def test_reviewbot_compose_sets_stop_grace_period():
    """docker-compose review-bot service declares a stop_grace_period so the drain has time
    before Docker escalates to SIGKILL."""
    import pathlib

    yaml = pytest.importorskip("yaml")
    root = pathlib.Path(__file__).resolve().parents[2]
    d = yaml.safe_load((root / "infra/compose/docker-compose.yml").read_text())
    assert d["services"]["review-bot"].get("stop_grace_period")


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
