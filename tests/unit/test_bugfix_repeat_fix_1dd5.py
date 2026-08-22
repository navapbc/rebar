"""Ticket 1dd5 — escalate a REPEAT FIX to the plan-review attestation gate.

The Gerrit bugfix-size gate (ticket ad0d B2) only asks a bug fix for a plan when its diff
clears the 150-non-test-line floor. A small fix to a file that the branch has ALREADY
bug-fixed twice in the last week is the other shape of "this is a design change wearing a
bug label" — and the backtest shows it recalls more later-culprit fixes than the floor does.

These tests pin the new predicate (`repeat_fix.repeat_fix_escalates`) against a git fixture
repo built in-test — never this repository's own history — and pin how the gate reports the
escalation. Ticket resolution is stubbed at the `bugfix_size_gate` module seam that the
predicate reaches through, exactly as the ad0d suite stubs it.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rebar.llm.code_review import bugfix_size_gate as bsg
from rebar.llm.code_review import repeat_fix as rf

pytestmark = pytest.mark.unit


# ── git fixture repo ────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "fixture-repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "fixture@example.invalid")
    _git(r, "config", "user.name", "Fixture")
    _git(r, "config", "commit.gpgsign", "false")
    return r


def _commit(repo: Path, path: str, body: str, message: str, *, days_ago: float = 0.0) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").strip()


_BUG_TICKET = "beef-0000-0000-0001"
_TASK_TICKET = "beef-0000-0000-0002"


def _stub_resolution(monkeypatch: pytest.MonkeyPatch, types: dict[str, str]) -> None:
    """Resolve a `rebar-ticket:` trailer to an id, and that id to a ticket type.

    `types` maps ticket id -> ticket_type; an id absent from it does not resolve at all.
    """

    def _resolve(message: str, repo_root=None) -> str | None:
        for line in (message or "").splitlines():
            if line.startswith("rebar-ticket:"):
                candidate = line.split(":", 1)[1].strip()
                return candidate if candidate in types else None
        return None

    monkeypatch.setattr(bsg, "ticket_for_commit_message", _resolve)
    monkeypatch.setattr(
        bsg,
        "_load_ticket_state",
        lambda tid, repo_root=None: {"ticket_id": tid, "ticket_type": types[tid]},
    )


def _bug_msg(n: int, ticket: str = _BUG_TICKET) -> str:
    return f"Fix the thing {n}\n\nrebar-ticket: {ticket}\n"


# ── the predicate ────────────────────────────────────────────────────────────────────────


def test_two_prior_bug_fixes_in_the_window_escalate(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v0\n", "Seed\n\nrebar-ticket: nope\n", days_ago=30)
    a = _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=5)
    b = _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2), days_ago=2)

    hit, priors = rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo))

    assert hit is True
    assert set(priors) == {a, b}


def test_a_single_prior_bug_fix_does_not_escalate(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v0\n", "Seed\n\nrebar-ticket: nope\n", days_ago=30)
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=3)

    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo)) == (False, [])


def test_prior_bug_fixes_outside_the_window_do_not_escalate(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=9)
    _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2), days_ago=8)

    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo)) == (False, [])


def test_prior_commits_on_a_non_bug_ticket_do_not_escalate(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_TASK_TICKET: "task"})
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1, _TASK_TICKET), days_ago=4)
    _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2, _TASK_TICKET), days_ago=2)

    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo)) == (False, [])


def test_prior_commits_resolving_to_nothing_do_not_escalate(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v1\n", "Fix one\n", days_ago=4)
    _commit(repo, "src/rebar/hot.py", "v2\n", "Fix two\n", days_ago=2)

    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo)) == (False, [])


def test_merge_commits_are_not_counted_as_prior_fixes(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v0\n", "Seed\n\nrebar-ticket: nope\n", days_ago=6)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/rebar/hot.py", "main\n", _bug_msg(1), days_ago=5)
    _git(repo, "checkout", "-q", "-b", "side", base)
    _commit(repo, "src/rebar/hot.py", "side\n", _bug_msg(2), days_ago=4)
    _git(repo, "checkout", "-q", "main")
    stamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    # A conflicting merge resolved to one side: the MERGE commit also "touches" the path.
    subprocess.run(["git", "-C", str(repo), "merge", "--no-commit", "side"], capture_output=True)
    (repo / "src" / "rebar" / "hot.py").write_text("merged\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", _bug_msg(3), env=env)

    hit, priors = rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo))

    # Two branch-side fixes plus one merge: only the two non-merge fixes may be counted.
    assert hit is True
    assert len(priors) == 2
    merge_sha = _git(repo, "rev-parse", "HEAD").strip()
    assert merge_sha not in priors


def test_priors_are_counted_per_path_not_across_paths(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/a.py", "v1\n", _bug_msg(1), days_ago=3)
    _commit(repo, "src/rebar/b.py", "v1\n", _bug_msg(2), days_ago=2)

    assert rf.repeat_fix_escalates(["src/rebar/a.py", "src/rebar/b.py"], repo_root=str(repo)) == (
        False,
        [],
    )


def test_no_paths_reads_no_history(repo) -> None:
    assert rf.repeat_fix_escalates([], repo_root=str(repo)) == (False, [])


def test_a_git_failure_fails_open(tmp_path) -> None:
    assert rf.repeat_fix_escalates(
        ["src/rebar/hot.py"], repo_root=str(tmp_path / "not-a-repo")
    ) == (False, [])


def test_the_window_and_minimum_are_named_parameters() -> None:
    assert rf.REPEAT_FIX_WINDOW_DAYS == 7
    assert rf.REPEAT_FIX_MIN_PRIOR == 2


def test_at_pins_the_window_to_a_point_in_time(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=20)
    _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2), days_ago=19)
    at = (datetime.now(timezone.utc) - timedelta(days=18)).timestamp()

    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo))[0] is False
    assert rf.repeat_fix_escalates(["src/rebar/hot.py"], repo_root=str(repo), at=at)[0] is True


# ── the gate reports the escalation ──────────────────────────────────────────────────────


def _verdict() -> dict:
    return {"verdict": "PASS", "blocking": [], "advisory": []}


def _file_diff(path: str, added: int) -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}",
        f"+++ b/{path}",
        f"@@ -1,1 +1,{max(added, 1)} @@",
    ]
    lines += [f"+new {i}" for i in range(added)]
    return "\n".join(lines) + "\n"


def _arm_attestation(monkeypatch: pytest.MonkeyPatch, classification: str = "unsigned") -> None:
    monkeypatch.setattr(
        bsg,
        "classify_plan_review_attestation",
        lambda tid, repo_root=None, state=None: {"verdict": classification, "reason": "stub"},
    )


def _two_priors(repo: Path, path: str = "src/rebar/hot.py") -> None:
    """Two prior bug fixes, then a HEAD commit standing in for the change under review.

    A review checkout has the change at HEAD and its base at HEAD~1 (`assemble` diffs
    ``HEAD~1..HEAD``), so the priors the gate may count are HEAD~1 and earlier."""
    _commit(repo, path, "v1\n", _bug_msg(1), days_ago=5)
    _commit(repo, path, "v2\n", _bug_msg(2), days_ago=2)
    _commit(repo, "src/rebar/under_review.py", "wip\n", _bug_msg(3), days_ago=0)


def test_repeat_fix_escalates_an_under_floor_bug_fix(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _arm_attestation(monkeypatch)
    _two_priors(repo)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=20),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    record = verdict["coverage"]["bugfix_size_gate"]
    assert record["escalation_reason"] == "repeat-fix"
    assert len(record["repeat_fix_priors"]) == 2
    assert record["non_test_lines"] == 20
    assert verdict["verdict"] == "BLOCK"
    assert verdict["blocking"][0]["criteria"] == [bsg.CRITERION_ID]


def test_an_under_floor_repeat_fix_with_an_attestation_does_not_block(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _arm_attestation(monkeypatch, classification="certified")
    _two_priors(repo)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=20),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    assert verdict["verdict"] == "PASS"
    assert verdict["blocking"] == []
    assert verdict["coverage"]["bugfix_size_gate"]["escalation_reason"] == "repeat-fix"


def test_the_size_floor_still_escalates_on_its_own(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _arm_attestation(monkeypatch)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/cold.py", added=160),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    record = verdict["coverage"]["bugfix_size_gate"]
    assert record["escalation_reason"] == "size"
    assert record["repeat_fix_priors"] == []
    assert verdict["verdict"] == "BLOCK"


def test_both_signals_are_reported_together(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _arm_attestation(monkeypatch)
    _two_priors(repo)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=160),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    assert verdict["coverage"]["bugfix_size_gate"]["escalation_reason"] == "size+repeat-fix"


def test_an_under_floor_fix_with_no_repeat_history_is_still_exempt(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})

    def _boom(*a, **k):
        raise AssertionError("an unescalated fix must not be classified")

    monkeypatch.setattr(bsg, "classify_plan_review_attestation", _boom)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/quiet.py", added=20),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    assert verdict == _verdict()


def test_a_test_only_diff_reads_no_history_and_is_exempt(repo, monkeypatch) -> None:
    _two_priors(repo, "tests/unit/test_hot.py")

    def _boom(*a, **k):
        raise AssertionError("a test-only diff must not consult history or the store")

    monkeypatch.setattr(rf, "repeat_fix_escalates", _boom)
    monkeypatch.setattr(bsg, "repeat_fix_escalates", _boom)
    monkeypatch.setattr(bsg, "ticket_for_commit_message", _boom)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("tests/unit/test_hot.py", added=400),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    assert verdict == _verdict()


def test_a_repeat_fix_on_a_non_bug_ticket_is_ignored(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug", _TASK_TICKET: "task"})
    _arm_attestation(monkeypatch)
    _two_priors(repo)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=20),
        commit_message=_bug_msg(3, _TASK_TICKET),
        repo_root=str(repo),
    )

    assert verdict == _verdict()


def test_the_teaching_finding_names_the_prior_fixes(repo, monkeypatch) -> None:
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _arm_attestation(monkeypatch)
    _two_priors(repo)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=20),
        commit_message=_bug_msg(3),
        repo_root=str(repo),
    )

    finding = verdict["blocking"][0]["finding"]
    priors = verdict["coverage"]["bugfix_size_gate"]["repeat_fix_priors"]
    assert all(sha[:12] in finding for sha in priors)
    assert str(rf.REPEAT_FIX_WINDOW_DAYS) in finding


def test_non_test_paths_in_diff_drops_test_material() -> None:
    diff = _file_diff("src/rebar/a.py", added=2) + _file_diff("tests/unit/test_a.py", added=2)

    assert bsg.non_test_paths_in_diff(diff) == ["src/rebar/a.py"]


def test_the_change_under_review_is_not_counted_as_its_own_prior(repo, monkeypatch) -> None:
    """The gate must walk the diff's BASE, not its head.

    ``assemble`` builds the review diff as ``HEAD~1..HEAD``, so in a review checkout HEAD IS
    the change under review. Walking HEAD would let a fix with a single genuine prior count
    itself as the second one and escalate on its own existence."""
    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})

    def _boom(*a, **k):
        raise AssertionError("one genuine prior must not escalate")

    monkeypatch.setattr(bsg, "classify_plan_review_attestation", _boom)
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=4)
    # HEAD is the change under review: same path, same trailer, a bug fix.
    _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2), days_ago=0)
    verdict = _verdict()

    bsg.apply_bugfix_size_gate(
        verdict,
        diff_text=_file_diff("src/rebar/hot.py", added=20),
        commit_message=_bug_msg(2),
        repo_root=str(repo),
    )

    assert verdict == _verdict()


# ----------------------------------------------------------------------------------------
# The gate's window ANCHOR (remediation of the LLM-Review block on Gerrit 2079)
# ----------------------------------------------------------------------------------------
_DIFF = (
    "diff --git a/src/rebar/hot.py b/src/rebar/hot.py\n"
    "--- a/src/rebar/hot.py\n"
    "+++ b/src/rebar/hot.py\n"
    "@@ -1 +1,2 @@\n"
    " v2\n"
    "+v3\n"
)


def test_the_gate_honours_an_explicit_window_anchor(repo, monkeypatch) -> None:
    """The gate and the backtest must ask the SAME question of the SAME window.

    Before this, the gate left the anchor at wall-clock ``now`` while the backtest passed the
    commit's own timestamp, so replaying a historical commit scored it against TODAY's history
    — and the backtest then reported that number as the gate's recall. Two implementations of
    one rule, disagreeing silently, which is the exact defect class this epic exists to end.

    Same repository, same paths, two anchors, two verdicts: escalating when the anchor sits
    just after the priors, and not escalating from today, because by now they have aged out of
    the window. A gate that ignored the anchor would return the same verdict for both.
    """
    from rebar.llm.code_review.bugfix_size_gate import escalation_for_diff

    _stub_resolution(monkeypatch, {_BUG_TICKET: "bug"})
    _commit(repo, "src/rebar/hot.py", "v0\n", "Seed\n\nrebar-ticket: nope\n", days_ago=400)
    _commit(repo, "src/rebar/hot.py", "v1\n", _bug_msg(1), days_ago=201)
    _commit(repo, "src/rebar/hot.py", "v2\n", _bug_msg(2), days_ago=200)
    anchor = (datetime.now(timezone.utc) - timedelta(days=199)).timestamp()

    _, dated_reason, dated_priors = escalation_for_diff(
        _DIFF, repo_root=str(repo), base_ref="HEAD", at=anchor
    )
    _, today_reason, _ = escalation_for_diff(_DIFF, repo_root=str(repo), base_ref="HEAD")

    assert "repeat-fix" in dated_reason, "the anchored window should see both prior fixes"
    assert len(dated_priors) == 2
    assert "repeat-fix" not in today_reason, (
        "from today those fixes are ~200 days old and outside the window; a gate that "
        "ignored `at` would escalate here too"
    )
