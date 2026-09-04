"""Committed-store atomicity contracts for the experimental completion close.

The tests exercise real ticket repositories and real Git commits.  Only the irreducible
fault boundary is patched: signing preparation, staged-file promotion, index add, or commit.
Every failure oracle checks prior HEAD/status, all three artifact classes, index/worktree
cleanliness, and a clean retry.
"""

from __future__ import annotations

import subprocess
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import rebar
from rebar import config, signing
from rebar._commands import (
    _seam,
    completion_bundle,
    completion_delivery,
    completion_txn,
    txn,
)
from rebar._commands._seam import CommandError
from rebar._snapshot.ticket_view import (
    CodeOID,
    CompletionReadBasis,
    PinnedTicketView,
    tracker_head,
)
from rebar._store import event_append, hlc, push, push_classify, push_state, staging
from rebar.llm.gate_context import use_ticket_view
from rebar.llm.plan_review.attest import current_material_fingerprint


def _run_git(*argv: str) -> str:
    proc = subprocess.run(["git", *argv], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _git(repo: str | Path, *args: str) -> str:
    """Run git in a NON-BARE worktree, which git may discover from ``repo``."""
    return _run_git("-C", str(repo), *args)


def _bare_git(repo: str | Path, *args: str) -> str:
    """Run git against a BARE repository, which must be named explicitly.

    git refuses to *discover* a bare repository through ``-C`` when a developer
    sets ``safe.bareRepository = explicit``, failing with exit 128 before any ref
    lookup [rebar:740d-187c-53a2-4b7d].  A top-level ``--git-dir`` names it.
    """
    return _run_git("--git-dir", str(repo), *args)


def _in_progress_ticket(repo: Path, title: str = "atomic close") -> str:
    ticket = rebar.create_ticket("task", title, repo_root=str(repo))
    rebar.transition(ticket, "open", "in_progress", repo_root=str(repo))
    return ticket


def _atomic_pass(repo: Path, ticket: str, *, run_id: str = "run-atomic") -> dict[str, Any]:
    tracker = str(config.tracker_dir(str(repo)))
    code_oid = CodeOID(_git(repo, "rev-parse", "HEAD"))
    with PinnedTicketView.at_oid(tracker, tracker_head(tracker), run_id=run_id) as view:
        with use_ticket_view(view):
            state = view.show_ticket(ticket)
            assert state["status"] == "in_progress"
            material = current_material_fingerprint(ticket, repo_root=str(repo))
            view.transitive_descendants(ticket)
        basis = view.completion_basis(code_oid)
    assert material is not None
    return {
        "verdict": "PASS",
        "findings": [],
        "criteria": [],
        "ticket_id": ticket,
        "runner": "contract-runner",
        "model": "contract-model",
        "source": "attested",
        "signable": True,
        "certifiable": True,
        "verified_at_sha": code_oid.value,
        "material_fingerprint": material,
        "ticket_read_mode": "lazy_pinned",
        "completion_read_basis": basis.to_dict(),
    }


def _commit_bundle(repo: str | Path, ticket: str, result: dict[str, Any]):
    root = str(repo)
    tracker_path = config.tracker_dir(root)
    return completion_bundle.commit_completion_bundle(
        result,
        ticket,
        str(tracker_path),
        root,
        ref=str(result["verified_at_sha"]),
        env_id=_seam.env_id(tracker_path),
        author=_seam.author("Unknown"),
    )


def _commit_worker(repo: str, ticket: str, result: dict[str, Any]) -> dict[str, object]:
    """Picklable process entry point for the same-ticket publication race."""
    return dict(_commit_bundle(repo, ticket, result).atomic_close)


def _ticket_remote(repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Attach a real bare tickets remote and return it with an independent writer clone."""
    tracker = Path(config.tracker_dir(str(repo)))
    remote = tmp_path / "tickets-remote.git"
    _git(tmp_path, "init", "--bare", "--quiet", "--initial-branch=tickets", str(remote))
    remotes = _git(tracker, "remote").splitlines()
    if "origin" in remotes:
        _git(tracker, "remote", "set-url", "origin", str(remote))
    else:
        _git(tracker, "remote", "add", "origin", str(remote))
    _git(tracker, "push", "origin", "HEAD:refs/heads/tickets")
    writer = tmp_path / "remote-writer"
    _git(tmp_path, "clone", "--quiet", "-b", "tickets", str(remote), str(writer))
    _git(writer, "config", "user.name", "Remote Writer")
    _git(writer, "config", "user.email", "remote-writer@example.test")
    return remote, writer


def _commit_comment_without_push(writer: Path, ticket: str, body: str, repo_root: Path) -> str:
    data = {"body": body}
    event = {
        "timestamp": hlc.next_tick(str(writer), ticket),
        "uuid": str(uuid.uuid4()),
        "event_type": "COMMENT",
        "env_id": _seam.env_id(writer),
        "author": "Remote Writer",
        "data": data,
    }
    _seam.finalize_event(event, ticket, "COMMENT", data, writer, str(repo_root))
    event_append.stage_and_commit(str(writer), ticket, event)
    return _git(writer, "rev-parse", "HEAD")


def _append_remote_comment(writer: Path, ticket: str, body: str, repo_root: Path) -> str:
    _commit_comment_without_push(writer, ticket, body, repo_root)
    _git(writer, "push", "origin", "HEAD:refs/heads/tickets")
    return _git(writer, "rev-parse", "HEAD")


def _bundle_paths(tracker: str, ref: str = "HEAD") -> list[str]:
    return [
        line
        for line in _git(
            tracker,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            ref + "^",
            ref,
        ).splitlines()
        if line
    ]


def _assert_no_bundle_after(tracker: str, baseline: str, ticket: str) -> None:
    assert _git(tracker, "rev-parse", "HEAD") == baseline
    assert _git(tracker, "status", "--porcelain") == ""
    assert _git(tracker, "diff", "--cached", "--name-only") == ""
    names = _git(tracker, "ls-tree", "-r", "--name-only", "HEAD", ticket).splitlines()
    assert not any(name.endswith("-COMPLETION_VERDICT.json") for name in names)
    assert not any(name.endswith("-SIGNATURE.json") for name in names)


def test_success_is_one_commit_with_exactly_verdict_status_and_signature(
    rebar_repo: Path,
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket)
    tracker = str(config.tracker_dir(str(rebar_repo)))
    baseline = _git(tracker, "rev-parse", "HEAD")

    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert _git(tracker, "rev-parse", "HEAD") != baseline
    paths = _bundle_paths(tracker)
    assert len(paths) == 3
    assert sum(path.endswith("-COMPLETION_VERDICT.json") for path in paths) == 1
    assert sum(path.endswith("-STATUS.json") for path in paths) == 1
    assert sum(path.endswith("-SIGNATURE.json") for path in paths) == 1
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    certified = rebar.verify_signature(
        ticket, kind="completion-verifier", repo_root=str(rebar_repo)
    )
    assert certified["verdict"] == "certified"
    assert certified["verified_at_sha"] == result["verified_at_sha"]
    assert outcome.completion_signature["signed"] is True
    assert outcome.atomic_close["atomic_close_events"] == 3
    assert "atomic_close_lock_wait_ms" in outcome.atomic_close
    assert "atomic_close_lock_hold_ms" in outcome.atomic_close
    assert "atomic_close_prepare_ms" in outcome.atomic_close
    assert "atomic_close_receipt_validation_ms" in outcome.atomic_close
    assert "atomic_close_push_ms" in outcome.atomic_close
    assert _git(tracker, "status", "--porcelain") == ""


@pytest.mark.parametrize("fault", ["signing", "staging", "index", "commit"])
def test_each_fault_leaves_no_partial_bundle_and_a_clean_retry_succeeds(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    ticket = _in_progress_ticket(rebar_repo, title=f"fault at {fault}")
    result = _atomic_pass(rebar_repo, ticket, run_id=f"run-{fault}")
    tracker = str(config.tracker_dir(str(rebar_repo)))
    baseline = _git(tracker, "rev-parse", "HEAD")

    with monkeypatch.context() as injected:
        if fault == "signing":
            injected.setattr(
                signing,
                "_prepare_manifest_event",
                lambda *_a, **_kw: (_ for _ in ()).throw(signing.SigningError("injected")),
            )
        elif fault == "staging":
            real_promote = staging.StagedEvent.promote
            calls = 0

            def fail_second_promote(self) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected staging failure")
                real_promote(self)

            injected.setattr(staging.StagedEvent, "promote", fail_second_promote)
        elif fault == "index":
            injected.setattr(
                event_append,
                "_git_add",
                lambda *_a, **_kw: subprocess.CompletedProcess(
                    ["git", "add"], 1, stdout="", stderr="injected index failure"
                ),
            )
        else:
            injected.setattr(
                event_append,
                "_git_commit",
                lambda *_a, **_kw: subprocess.CompletedProcess(
                    ["git", "commit"], 1, stdout="", stderr="injected commit failure"
                ),
            )
            injected.setattr(event_append, "_recover_from_unmerged", lambda *_a: (False, None))
            injected.setattr(event_append, "_recover_from_invalid_object", lambda *_a: False)

        with pytest.raises(CommandError, match=r"injected|atomic rename failed|git commit failed"):
            _commit_bundle(rebar_repo, ticket, result)

    _assert_no_bundle_after(tracker, baseline, ticket)
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "in_progress"

    retry = _commit_bundle(rebar_repo, ticket, result)
    assert retry.atomic_close["idempotent"] is False
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    assert len(_bundle_paths(tracker)) == 3


def test_same_basis_retry_is_idempotent_and_creates_no_second_commit(rebar_repo: Path) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket, run_id="same-basis")
    tracker = str(config.tracker_dir(str(rebar_repo)))

    first = _commit_bundle(rebar_repo, ticket, result)
    after_first = _git(tracker, "rev-parse", "HEAD")
    second = _commit_bundle(rebar_repo, ticket, result)

    assert first.atomic_close["idempotent"] is False
    assert second.atomic_close["idempotent"] is True
    assert second.atomic_close["delivery"] == "already_present"
    assert _git(tracker, "rev-parse", "HEAD") == after_first


def test_equivalent_retry_uses_authenticated_manifest(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket, run_id="signed-basis")
    _commit_bundle(rebar_repo, ticket, result)
    tracker = str(config.tracker_dir(str(rebar_repo)))
    signed_basis = CompletionReadBasis.from_dict(result["completion_read_basis"])
    forged_basis = CompletionReadBasis(
        run_id="plaintext-forgery",
        code_oid=signed_basis.code_oid,
        tickets_oid=signed_basis.tickets_oid,
        receipt=signed_basis.receipt,
        receipt_digest=signed_basis.receipt_digest,
    )
    real_show = PinnedTicketView.show_ticket

    def show_with_tampered_mirror(self, *args, **kwargs):
        state = real_show(self, *args, **kwargs)
        record = state["attestations"]["completion-verifier"]
        record["manifest"] = [
            (
                f"completion-run:{forged_basis.run_id}"
                if step == f"completion-run:{signed_basis.run_id}"
                else step
            )
            for step in record["manifest"]
        ]
        return state

    monkeypatch.setattr(PinnedTicketView, "show_ticket", show_with_tampered_mirror)

    assert not completion_bundle._equivalent_close_at(
        tracker,
        ticket,
        forged_basis,
        tracker_head(tracker),
        repo_root=str(rebar_repo),
    )


def test_equivalent_retry_rejects_certificate_stale_after_reopen(rebar_repo: Path) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket, run_id="before-reopen")
    _commit_bundle(rebar_repo, ticket, result)
    rebar.reopen(ticket, repo_root=str(rebar_repo))
    rebar.transition(ticket, "open", "closed", repo_root=str(rebar_repo))

    with pytest.raises(txn.ConcurrencyMismatch, match="ticket material read by completion"):
        _commit_bundle(rebar_repo, ticket, result)


def test_unrelated_local_advance_revalidates_and_retries(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    unrelated = rebar.create_ticket("task", "unrelated writer", repo_root=str(rebar_repo))
    result = _atomic_pass(rebar_repo, ticket, run_id="unrelated-retry")
    real_commit = completion_txn.commit_atomic_completion_close
    calls = 0

    def advance_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            rebar.comment(unrelated, "concurrent unrelated write", repo_root=str(rebar_repo))
            raise completion_txn.TrackerHeadAdvanced("injected compare-and-set loss")
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(completion_txn, "commit_atomic_completion_close", advance_once)
    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert outcome.atomic_close["atomic_close_attempts"] == 2
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    assert rebar.show_ticket(unrelated, repo_root=str(rebar_repo))["comments"][0]["body"] == (
        "concurrent unrelated write"
    )


def test_relevant_receipt_drift_rejects_without_closing(rebar_repo: Path) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket, run_id="relevant-drift")
    tracker = str(config.tracker_dir(str(rebar_repo)))
    rebar.comment(ticket, "material changed after verification", repo_root=str(rebar_repo))
    drift_head = _git(tracker, "rev-parse", "HEAD")

    with pytest.raises(txn.ConcurrencyMismatch, match="ticket material read by completion"):
        _commit_bundle(rebar_repo, ticket, result)

    _assert_no_bundle_after(tracker, drift_head, ticket)
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "in_progress"


def test_two_processes_publish_at_most_one_equivalent_same_ticket_bundle(
    rebar_repo: Path,
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    result = _atomic_pass(rebar_repo, ticket, run_id="parallel-same-ticket")
    tracker = str(config.tracker_dir(str(rebar_repo)))
    baseline_count = int(_git(tracker, "rev-list", "--count", "HEAD"))

    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_commit_worker, str(rebar_repo), ticket, result) for _ in range(2)]
        outcomes = [future.result(timeout=60) for future in futures]

    assert int(_git(tracker, "rev-list", "--count", "HEAD")) == baseline_count + 1
    assert sorted(bool(outcome["idempotent"]) for outcome in outcomes) == [False, True]
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    assert len(_bundle_paths(tracker)) == 3


def test_relevant_remote_rejection_never_places_candidate_on_shared_head(
    rebar_repo: Path, tmp_path: Path
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    remote, writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="relevant-remote-drift")
    tracker = str(config.tracker_dir(str(rebar_repo)))
    baseline = _git(tracker, "rev-parse", "HEAD")
    remote_tip = _append_remote_comment(writer, ticket, "remote material changed", rebar_repo)

    with pytest.raises(txn.ConcurrencyMismatch, match="ticket material read by completion"):
        _commit_bundle(rebar_repo, ticket, result)

    _assert_no_bundle_after(tracker, baseline, ticket)
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "in_progress"
    assert _bare_git(remote, "rev-parse", "refs/heads/tickets") == remote_tip
    names = _bare_git(
        remote, "ls-tree", "-r", "--name-only", "refs/heads/tickets", ticket
    ).splitlines()
    assert not any(name.endswith("-COMPLETION_VERDICT.json") for name in names)
    assert not any(name.endswith("-SIGNATURE.json") for name in names)


def test_successful_push_followed_by_remote_rewrite_fails_closed(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    remote, _writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="post-push-remote-rewrite")
    tracker = str(config.tracker_dir(str(rebar_repo)))
    local_baseline = _git(tracker, "rev-parse", "HEAD")
    remote_baseline = _bare_git(remote, "rev-parse", "refs/heads/tickets")
    real_git = push._git
    rewrote = False

    def push_then_rewrite(base, *args, **kwargs):
        nonlocal rewrote
        actual = real_git(base, *args, **kwargs)
        if not rewrote and args[:2] == ("push", "origin") and actual.returncode == 0:
            _bare_git(remote, "update-ref", "refs/heads/tickets", remote_baseline)
            rewrote = True
        return actual

    monkeypatch.setattr(push, "_git", push_then_rewrite)

    with pytest.raises(txn.ConcurrencyMismatch, match="no longer contains the completion"):
        _commit_bundle(rebar_repo, ticket, result)

    assert rewrote is True
    _assert_no_bundle_after(tracker, local_baseline, ticket)
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "in_progress"
    assert _bare_git(remote, "rev-parse", "refs/heads/tickets") == remote_baseline


def test_unrelated_remote_rejection_merges_and_rebuilds_private_candidate(
    rebar_repo: Path, tmp_path: Path
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    unrelated = rebar.create_ticket("task", "remote unrelated", repo_root=str(rebar_repo))
    remote, writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="unrelated-remote-drift")
    _append_remote_comment(writer, unrelated, "safe concurrent delta", rebar_repo)

    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert outcome.atomic_close["atomic_close_attempts"] == 2
    assert outcome.atomic_close["delivery"] == "pushed"
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    assert rebar.show_ticket(unrelated, repo_root=str(rebar_repo))["comments"][0]["body"] == (
        "safe concurrent delta"
    )
    remote_names = _bare_git(
        remote, "ls-tree", "-r", "--name-only", "refs/heads/tickets", ticket
    ).splitlines()
    assert sum(name.endswith("-COMPLETION_VERDICT.json") for name in remote_names) == 1
    assert sum(name.endswith("-STATUS.json") for name in remote_names) >= 1
    assert sum(name.endswith("-SIGNATURE.json") for name in remote_names) >= 1


def test_lost_push_ack_is_confirmed_without_publishing_a_second_bundle(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    remote, _writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="ambiguous-push-ack")
    real_git = push._git
    accepted = False

    def accept_then_report_transport_failure(base, *args, **kwargs):
        nonlocal accepted
        actual = real_git(base, *args, **kwargs)
        if not accepted and args[:2] == ("push", "origin"):
            assert actual.returncode == 0
            accepted = True
            return subprocess.CompletedProcess(
                actual.args,
                1,
                stdout=actual.stdout,
                stderr="fatal: the remote end hung up unexpectedly",
            )
        return actual

    monkeypatch.setattr(push, "_git", accept_then_report_transport_failure)

    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert accepted is True
    assert outcome.atomic_close["delivery"] == "pushed_after_ambiguous_ack"
    assert outcome.atomic_close["atomic_close_push_attempts"] == 1
    remote_names = _bare_git(
        remote, "ls-tree", "-r", "--name-only", "refs/heads/tickets", ticket
    ).splitlines()
    assert sum(name.endswith("-COMPLETION_VERDICT.json") for name in remote_names) == 1
    assert sum(name.endswith("-STATUS.json") for name in remote_names) >= 1
    assert sum(name.endswith("-SIGNATURE.json") for name in remote_names) >= 1


def test_s3_multi_bundle_reuses_the_existing_heal_policy_before_retry(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    _remote, _writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="s3-heal-retry")
    real_git = push._git
    push_attempts = 0
    healed: list[tuple[str, str]] = []

    def multi_bundle_once(base, *args, **kwargs):
        nonlocal push_attempts
        if args[:2] == ("push", "origin"):
            push_attempts += 1
            if push_attempts == 1:
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    stdout="",
                    stderr='error: tickets "multiple bundles exists on server"',
                )
        return real_git(base, *args, **kwargs)

    def heal(base_path, remote, branch, remote_ref, stderr, strict):
        assert strict is True
        assert "multiple bundles" in stderr
        healed.append((remote, branch))
        return True

    monkeypatch.setattr(push, "_git", multi_bundle_once)
    monkeypatch.setattr(push_classify, "_heal_multi_bundle_or_stop", heal)

    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert healed == [("origin", "tickets")]
    assert push_attempts == 2
    assert outcome.atomic_close["delivery"] == "pushed"


def test_post_push_convergence_preserves_concurrent_local_delivery_pending(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _in_progress_ticket(rebar_repo)
    unrelated = rebar.create_ticket("task", "local concurrent", repo_root=str(rebar_repo))
    remote, _writer = _ticket_remote(rebar_repo, tmp_path)
    result = _atomic_pass(rebar_repo, ticket, run_id="local-ahead-after-push")
    tracker = Path(config.tracker_dir(str(rebar_repo)))
    real_merge = completion_delivery._merge_ref
    advanced = False

    def advance_before_merge(*args, **kwargs):
        nonlocal advanced
        if not advanced:
            advanced = True
            _commit_comment_without_push(
                tracker,
                unrelated,
                "committed locally while completion converged",
                rebar_repo,
            )
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(completion_delivery, "_merge_ref", advance_before_merge)

    outcome = _commit_bundle(rebar_repo, ticket, result)

    assert advanced is True
    assert outcome.atomic_close["delivery"] == "pushed_local_pending"
    pending = push_state.read_status(tracker)
    assert pending["state"] == "pending"
    assert pending["reason"] == "pushed-local-ahead"
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    assert rebar.show_ticket(unrelated, repo_root=str(rebar_repo))["comments"][0]["body"] == (
        "committed locally while completion converged"
    )
    remote_names = _bare_git(
        remote, "ls-tree", "-r", "--name-only", "refs/heads/tickets", unrelated
    ).splitlines()
    assert not any(name.endswith("-COMMENT.json") for name in remote_names)
