"""Regression test for Bug f058: HEAD drift tolerates benign external writers.

Bug context:
  `_apply_batch` pins the `tickets` orphan-branch HEAD before its mutation
  loop and re-checks on each iteration. If HEAD advances, it raises
  HeadDriftError to abort the pass. The d822 fix (PR #425) removed the
  in-process `_file_conflict_bug_ticket` subprocess as a commit source.
  But the drift detector still aborts on EXTERNAL writers — e.g., a
  parallel Claude session running `rebar transition <id> ... closed`
  triggers an auto-compact (ticket-transition.sh) which commits
  `ticket: COMPACT <id>` to the tickets branch. The reconciler aborts
  mid-pass even though the external commit doesn't conflict with the
  in-flight mutations.

  Empirical confirmation (ADVANCED-tier historical investigator):
  drift SHAs `78392cd6→19da05f6` in field-probe-unassigned-1779984990
  matched a `ticket: COMPACT 6d43-a70d-871c-4973` commit emitted by a
  sibling Claude session — NOT a probe-created ticket.

Fix:
  Replace the unconditional raise with a tolerance check. When the
  intervening commit's subject matches benign external patterns
  (`ticket:`, `suggestion:`, `acquire lock`, `release lock`), refresh
  `head_pin` and continue. Only raise HeadDriftError if the subject
  doesn't match benign patterns (which would indicate a competing
  reconciler pass — the original concern the detector was built for).

Coverage in this file:
  * The direct-classifier tests exercise `_drift_is_benign` in isolation.
  * The behavioral tests drive the REAL `_apply_batch` mutation loop against
    a REAL `tickets`-branch git repo, injecting real HEAD-advancing commits
    between mutations (as a side effect of the mocked Jira client), and assert
    only OBSERVABLE behavior: Jira mock call counts, the written manifest JSON,
    the `abort_due_to_drift` stderr JSON's `mutations_completed`, and the
    raised HeadDriftError. Nothing here inspects source text or private names.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_drift_tolerance", APPLIER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_drift_tolerance"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture(autouse=True)
def _dso_project(monkeypatch):
    # Pin the configured project to DSO so the bug-626d cross-project guard does
    # not flag the DSO-* target keys used by the behavioral tests below.
    monkeypatch.setenv("JIRA_PROJECT", "DSO")


# ---------------------------------------------------------------------------
# Direct-classifier tests (kept): exercise `_drift_is_benign` in isolation.
# ---------------------------------------------------------------------------


def test_drift_tolerates_ticket_compact(applier):
    """The most common external drift trigger: parallel session running
    `rebar transition <id> ... closed` → auto-compact via
    ticket-transition.sh:594 → `ticket: COMPACT <id>` commit."""
    assert applier._drift_is_benign("ticket: COMPACT 6d43-a70d-871c-4973") is True


def test_drift_tolerates_ticket_status(applier):
    """Parallel session transitioning a ticket emits `ticket: STATUS`."""
    assert applier._drift_is_benign("ticket: STATUS abc-1234") is True


def test_drift_tolerates_ticket_create(applier):
    """A parallel session creating a new ticket emits `ticket: CREATE`."""
    assert applier._drift_is_benign("ticket: CREATE def-456") is True


def test_drift_tolerates_ticket_edit_comment_delete(applier):
    """All ticket-CLI write subcommands emit `ticket: <VERB>` commits."""
    assert applier._drift_is_benign("ticket: EDIT abc-1234") is True
    assert applier._drift_is_benign("ticket: COMMENT abc-1234") is True
    assert applier._drift_is_benign("ticket: DELETE abc-1234") is True
    assert applier._drift_is_benign("ticket: SNAPSHOT abc-1234") is True


def test_drift_tolerates_suggestion_record(applier):
    """The suggestion subsystem emits `suggestion: RECORD` commits."""
    assert applier._drift_is_benign("suggestion: RECORD") is True


def test_drift_tolerates_acquire_release_lock(applier):
    """Other reconciler passes acquiring/releasing their pass lock emit
    `acquire lock pass_id=...` / `release lock pass_id=...` commits.
    These are benign in the sense that they don't represent competing
    mutations to in-flight target tickets."""
    assert applier._drift_is_benign("acquire lock pass_id=xyz") is True
    assert applier._drift_is_benign("release lock pass_id=xyz") is True


def test_drift_rejects_competing_pass_record(applier):
    """A competing reconciler outbound write commits `pass_record: ...`.
    These represent actual concurrent reconciliation — the original
    intent of the drift detector. Must NOT be silenced."""
    assert applier._drift_is_benign("pass_record: 2026-05-28T16-31-54") is False


def test_drift_rejects_unknown_subjects(applier):
    """Unrecognized commit subjects (developer manual edits, hostile
    writes, etc.) must NOT be silently tolerated."""
    assert applier._drift_is_benign("feat: some manual commit") is False
    assert applier._drift_is_benign("fix: something") is False
    assert applier._drift_is_benign("") is False
    assert applier._drift_is_benign("WIP debugging") is False


# ---------------------------------------------------------------------------
# Behavioral fixtures: a real `tickets`-branch git repo + a Jira client mock
# whose outbound writes inject real HEAD-advancing commits between mutations.
# ---------------------------------------------------------------------------


def _git_env() -> dict:
    import os

    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Drift Test",
            "GIT_AUTHOR_EMAIL": "drift@example.com",
            "GIT_COMMITTER_NAME": "Drift Test",
            "GIT_COMMITTER_EMAIL": "drift@example.com",
        }
    )
    return env


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, env=_git_env())


def _init_tickets_repo(root: Path) -> None:
    """Create a real git repo with the `tickets` branch checked out and one commit."""
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "checkout", "-q", "-b", "tickets")
    _git(root, "commit", "-q", "--allow-empty", "-m", "genesis")


def _commit(root: Path, subject: str) -> None:
    """Advance the `tickets` HEAD with a real empty commit carrying *subject*.

    An empty-string subject produces a commit whose `%s` reads back as "" —
    the real-git analogue of a subject-lookup failure.
    """
    _git(root, "commit", "-q", "--allow-empty", "--allow-empty-message", "-m", subject)


def _make_committing_acli_module(root: Path, drift_subjects: list):
    """Return (mock acli module, mock client) whose each outbound write commits.

    ``drift_subjects`` is consumed one entry per outbound Jira write (create /
    update / delete), IN CALL ORDER: a str entry is committed to the tickets
    branch as a side effect of that write (advancing HEAD so the NEXT
    per-mutation drift recheck observes real drift with a real subject); a
    ``None`` entry commits nothing.
    """
    subj_iter = iter(drift_subjects)

    def _inject() -> None:
        subj = next(subj_iter, None)
        if subj is not None:
            _commit(root, subj)

    client = MagicMock()
    client.search_issues = MagicMock(return_value=[])

    def _create(_payload, *a, **k):
        _inject()
        return {"key": "DSO-1"}

    def _update(key, *a, **k):
        _inject()
        return {"key": key}

    def _delete(key, *a, **k):
        _inject()
        return None

    client.create_issue = MagicMock(side_effect=_create)
    client.update_issue = MagicMock(side_effect=_update)
    client.delete_issue = MagicMock(side_effect=_delete)

    mod = types.ModuleType("acli_committing")
    mod.AcliClient = MagicMock(return_value=client)
    return mod, client


def _make_real_head_concurrency_module() -> types.ModuleType:
    """Mock _concurrency whose snapshot_head is the REAL one (reads actual HEAD).

    Only ``rebase_retry`` is stubbed — to run the pass-record write without a
    network push — so the drift guard reads the genuine tickets-branch HEAD.
    """
    from rebar_reconciler import _concurrency as real_conc

    class _Result:
        ok = True
        event = None
        value = None

    def _stub_rebase_retry(_repo_root, write_fn, **_kwargs):
        write_fn()
        return _Result()

    mod = types.ModuleType("_concurrency_real_head")
    mod.snapshot_head = real_conc.snapshot_head  # type: ignore[attr-defined]
    mod.rebase_retry = _stub_rebase_retry  # type: ignore[attr-defined]
    return mod


def _run_batch(applier, tmp_path, mutations, drift_subjects, pass_id):
    """Drive the real _apply_batch with an injecting client. Returns (path, client)."""
    _init_tickets_repo(tmp_path)
    acli_mod, client = _make_committing_acli_module(tmp_path, drift_subjects)
    concurrency = _make_real_head_concurrency_module()
    with (
        patch.object(applier, "_load_acli", return_value=acli_mod),
        patch.object(applier, "_load_concurrency", return_value=concurrency),
    ):
        path = applier._apply_batch(mutations, pass_id, repo_root=tmp_path)
    return path, client


# ---------------------------------------------------------------------------
# Behavioral tests (a)–(g): drive the real _apply_batch loop.
# ---------------------------------------------------------------------------


def test_benign_unrelated_ticket_commit_continues(tmp_path, applier):
    """A benign external commit for an UNRELATED ticket between mutations lets
    the pass continue: both outbound updates are still issued and the manifest
    records mutation_count == 2."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]
    # mut1's write commits `ticket: STATUS OTHER-999` (an unrelated ticket).
    subjects = ["ticket: STATUS OTHER-999", None]
    path, client = _run_batch(applier, tmp_path, mutations, subjects, "pa")
    assert client.update_issue.call_count == 2
    assert json.loads(path.read_text())["mutation_count"] == 2


def test_benign_same_ticket_edit_commit_continues(tmp_path, applier):
    """A benign `ticket: EDIT` commit for the ticket being mutated between
    mutations lets the pass continue: both updates issued, manifest count 2."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]
    path, client = _run_batch(applier, tmp_path, mutations, ["ticket: EDIT 6d43-a70d", None], "pb")
    assert client.update_issue.call_count == 2
    assert json.loads(path.read_text())["mutation_count"] == 2


def test_benign_ticket_compact_delete_commit_continues(tmp_path, applier):
    """A benign `ticket: COMPACT <id>` (delete/auto-compact) commit between
    mutations — the canonical f058 trigger — lets the pass continue: both
    updates issued, manifest count 2."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]
    subjects = ["ticket: COMPACT 6d43-a70d-871c-4973", None]
    path, client = _run_batch(applier, tmp_path, mutations, subjects, "pc")
    assert client.update_issue.call_count == 2
    assert json.loads(path.read_text())["mutation_count"] == 2


def test_sequential_benign_commits_continue_to_completion(tmp_path, applier):
    """Several sequential benign external commits — one between each pair of
    mutations — let the pass run to completion: all three updates issued and
    manifest count 3."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "a"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "b"}},
        {"action": "update", "key": "DSO-12", "fields": {"summary": "c"}},
    ]
    subjects = ["ticket: STATUS DSO-10", "ticket: COMPACT DSO-11", "ticket: EDIT DSO-12"]
    path, client = _run_batch(applier, tmp_path, mutations, subjects, "pd")
    assert client.update_issue.call_count == 3
    assert json.loads(path.read_text())["mutation_count"] == 3


def test_competing_pass_record_commit_aborts_before_next_jira_call(tmp_path, applier):
    """A COMPETING non-benign `pass_record:` commit between mutations raises
    HeadDriftError and aborts BEFORE the second mutation's Jira write is
    issued (update_issue called exactly once)."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]
    _init_tickets_repo(tmp_path)
    acli_mod, client = _make_committing_acli_module(
        tmp_path, ["pass_record: 2026-05-28T16-31-54", None]
    )
    concurrency = _make_real_head_concurrency_module()
    with (
        patch.object(applier, "_load_acli", return_value=acli_mod),
        patch.object(applier, "_load_concurrency", return_value=concurrency),
    ):
        with pytest.raises(applier.HeadDriftError):
            applier._apply_batch(mutations, "pe", repo_root=tmp_path)
    assert client.update_issue.call_count == 1


def test_empty_subject_lookup_failure_is_non_benign_and_aborts(tmp_path, applier):
    """A commit whose subject cannot be determined (empty) is treated as
    NON-benign (conservative): HeadDriftError is raised and the second
    mutation's Jira write is never issued (update_issue called once)."""
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]
    _init_tickets_repo(tmp_path)
    # First write commits an EMPTY-subject commit → _get_commit_subject == "".
    acli_mod, client = _make_committing_acli_module(tmp_path, ["", None])
    concurrency = _make_real_head_concurrency_module()
    with (
        patch.object(applier, "_load_acli", return_value=acli_mod),
        patch.object(applier, "_load_concurrency", return_value=concurrency),
    ):
        with pytest.raises(applier.HeadDriftError):
            applier._apply_batch(mutations, "pf", repo_root=tmp_path)
    assert client.update_issue.call_count == 1


def test_abort_stderr_reports_exact_mutations_completed(tmp_path, applier, capsys):
    """After a mid-pass abort, the emitted `abort_due_to_drift` stderr JSON's
    `mutations_completed` equals the number actually applied before the drift.

    Two mutations complete (a benign commit after the first lets it continue),
    then a non-benign commit after the second aborts the third → completed == 2.
    """
    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "a"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "b"}},
        {"action": "update", "key": "DSO-12", "fields": {"summary": "c"}},
    ]
    _init_tickets_repo(tmp_path)
    acli_mod, client = _make_committing_acli_module(
        tmp_path, ["ticket: STATUS DSO-10", "pass_record: competing", None]
    )
    concurrency = _make_real_head_concurrency_module()
    with (
        patch.object(applier, "_load_acli", return_value=acli_mod),
        patch.object(applier, "_load_concurrency", return_value=concurrency),
    ):
        with pytest.raises(applier.HeadDriftError):
            applier._apply_batch(mutations, "pg", repo_root=tmp_path)

    err = capsys.readouterr().err
    abort_lines = [ln for ln in err.splitlines() if "abort_due_to_drift" in ln]
    assert abort_lines, f"expected an abort_due_to_drift stderr line, got: {err!r}"
    payload = json.loads(abort_lines[-1])
    assert payload["mutations_completed"] == 2
    assert client.update_issue.call_count == 2
