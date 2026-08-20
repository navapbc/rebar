"""Link-backed administrative dispositions and the plan-review CLOSE gate (bug 69b9).

THE DEFECT. ``gates.close_plan_review_gate_check`` could not see the close disposition at
all — it took no ``close_class`` parameter, so a story closed as ``superseded`` against a
live ``supersedes`` link still had to produce a plan-review attestation. It cannot: a
genuinely superseded plan describes work that already landed elsewhere, so ``review-plan``
correctly BLOCKs it, and the more truly superseded the ticket is the more certainly so. The
only exit was ``--force``, which records no signature — so every superseded ticket degraded
the audit trail for a disposition that is administrative and evidence-backed.

Its sibling close gate already honours exactly this evidence
(``close_precheck._has_live_replacement_link``), so the asymmetry was an omission, not a
policy: one gate reads the live replacement link, the other was never handed it.

THE FIX asserted here. The gate takes keyword-only ``close_class`` / ``tracker`` (defaults
keep every existing caller unchanged) and exempts a close ONLY when BOTH hold: the class is
a LINK-BACKED administrative disposition (``superseded`` / ``duplicate`` — never the
reason-required ``obsolete`` / ``wontfix``, which carry no verifiable evidence) AND the
existing predicate confirms a live replacement link. The exemption reports a DISTINCT
``disposition`` verdict so the audit trail separates it from a type ``exempt`` and from an
unaudited ``--force`` bypass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import gates, transition
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _arm_plan_review_close_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable ONLY the plan-review close gate; every other gate stays off."""
    monkeypatch.setattr(
        gates,
        "gate_enabled",
        lambda _root, name, **_k: name == "require_plan_review_for_close",
    )


def _make(repo: Path, ticket_type: str, title: str, *, claim: bool = True) -> str:
    tid = rebar.create_ticket(ticket_type, title, repo_root=str(repo))
    if claim:
        rebar.claim(tid, repo_root=str(repo))
    return tid


def _close(tid: str, *flags: str) -> int:
    return transition.transition_cli([tid, "in_progress", "closed", *flags])


def _state(repo: Path, tid: str) -> dict:
    return reduce_ticket(str(repo / ".tickets-tracker" / tid)) or {}


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


# --- AC4: the ticket's six-step reproduction, end to end ----------------------------------


def test_six_step_reproduction_superseded_story_now_closes(store, monkeypatch):
    """Steps 1-4 of the report: step 4 must now SUCCEED without --force."""
    _arm_plan_review_close_gate(monkeypatch)
    # 1. Stories A and B; B carries the plan A will absorb.
    story_a = _make(store, "story", "A absorbs the work", claim=False)
    story_b = _make(store, "story", "B planned it first")
    # 2-3. A landed it; record the evidence for the administrative disposition.
    rebar.link(story_a, story_b, "supersedes", repo_root=str(store))

    # 4. Previously: "plan-review close gate: unsigned". Now it closes.
    assert _close(story_b, "--class=superseded") == 0

    state = _state(store, story_b)
    assert state["status"] == "closed"
    assert state["close_class"] == "superseded"
    # 5-6 are moot: no --force, so no unaudited bypass was recorded.
    assert "force_close_reason" not in state


def test_duplicate_task_with_outbound_link_closes_under_the_plan_review_gate(store, monkeypatch):
    _arm_plan_review_close_gate(monkeypatch)
    canonical = _make(store, "task", "canonical", claim=False)
    tid = _make(store, "task", "the copy")
    rebar.link(tid, canonical, "duplicates", repo_root=str(store))

    assert _close(tid, "--class=duplicate") == 0
    assert _state(store, tid)["status"] == "closed"


# --- AC2: the anti-bypass boundary -- no live link means NO exemption ---------------------


def test_superseded_without_any_link_is_still_refused(store, monkeypatch):
    """The claimed evidence is absent, so the attestation is still required."""
    _arm_plan_review_close_gate(monkeypatch)
    tid = _make(store, "story", "claims superseded, links nothing")

    check = gates.close_plan_review_gate_check(
        tid,
        _state(store, tid),
        repo_root=str(store),
        close_class="superseded",
        tracker=_tracker(store),
    )

    assert check["ok"] is False
    assert check["verdict"] != "disposition"


def test_superseded_naming_a_retired_replacement_is_still_refused(store, monkeypatch):
    """A link whose target is not a live ticket is not evidence."""
    _arm_plan_review_close_gate(monkeypatch)
    tid = _make(store, "story", "links a ghost")

    check = gates.close_plan_review_gate_check(
        tid,
        _state(store, tid),
        repo_root=str(store),
        close_class="superseded",
        tracker=str(store / "no-such-tracker"),
    )

    assert check["ok"] is False
    assert check["verdict"] != "disposition"


def test_plain_close_with_no_disposition_is_unaffected(store, monkeypatch):
    _arm_plan_review_close_gate(monkeypatch)
    tid = _make(store, "story", "ordinary work")

    check = gates.close_plan_review_gate_check(tid, _state(store, tid), repo_root=str(store))

    assert check["ok"] is False
    assert check["verdict"] != "disposition"


# --- AC-scope: reason-required classes are DELIBERATELY excluded --------------------------


@pytest.mark.parametrize("reason_class", ["obsolete", "wontfix"])
def test_reason_required_classes_never_exempt_even_with_a_live_link(
    store, monkeypatch, reason_class
):
    """obsolete/wontfix are reason-required, not link-backed: no verifiable evidence."""
    _arm_plan_review_close_gate(monkeypatch)
    replacement = _make(store, "story", "a live sibling", claim=False)
    tid = _make(store, "story", "claims obsolescence")
    rebar.link(replacement, tid, "supersedes", repo_root=str(store))

    check = gates.close_plan_review_gate_check(
        tid,
        _state(store, tid),
        repo_root=str(store),
        close_class=reason_class,
        tracker=_tracker(store),
    )

    assert check["ok"] is False
    assert check["verdict"] != "disposition"


# --- AC3: the verdict is distinguishable in the audit trail -------------------------------


def test_disposition_verdict_is_distinct_from_exempt_and_from_force(store, monkeypatch):
    _arm_plan_review_close_gate(monkeypatch)
    replacement = _make(store, "story", "the replacement", claim=False)
    tid = _make(store, "story", "superseded by it")
    rebar.link(replacement, tid, "supersedes", repo_root=str(store))

    disposition = gates.close_plan_review_gate_check(
        tid,
        _state(store, tid),
        repo_root=str(store),
        close_class="superseded",
        tracker=_tracker(store),
    )
    assert disposition["ok"] is True
    assert disposition["verdict"] == "disposition"

    # Not the ticket-TYPE exemption: a bug reports `exempt`, and does so with no class at all.
    bug = _make(store, "bug", "a bug", claim=False)
    type_exempt = gates.close_plan_review_gate_check(bug, _state(store, bug), repo_root=str(store))
    assert type_exempt["verdict"] == "exempt"
    assert disposition["verdict"] != type_exempt["verdict"]

    # Not a --force bypass: the disposition close records no force reason, and force records
    # one while the gate is never consulted at all.
    assert _close(tid, "--class=superseded") == 0
    assert "force_close_reason" not in _state(store, tid)

    forced = _make(store, "story", "forced through")
    assert _close(forced, "--force=operator judgement") == 0
    assert _state(store, forced)["force_close_reason"] == "operator judgement"


# --- backward compatibility: the defaults leave every existing caller unchanged -----------


def test_signature_defaults_keep_positional_callers_working(store, monkeypatch):
    _arm_plan_review_close_gate(monkeypatch)
    tid = _make(store, "story", "no keywords passed")

    check = gates.close_plan_review_gate_check(tid, _state(store, tid), repo_root=str(store))

    assert set(check) >= {"ok", "verdict", "reason"}
