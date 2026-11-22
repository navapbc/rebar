"""Pre-LLM review steps log their start and have their own bound (bug 688f-060e-c96c-4484).

On 2026-11-21 reviews stalled silently for the full 1200 s review timeout while the snapshot
build waited on the snapshot store's accounting lock. These tests pin the class fix: every step
before the LLM call writes a ``review_step`` line before it starts, a stuck step fails the
review as a retryable error within its own bound, and an abandoned step never runs git against
a deleted scratch folder.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from rebar.review_bot import adapter, review_steps, voter
from rebar.review_bot.config import DEFAULT_REVIEW_TIMEOUT_SECONDS, ReceiverConfig
from rebar.review_bot.dedup import DedupStore

IDS = ("Iabc", "rev1")


def _step_lines(caplog) -> list[dict]:
    out = []
    for rec in caplog.records:
        try:
            body = json.loads(rec.getMessage())
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("event") == "review_step":
            out.append(body)
    return out


def test_step_bound_is_well_under_the_review_timeout(monkeypatch):
    monkeypatch.delenv("REVIEW_TIMEOUT_SECONDS", raising=False)
    assert review_steps.step_timeout_seconds() == DEFAULT_REVIEW_TIMEOUT_SECONDS / 4
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "80")
    assert review_steps.step_timeout_seconds() == 20


def test_a_stuck_step_logs_its_start_then_times_out_within_its_bound(monkeypatch, caplog):
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "0.8")  # step bound 0.2 s
    release = threading.Event()
    caplog.set_level(logging.INFO, logger="rebar.review_bot.voter")

    async def go():
        try:
            return await review_steps.run_step("clone", IDS, release.wait, 30)
        finally:
            release.set()  # let the abandoned thread end before the loop shuts down

    with pytest.raises(review_steps.ReviewStepTimeout) as info:
        asyncio.run(go())
    assert info.value.step == "clone"
    assert info.value.timeout == pytest.approx(0.2)
    lines = _step_lines(caplog)
    assert [(x["step"], x["change_id"], x["revision_id"]) for x in lines] == [
        ("clone", "Iabc", "rev1")
    ]


def test_scratch_folder_outlives_an_abandoned_step_thread():
    """The folder stays while the abandoned thread runs and goes when it returns."""
    started, release = threading.Event(), threading.Event()
    seen: list[bool] = []

    def step(path: str) -> None:
        started.set()
        release.wait(30)
        seen.append(Path(path).is_dir())

    async def go() -> str:
        scratch = review_steps.ReviewScratch()
        task = asyncio.ensure_future(scratch.run(step, scratch.path))
        await asyncio.to_thread(started.wait, 30)
        task.cancel()  # the review is abandoned while its step thread still runs
        with pytest.raises(asyncio.CancelledError):
            await task
        scratch.close()
        still_there = os.path.isdir(scratch.path)  # noqa: ASYNC240 — a local stat in a test
        release.set()
        for _ in range(500):  # the last thread removes the folder as it returns
            if not os.path.exists(scratch.path):  # noqa: ASYNC240 — a local stat in a test
                break
            await asyncio.sleep(0.01)
        assert still_there, "removed while the step thread still runs"
        return scratch.path

    path = asyncio.run(go())
    assert seen == [True], "the abandoned step saw its folder deleted"
    assert not Path(path).exists()


def test_no_step_starts_after_the_review_is_abandoned():
    ran: list[str] = []
    scratch = review_steps.ReviewScratch()
    scratch.close()
    assert not Path(scratch.path).exists()
    with pytest.raises(review_steps.ReviewAbandoned):
        asyncio.run(scratch.run(ran.append, "x"))
    assert ran == []


def test_each_attempt_gets_its_own_scratch_folder():
    with review_steps.ReviewScratch() as first:
        first_path = first.path
    with review_steps.ReviewScratch() as second:
        assert second.path != first_path
        assert Path(second.path).name.startswith(review_steps.SCRATCH_PREFIX)
    assert not Path(first_path).exists() and not Path(second.path).exists()


# --------------------------------------------------------------------------------------
# Through the voter
# --------------------------------------------------------------------------------------
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


class _Gerrit:
    def __init__(self, *, block_clone: threading.Event | None = None):
        self._block_clone = block_clone
        self.votes: list[tuple] = []

    def has_llm_review_vote(self, change_id, revision="current"):
        return False

    def get_commit(self, change_id, revision="current"):
        return {"parents": [{"commit": "p0"}], "message": "subject\n"}

    def clone_change_ref(self, change_number, revision_ref, dest):
        if self._block_clone is not None:
            self._block_clone.wait(30)
        return dest

    def get_patch(self, change_id, revision="current"):
        return "diff --git a/x.py b/x.py\n+pass\n"

    def get_change_event(self, change_id):
        return {"patchSet": {"revision": "rev1"}}


def test_a_hung_clone_fails_the_review_as_a_retryable_error(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "2")  # step bound 0.5 s
    errors: list[dict] = []
    monkeypatch.setattr(voter, "_voter_error", lambda **kw: errors.append(kw))
    release = threading.Event()
    gerrit = _Gerrit(block_clone=release)
    monkeypatch.setattr(voter, "post_review", lambda gc, *a, **k: gerrit.votes.append(a) or 200)
    caplog.set_level(logging.INFO, logger="rebar.review_bot.voter")

    async def go():
        try:
            return await voter.review_and_vote(
                _event(),
                config=_cfg(tmp_path),
                gerrit=gerrit,
                dedup=DedupStore(str(tmp_path / "v.db")),
            )
        finally:
            release.set()  # let the abandoned clone thread end before the loop shuts down

    result = asyncio.run(go())
    assert result["status"] == "error" and result["stage"] == "clone"
    assert gerrit.votes == []
    assert len(errors) == 1 and "review step clone timed out after 0.5s" in errors[0]["error"]
    steps = [x["step"] for x in _step_lines(caplog)]
    assert steps == ["existing_vote_check", "commit_fetch", "clone"]


def test_every_pre_llm_step_logs_its_start_in_order(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(adapter, "prepare_review_snapshot", lambda repo_root: None)
    monkeypatch.setattr(
        adapter,
        "code_review_decision",
        lambda *a, **k: {"decision": "PASS", "message": "ok", "findings": []},
    )
    monkeypatch.setattr(voter, "emit_code_review_artifact", lambda *a, **k: None)
    monkeypatch.setattr(voter, "post_review", lambda gc, *a, **k: 200)
    caplog.set_level(logging.INFO, logger="rebar.review_bot.voter")
    result = asyncio.run(
        voter.review_and_vote(
            _event(),
            config=_cfg(tmp_path),
            gerrit=_Gerrit(),
            dedup=DedupStore(str(tmp_path / "v.db")),
        )
    )
    assert result["status"] == "voted"
    assert [x["step"] for x in _step_lines(caplog)] == [
        "existing_vote_check",
        "commit_fetch",
        "clone",
        "snapshot",
        "diff_fetch",
        "llm_review",
    ]


def test_snapshot_step_resolves_the_same_handle_the_gate_resolves(monkeypatch, tmp_path):
    """The pre-step must warm exactly the snapshot the gate reads, or it would not move the
    gate's wait out of the LLM step."""
    from rebar.llm import gate_source
    from rebar.llm.workflow.gate_dispatch import CodeReviewRequest

    calls: list[dict] = []
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda **kw: calls.append(kw))
    (tmp_path / ".git").mkdir()
    adapter.prepare_review_snapshot(str(tmp_path))
    fields = CodeReviewRequest.__dataclass_fields__
    assert calls == [
        {
            "ref": fields["head"].default,
            "source": fields["source"].default,
            "repo_root": str(tmp_path),
        }
    ]


def test_snapshot_step_failure_is_left_to_the_gate(monkeypatch, tmp_path):
    from rebar.llm import gate_source

    def boom(**kw):
        raise RuntimeError("snapshot store unavailable")

    monkeypatch.setattr(gate_source, "resolve_gate_handle", boom)
    (tmp_path / ".git").mkdir()
    adapter.prepare_review_snapshot(str(tmp_path))  # logged, not raised


def test_snapshot_step_skips_a_folder_that_is_not_a_clone(monkeypatch, tmp_path):
    from rebar.llm import gate_source

    calls: list[dict] = []
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda **kw: calls.append(kw))
    adapter.prepare_review_snapshot(str(tmp_path))
    assert calls == []
