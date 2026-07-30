"""``edit --review`` and ``claim --review`` (story a114-8f96-ff2d-461d).

Both flags fuse the common "mutate, then re-run the plan review" loop into the
consuming verb (OSS precedent: cargo publish's default verify; terraform apply's
staleness-checked plan artifact):

  * ``edit <id> ... --review`` — a VALUELESS flag popped before the ``--key=value``
    field loop; after ``edit_core`` commits the EDIT, ``rebar.llm.review_plan(id,
    sign=True)`` runs and the process exits with the disposition mapping
    (0 PASS / 1 BLOCK / 2 INDETERMINATE / 11 retryable). The edit stays committed
    whatever the verdict.
  * ``claim <id> --review`` — two-stage sensing: stage 1 asks the shared
    ``gates._plan_review_gate_applies`` helper (gate enabled + type not exempt);
    stage 2 asks ``llm.claim_gate_check`` for currency. Only a stale/missing
    attestation triggers ``review_plan``; the claim core runs ONLY on a PASS.
    BLOCK / INDETERMINATE / retryable never invoke the claim core (exit 1/2/11).
    A non-applicable gate prints a notice and claims. The flag never propagates
    through the parent-first cascade.
  * Neither flag holds the store flock across the review, and a RAISING
    ``review_plan`` propagates through the standard CLI error path (edit stays
    committed; the claim is never attempted).

All review calls are stubbed (verdict dicts / raising stubs) — no LLM, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._commands import claim as claim_mod
from rebar._commands import composer, txn

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    return root


def _enable_claim_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")


def _verdict(verdict: str, **extra) -> dict:
    return {"verdict": verdict, "ticket_id": "stub", **extra}


class _ReviewStub:
    """Records review_plan calls and returns (or raises) a canned result."""

    def __init__(self, result=None, exc: Exception | None = None):
        self.result = result if result is not None else _verdict("PASS")
        self.exc = exc
        self.calls: list[str] = []

    def __call__(self, ticket_id, **kwargs):
        self.calls.append(ticket_id)
        if self.exc is not None:
            raise self.exc
        return dict(self.result, ticket_id=ticket_id)


class _GateCheckStub:
    """claim_gate_check stub: not-ok for `stale_ids` until the paired review stub
    has reviewed that id, ok otherwise."""

    def __init__(self, review: _ReviewStub, stale_ids: set[str]):
        self.review = review
        self.stale_ids = stale_ids
        self.calls: list[str] = []

    def __call__(self, ticket_id, **kwargs):
        self.calls.append(ticket_id)
        if ticket_id in self.stale_ids and ticket_id not in self.review.calls:
            return {
                "ok": False,
                "reason": "no certified plan-review attestation",
                "verdict": "unsigned",
            }
        return {"ok": True, "reason": "current", "verdict": "certified"}


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo)).get("status")


def _priority(tid: str, repo: Path):
    return rebar.show_ticket(tid, repo_root=str(repo)).get("priority")


# ---------------------------------------------------------------- edit --review


def test_bare_review_flag_does_not_consume_next_token(repo, monkeypatch):
    """`--review --priority 1` keeps BOTH: --review parses valueless (AC 1)."""
    stub = _ReviewStub(_verdict("PASS"))
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    rc = composer.edit_cli([tid, "--review", "--priority", "1"], repo_root=str(repo))
    assert rc == 0
    assert _priority(tid, repo) == 1
    assert stub.calls == [tid]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_verdict("PASS"), 0),
        (_verdict("BLOCK"), 1),
        (_verdict("INDETERMINATE"), 2),
        (_verdict("INDETERMINATE", coverage={"retryable": True}), 11),
    ],
)
def test_edit_review_disposition_exit_codes(repo, monkeypatch, result, expected):
    """edit --review maps the verdict via _disposition_exit_code(..., indeterminate_code=2)
    (ACs 2, 4) and runs the review strictly AFTER the EDIT commit."""
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    seen_at_review: dict = {}

    class _OrderStub(_ReviewStub):
        def __call__(self, ticket_id, **kwargs):
            # The EDIT must already be committed when the review starts.
            seen_at_review["priority"] = _priority(ticket_id, repo)
            return super().__call__(ticket_id, **kwargs)

    stub = _OrderStub(result)
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    rc = composer.edit_cli([tid, "--priority=3", "--review"], repo_root=str(repo))
    assert rc == expected
    assert seen_at_review["priority"] == 3  # review ran after the commit
    assert _priority(tid, repo) == 3  # a non-PASS leaves the edit committed


def test_edit_without_review_never_calls_review_plan(repo, monkeypatch):
    stub = _ReviewStub()
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    assert composer.edit_cli([tid, "--priority=1"], repo_root=str(repo)) == 0
    assert stub.calls == []


def test_edit_review_raising_stub_leaves_edit_committed(repo, monkeypatch):
    """A raising review_plan propagates via the standard CLI error path with the
    EDIT still committed (AC 9)."""
    stub = _ReviewStub(exc=RuntimeError("provider outage"))
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    with pytest.raises(RuntimeError, match="provider outage"):
        composer.edit_cli([tid, "--priority=4", "--review"], repo_root=str(repo))
    assert _priority(tid, repo) == 4


# --------------------------------------------------------------- claim --review


def test_claim_review_pass_claims(repo, monkeypatch):
    """Stale attestation + PASS review → the claim proceeds (AC 3)."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub(_verdict("PASS"))
    gate = _GateCheckStub(review, {tid})
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", gate)
    rc = claim_mod.claim_cli([tid, "--assignee", "me", "--review"], repo_root=str(repo))
    assert rc == 0
    assert review.calls == [tid]
    assert _status(tid, repo) == "in_progress"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_verdict("BLOCK"), 1),
        (_verdict("INDETERMINATE"), 2),
        (_verdict("INDETERMINATE", coverage={"retryable": True}), 11),
    ],
)
def test_claim_review_non_pass_never_invokes_claim_core(
    repo, monkeypatch, capsys, result, expected
):
    """BLOCK / INDETERMINATE / retryable-degrade: no claim, exit 1/2/11, summary
    printed (AC 3)."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub(result)
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", _GateCheckStub(review, {tid}))
    core_calls: list = []
    real_core = txn.claim_core
    monkeypatch.setattr(
        txn, "claim_core", lambda *a, **k: core_calls.append(a) or real_core(*a, **k)
    )
    capsys.readouterr()
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    captured = capsys.readouterr()
    assert rc == expected
    assert core_calls == []  # the claim core is never invoked
    assert "PLAN REVIEW" in captured.out  # the review summary is printed
    assert _status(tid, repo) == "open"


def test_claim_review_skips_review_when_attestation_current(repo, monkeypatch):
    """Stage-2 sensing: a current attestation means NO review runs; claim proceeds."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub()
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", _GateCheckStub(review, set()))
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    assert rc == 0
    assert review.calls == []
    assert _status(tid, repo) == "in_progress"


def test_claim_review_gate_disabled_prints_notice_and_claims(repo, monkeypatch, capsys):
    """Config-disabled gate: one-line notice, claim proceeds, no review (AC 5)."""
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub()
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    capsys.readouterr()
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    captured = capsys.readouterr()
    assert rc == 0
    assert review.calls == []
    assert "plan-review gate not enabled for this ticket; --review skipped" in captured.err
    assert _status(tid, repo) == "in_progress"


def test_claim_review_exempt_type_prints_notice_and_claims(repo, monkeypatch, capsys):
    """Exempt type (bug) with the gate ON: notice + claim, no review (AC 5)."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("bug", "b", repo_root=str(repo))
    review = _ReviewStub()
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    capsys.readouterr()
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    captured = capsys.readouterr()
    assert rc == 0
    assert review.calls == []
    assert "plan-review gate not enabled for this ticket; --review skipped" in captured.err
    assert _status(tid, repo) == "in_progress"


def test_claim_review_fails_closed_when_recheck_still_stale(repo, monkeypatch):
    """A PASS review whose attestation somehow did not stick: the normal precheck
    re-checks and fails closed with the standard message (AC 5 / plan §2)."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub(_verdict("PASS"))
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    # Always stale — even after the review "passed".
    monkeypatch.setattr(
        rebar.llm,
        "claim_gate_check",
        lambda ticket_id, **k: {"ok": False, "reason": "still stale", "verdict": "stale"},
    )
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    assert rc == 1
    assert review.calls == [tid]
    assert _status(tid, repo) == "open"


def test_claim_review_not_propagated_through_parent_cascade(repo, monkeypatch):
    """The cascade claims the open parent WITHOUT --review: only the requested
    child is reviewed (AC 5)."""
    _enable_claim_gate(repo)
    parent = rebar.create_ticket("epic", "p", repo_root=str(repo))
    child = rebar.create_ticket("task", "c", parent=parent, repo_root=str(repo))
    review = _ReviewStub(_verdict("PASS"))
    # Child is stale (needs the review); parent reads current so its cascaded
    # claim passes the normal precheck without any review.
    gate = _GateCheckStub(review, {child})
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", gate)
    rc = claim_mod.claim_cli([child, "--review"], repo_root=str(repo))
    assert rc == 0
    assert review.calls == [child]  # exactly one review, never the parent
    assert _status(child, repo) == "in_progress"
    assert _status(parent, repo) == "in_progress"


def test_claim_review_raising_stub_never_invokes_claim_core(repo, monkeypatch):
    """A raising review_plan propagates; the claim core is never invoked (AC 9)."""
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    review = _ReviewStub(exc=RuntimeError("config error"))
    monkeypatch.setattr(rebar.llm, "review_plan", review)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", _GateCheckStub(review, {tid}))
    core_calls: list = []
    monkeypatch.setattr(txn, "claim_core", lambda *a, **k: core_calls.append(a))
    with pytest.raises(RuntimeError, match="config error"):
        claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    assert core_calls == []
    assert _status(tid, repo) == "open"


# ------------------------------------------------------- flock is not held


class _FlockProbeStub(_ReviewStub):
    """A review stub that ACQUIRES the store write flock — succeeds only if the
    caller released the lock before invoking the review (AC 8)."""

    def __init__(self, repo: Path, result=None):
        super().__init__(result)
        self.repo = repo

    def __call__(self, ticket_id, **kwargs):
        from rebar import config as _config
        from rebar._store import lock as _lock

        tracker = str(_config.tracker_dir(str(self.repo)))
        with _lock.write_lock(tracker, timeout=1, attempts=2):
            pass
        return super().__call__(ticket_id, **kwargs)


def test_edit_review_does_not_hold_store_flock(repo, monkeypatch):
    stub = _FlockProbeStub(repo, _verdict("PASS"))
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    rc = composer.edit_cli([tid, "--priority=1", "--review"], repo_root=str(repo))
    assert rc == 0
    assert stub.calls == [tid]


def test_claim_review_does_not_hold_store_flock(repo, monkeypatch):
    _enable_claim_gate(repo)
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    stub = _FlockProbeStub(repo, _verdict("PASS"))
    monkeypatch.setattr(rebar.llm, "review_plan", stub)
    monkeypatch.setattr(rebar.llm, "claim_gate_check", _GateCheckStub(stub, {tid}))
    rc = claim_mod.claim_cli([tid, "--review"], repo_root=str(repo))
    assert rc == 0
    assert stub.calls == [tid]
    assert _status(tid, repo) == "in_progress"


# ------------------------------------------------------------------- docs/help


def test_help_sources_document_review_flag():
    """Help sources state the flag, its non-atomicity, and review-plan --status (AC 6)."""
    for name in ("edit.txt", "claim.txt"):
        text = (REPO_ROOT / "src" / "rebar" / "_cli" / "help" / name).read_text()
        assert "--review" in text, name
        assert "not atomic" in text, name
        assert "review-plan" in text and "--status" in text, name


def test_exit_codes_doc_documents_review_flag_codes():
    """docs/exit-codes.md documents the --review exit codes and states the
    flagless contracts are unchanged (AC 7)."""
    text = (REPO_ROOT / "docs" / "exit-codes.md").read_text()
    assert "--review" in text
    assert "flagless" in text
    for needle in ("edit", "claim"):
        assert needle in text
