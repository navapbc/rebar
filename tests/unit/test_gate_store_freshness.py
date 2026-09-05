"""A gate must not certify against a ticket store it cannot trust (bug b928-3ab6-5985-417b).

The incident: under sustained contention on ``origin/tickets`` a clone spent hours neither
publishing its own writes nor adopting the remote's, and a ticket carrying a VALID
``completion-verifier`` attestation read as ``unsigned`` through it. That is not a late
answer, it is a WRONG one — and the gate then mints or withholds an operation certificate
on the strength of it.

The decisive tests here are the ones where a gate reads a DELIBERATELY STALE store and
REFUSES. A happy-path test proves nothing about this bug: the failure mode is precisely
that the gate answered confidently from a store that was not current.

The inverse guard matters just as much. Operator decision B4 (``vapoury-attack-lamb``,
pinned in ``docs/concurrency.md``) makes a failed tickets-branch push a SIGNAL and never an
exception. Nothing here may leak into the ordinary write path — see
``test_a_stale_store_does_not_make_an_ordinary_write_raise``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._store import freshness, push_state

pytestmark = pytest.mark.unit


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A tracker level with its origin's ``tickets`` branch: the healthy baseline."""
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    init_bare_remote(origin)
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    _git(tracker, "remote", "add", "origin", str(origin))
    (tracker / "seed.json").write_text("{}\n")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    return tracker


def _advance_remote_only(tracker: Path) -> None:
    """Publish a commit from a SECOND clone, then fetch it — leaving ``tracker`` behind.

    This is the incident shape exactly: an event written elsewhere is in this clone's git
    dir (it fetched during push recovery) but is NOT in the view its reducer reads.
    """
    origin = _git(tracker, "remote", "get-url", "origin").stdout.strip()
    other = tracker.parent / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", origin, str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "o@e.com")
    _git(other, "config", "user.name", "O")
    (other / "elsewhere.json").write_text('{"body": "written on another machine"}\n')
    _git(other, "add", "elsewhere.json")
    _git(other, "commit", "-q", "-m", "ticket: SIGNATURE completion-verifier")
    _git(other, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")


# ── the staleness definition ───────────────────────────────────────────────────────────


def test_a_level_store_is_fresh(store: Path) -> None:
    """The baseline: no marker, level with the remote-tracking ref, so gates proceed."""
    result = freshness.store_freshness(str(store))
    assert result["fresh"] is True, f"a healthy store was reported stale: {result}"
    assert result["verdict"] == "fresh"


def test_a_store_behind_the_shared_store_is_stale(store: Path) -> None:
    """THE incident signal: events exist that this clone's reducer cannot see.

    ``fsck`` deliberately stays silent on 'merely behind' because a WRITER ff-adopts on
    its next push. A gate is a pure READER and never triggers that adoption, so for
    certification this is the sharpest signal there is.
    """
    _advance_remote_only(store)
    result = freshness.store_freshness(str(store))
    assert result["fresh"] is False, (
        "a store that is provably behind the shared store was reported fresh; a gate "
        f"would certify against a view missing ticket events. Got: {result}"
    )
    assert result["verdict"] == "behind"
    assert result["behind"] == 1, f"the behind-count was not reported: {result}"


def test_a_diverged_store_is_stale(store: Path) -> None:
    """Neither history contains the other: this clone can neither see nor publish.

    ``fsck`` already counts this as an integrity issue; a gate must never certify through it.
    """
    _advance_remote_only(store)
    (store / "local.json").write_text("{}\n")
    _git(store, "add", "local.json")
    _git(store, "commit", "-q", "-m", "ticket: local, written while behind")
    result = freshness.store_freshness(str(store))
    assert result["fresh"] is False and result["verdict"] == "diverged", (
        f"a diverged store was not reported as such: {result}"
    )
    assert "DIVERGED" in result["reason"], f"the refusal does not name divergence: {result}"


def test_an_outstanding_delivery_failure_is_stale(store: Path) -> None:
    """The durable push-pending marker: this clone holds events nobody else has."""
    push_state.record_failure(
        str(store), "final-push-rejected", "! [rejected] (fetch first)", "origin/tickets"
    )
    result = freshness.store_freshness(str(store))
    assert result["fresh"] is False, f"an outstanding delivery failure read as fresh: {result}"
    assert result["verdict"] == "push-pending"
    assert "final-push-rejected" in result["reason"]


def test_a_store_merely_AHEAD_is_not_reported_behind(store: Path) -> None:
    """Being ahead is not a stale READ: this clone has every event the remote has."""
    (store / "local.json").write_text("{}\n")
    _git(store, "add", "local.json")
    _git(store, "commit", "-q", "-m", "ticket: local")
    assert freshness.store_freshness(str(store))["verdict"] == "fresh"


def test_the_probe_fails_OPEN_on_its_own_error(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken diagnostic must never convince a healthy gate that its store is broken."""

    def _boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(push_state, "read_status", _boom)
    result = freshness.store_freshness(str(store))
    assert result["fresh"] is True, f"a failed probe blocked a gate: {result}"


# ── the gate read paths ────────────────────────────────────────────────────────────────


def _stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every freshness probe to report the incident condition."""
    monkeypatch.setattr(
        freshness,
        "store_freshness",
        lambda *_a, **_k: {
            "fresh": False,
            "verdict": "behind",
            "reason": "the local ticket store is 7 commit(s) BEHIND the shared store",
            "unpushed": None,
            "behind": 7,
        },
    )


def test_the_claim_gate_refuses_on_a_stale_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """DECISIVE: a certified, valid attestation still does not authorise a claim here.

    The signature read is stubbed CERTIFIED and validity stubbed VALID, so the only thing
    that can produce a refusal is the freshness assertion — and it must run BEFORE the
    attestation is consulted, because on the real stale store that read is the thing
    returning the wrong answer.
    """
    from rebar import signing
    from rebar.llm.plan_review import attest, attest_gate

    monkeypatch.setattr(
        signing, "verify_signature", lambda *_a, **_k: {"verified": True, "verdict": "certified"}
    )
    monkeypatch.setattr(attest, "compute_validity", lambda *_a, **_k: {"valid": True})
    fresh = attest_gate.claim_gate_check("b928-3ab6-5985-417b")
    assert fresh["ok"] is True, f"the stubbed happy path did not pass: {fresh}"

    _stale(monkeypatch)
    check = attest_gate.claim_gate_check("b928-3ab6-5985-417b")
    assert check["ok"] is False, (
        "the claim gate certified against a store that is not current — the exact "
        f"failure mode of b928. Got: {check}"
    )
    assert check["verdict"] == freshness.STALE_VERDICT
    assert "BEHIND" in check["reason"]


def test_plan_review_status_reports_the_stale_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read-side surface: ``review-plan --status`` answers what a claim would decide."""
    from rebar.llm.plan_review import attest_gate

    _stale(monkeypatch)
    status = attest_gate.plan_review_status("b928-3ab6-5985-417b")
    assert status["ok"] is False and status["verdict"] == freshness.STALE_VERDICT, (
        f"the currency query hid a stale store: {status}"
    )


def test_the_plan_review_close_gate_refuses_on_a_stale_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same harm at close: a stale store makes a valid attestation read as absent."""
    from rebar._commands import gates

    monkeypatch.setattr(gates, "gate_enabled", lambda *_a, **_k: gates.GateState.ENABLED)
    _stale(monkeypatch)
    check = gates.close_plan_review_gate_check("b928-3ab6-5985-417b", {"ticket_type": "story"})
    assert check["ok"] is False, f"the close gate certified against a stale store: {check}"
    assert check["verdict"] == freshness.STALE_VERDICT
    assert check["gate_ran"] is True


def test_the_completion_close_gate_refuses_before_the_billable_verifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The completion gate must refuse WITHOUT spending an LLM run on a stale view."""
    from rebar._commands import close_precheck
    from rebar._commands._seam import CommandError

    monkeypatch.setattr(close_precheck, "_gate_skip_expectation", lambda *_a, **_k: None)
    _stale(monkeypatch)
    with pytest.raises(CommandError) as excinfo:
        close_precheck._completion_precheck(
            "b928-3ab6-5985-417b", "story", str(tmp_path), None, reason="", force_close=""
        )
    assert freshness.STALE_VERDICT in excinfo.value.message, (
        f"the refusal does not name the condition: {excinfo.value.message}"
    )
    assert "rebar fsck" in excinfo.value.message, "the refusal is not actionable"


# ── operator decision B4: the ordinary write path still SIGNALS, never raises ───────────


def test_a_stale_store_does_not_make_an_ordinary_write_raise(store: Path) -> None:
    """B4 non-regression: freshness lives in the GATE read paths, never in the write path.

    ``push_tickets_branch`` ALWAYS returns ``None`` (``docs/concurrency.md``). A gate that
    learned to refuse must not have taught the write path to raise — the rejected remedy
    was blanket write-refusal, which converts a degraded system into an outage.
    """
    from rebar._store import push

    _advance_remote_only(store)
    push_state.record_failure(str(store), "final-push-rejected", "detail", "origin/tickets")
    assert not freshness.store_freshness(str(store))["fresh"], "fixture is not stale"
    assert push.push_tickets_branch(str(store)) is None
