"""RED oracle for baa8 (curvaceous-powellite-foal): the voter must not cast an
LLM-Review vote on a revision that is no longer the change's CURRENT revision.

Mechanism under test (proven root cause): the review takes 15-45 min on a single
serial worker. ``app._worker`` runs the daa7 staleness guard (``_superseded_by``) ONCE,
BEFORE the review starts. ``voter._review_and_vote`` then clones, runs the multi-pass
LLM review, and calls ``post_review`` (→ ``gc.post_vote``) at the END with NO further
currency check. So a patchset that becomes superseded DURING the review is still voted
on. That vote's Gerrit comment dispatches the Verified workflow for the STALE refspec
(g2p ``recheck = verify`` substring mapping), and — because ``gerrit-verify`` concurrency
is keyed by Change-Id with ``cancel-in-progress`` — the stale run cancels the CURRENT
patchset's Verified run (ADR-0020: the current patchset must get its own fresh CI).

Authoritative intended behavior (this test encodes the contract, not the reporter's
assumption):
- daa7 (``oozy-darkish-merganser``) guard docstring: "DISCARD ... rather than spend
  15-45 minutes reviewing -- and VOTING ON -- a superseded patchset."
- ADR-0009: the bot votes on the CURRENT revision.
- ADR-0020: a stale/copied CI signal must never stand in for the current patchset's CI.

Held out from the fix subagent per /rebar-debug Phase 2 Step 5.
"""

from __future__ import annotations

import asyncio

from rebar.review_bot import voter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritError


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


class _Gerrit:
    """Minimal fake: records votes; reports a configurable CURRENT revision.

    ``current_revision`` is what ``get_change_event`` will report as the change's current
    revision. ``current_revision=None`` models a Gerrit lookup that cannot answer (fail
    open). ``raise_current`` models a lookup that raises (must also fail open)."""

    def __init__(self, *, current_revision="rev1", raise_current=False):
        self._current = current_revision
        self._raise_current = raise_current
        self.votes: list[tuple] = []

    def has_llm_review_vote(self, change_id, revision="current"):
        return False

    def clone_change_ref(self, change_number, revision_ref, dest):
        return dest

    def get_patch(self, change_id, revision="current"):
        return "diff --git a/x.py b/x.py\n+pass\n"

    def get_commit(self, change_id, revision="current"):
        return {"parents": [{"commit": "p0"}]}

    def get_change_event(self, change_id):
        if self._raise_current:
            raise GerritError("current-revision lookup failed", status=500)
        if self._current is None:
            return None
        return {
            "type": "manual-rerun",
            "change": {"id": change_id, "number": 42, "project": "rebar"},
            "patchSet": {"number": 9, "revision": self._current, "ref": "refs/changes/42/42/9"},
        }

    def post_vote(self, change_id, revision, value, message, robot_comments=None, comments=None):
        self.votes.append((change_id, revision, value, message))
        return 200


def _patch_pass(monkeypatch):
    """Stub the four-pass gate to a clean PASS and neuter the store-artifact write."""
    import rebar.llm.workflow.gate_dispatch as gd

    monkeypatch.setattr(
        gd,
        "produce_code_review_verdict",
        lambda request: {
            "verdict": "PASS",
            "blocking": [],
            "advisory": [],
            "coverage": {"llm_ran": True},
        },
        raising=True,
    )
    monkeypatch.setattr(voter, "emit_code_review_artifact", lambda *a, **k: None, raising=True)


def test_no_vote_when_revision_superseded_during_review(monkeypatch, tmp_path):
    """The mechanism: reviewed rev1, but the change's current revision is now rev2.

    The vote (and its Verified-dispatching comment) MUST be suppressed."""
    _patch_pass(monkeypatch)
    g = _Gerrit(current_revision="rev2")  # superseded during the review
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_event(revision="rev1"), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert g.votes == [], (
        "must NOT cast a vote on a superseded revision (dispatches stale Verified)"
    )
    assert res["status"] == "skipped"
    assert res["reason"] == "superseded"
    # write-on-success must NOT have recorded a vote for the stale revision
    assert store.already_voted("rebar~main~Iabc", "rev1") is False


def test_votes_when_revision_still_current(monkeypatch, tmp_path):
    """Negative control: reviewed rev1 is still the current revision → vote IS cast."""
    _patch_pass(monkeypatch)
    g = _Gerrit(current_revision="rev1")  # still current
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_event(revision="rev1"), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert res["status"] == "voted"
    assert g.votes and g.votes[0][1] == "rev1" and g.votes[0][2] == 1


def test_fails_open_and_votes_when_current_revision_unknown(monkeypatch, tmp_path):
    """Safety: a Gerrit lookup that cannot answer must NOT swallow a real review."""
    _patch_pass(monkeypatch)
    g = _Gerrit(current_revision=None)  # lookup returns None (cannot determine)
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_event(revision="rev1"), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert res["status"] == "voted"
    assert g.votes and g.votes[0][1] == "rev1"


def test_fails_open_and_votes_when_current_revision_lookup_raises(monkeypatch, tmp_path):
    """Safety: a Gerrit lookup that RAISES must also fail open (review is not lost)."""
    _patch_pass(monkeypatch)
    g = _Gerrit(raise_current=True)
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(_event(revision="rev1"), config=_cfg(tmp_path), gerrit=g, dedup=store)
    )
    assert res["status"] == "voted"
    assert g.votes and g.votes[0][1] == "rev1"


def test_forced_rerun_bypasses_staleness_and_votes(monkeypatch, tmp_path):
    """A forced /rerun is deliberately requested against the current revision; the
    staleness suppression must not drop it (AC: the authorized recheck still dispatches
    exactly one replacement run). Mirrors daa7's force-bypass of the pre-review guard."""
    _patch_pass(monkeypatch)
    g = _Gerrit(current_revision="rev2")  # would look superseded, but force bypasses
    store = DedupStore(str(tmp_path / "v.db"))
    res = asyncio.run(
        voter.review_and_vote(
            _event(revision="rev1"), config=_cfg(tmp_path), gerrit=g, dedup=store, force=True
        )
    )
    assert res["status"] == "voted"
    assert g.votes and g.votes[0][1] == "rev1"
