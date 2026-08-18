"""Mutation-confirmation contract (ticket 6bda-9d58-8546-4638).

Every mutating CLI verb (``_WRITES_FULL`` + ``_LIFECYCLE``) confirms its result on
stdout with one kubectl-style line — ``<past-tense-verb> <args-summary>`` on a
successful write, ``no change: <reason>`` on an idempotent no-op — plus the global
``--quiet``/``-q`` and ``--output``/``-o`` flags extracted at the top-level router.

All assertions target OBSERVABLE behaviour only: process stdout/stderr, exit
codes, the on-disk event log, and the reduced state ``rebar show`` reports.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar.graph._links import add_dependency

pytestmark = pytest.mark.unit

_ID_RE = re.compile(r"\b[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}\b")


# ── fixtures / helpers ────────────────────────────────────────────────────────
@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real initialized rebar store rooted at a throwaway git repo."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _cli(*args: str, repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=subprocess_env(),
    )


def _create(repo: Path, ttype: str = "task", title: str = "t", *extra: str) -> str:
    proc = _cli("create", ttype, title, "--output", "json", *extra, repo=repo)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["id"]


def _event_count(repo: Path, tid: str) -> int:
    return len(list((repo / ".tickets-tracker" / tid).glob("*.json")))


def _alias_of(repo: Path, tid: str) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo)).get("alias") or tid


# ── create / idea (normalized; zero information loss) ────────────────────────
def test_create_confirmation_carries_alias_id_and_title(repo: Path) -> None:
    proc = _cli("create", "task", "confirmation subject", repo=repo)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, "one confirmation line, no trailing bare-id line"
    line = lines[0]
    tid = _ID_RE.search(line).group(0)
    alias = _alias_of(repo, tid)
    # Every datum of the old two-line form survives: alias, canonical id, title.
    assert line == f"created {alias} ({tid}): confirmation subject"


def test_create_legacy_json_shape_is_unchanged(repo: Path) -> None:
    proc = _cli("create", "task", "json shape", "--output", "json", repo=repo)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert set(doc) == {"id", "alias", "title"}
    assert doc["title"] == "json shape"


def test_idea_confirmation_keeps_idea_marker(repo: Path) -> None:
    proc = _cli("idea", "an idea title", repo=repo)
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    tid = _ID_RE.search(line).group(0)
    assert line == f"created idea {_alias_of(repo, tid)} ({tid}): an idea title"


# ── comment (registry leaf; global-flag extraction) ───────────────────────────
def test_comment_confirmation_and_quiet_and_json(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("comment", tid, "hello", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"comment added to {tid}"

    quiet = _cli("comment", tid, "quiet body", "--quiet", repo=repo)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == "", "--quiet suppresses the confirmation"

    js = _cli("comment", tid, "json body", "-o", "json", repo=repo)
    assert js.returncode == 0, js.stderr
    doc = json.loads(js.stdout)
    assert doc == {"outcome": "commented", "subject": tid, "detail": "comment added"}


def test_extraction_never_consumes_tokens_after_double_dash(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("comment", tid, "--", "--quiet is part of the body", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"comment added to {tid}", "still confirms (not quiet)"
    comments = rebar.show_ticket(tid, repo_root=str(repo))["comments"]
    assert comments[-1]["body"] == "--quiet is part of the body"


def test_quiet_plus_output_json_still_prints_json(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("tag", tid, "qt", "--quiet", "--output", "json", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["outcome"] == "tagged"


def test_invalid_output_mode_is_exit_2(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("tag", tid, "x", "--output", "yaml", repo=repo)
    assert proc.returncode == 2
    assert "unsupported output format" in proc.stderr


# ── tag / untag / archive (no-op detection; zero extra events on no-op) ───────
def test_tag_untag_success_and_noop_lines(repo: Path) -> None:
    tid = _create(repo)
    assert _cli("tag", tid, "alpha", repo=repo).stdout.strip() == f"tagged {tid}: +alpha"
    before = _event_count(repo, tid)
    noop = _cli("tag", tid, "alpha", repo=repo)
    assert noop.returncode == 0
    assert noop.stdout.strip() == f"no change: tag alpha already on {tid}"
    assert _event_count(repo, tid) == before, "an idempotent no-op writes nothing"

    assert _cli("untag", tid, "alpha", repo=repo).stdout.strip() == f"untagged {tid}: -alpha"
    before = _event_count(repo, tid)
    noop = _cli("untag", tid, "alpha", repo=repo)
    assert noop.returncode == 0
    assert noop.stdout.strip() == f"no change: tag alpha not on {tid}"
    assert _event_count(repo, tid) == before


def test_archive_success_and_noop(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("archive", tid, repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"archived {tid}"
    before = _event_count(repo, tid)
    noop = _cli("archive", tid, repo=repo)
    assert noop.returncode == 0
    assert noop.stdout.strip() == f"no change: {tid} already archived"
    assert _event_count(repo, tid) == before


# ── link / unlink ─────────────────────────────────────────────────────────────
def test_link_success_noop_and_unlink_lines(repo: Path) -> None:
    a, b = _create(repo, title="a"), _create(repo, title="b")
    proc = _cli("link", a, b, "relates_to", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"linked {a} -> {b} (relates_to)"

    before = _event_count(repo, a)
    noop = _cli("link", a, b, "relates_to", repo=repo)
    assert noop.returncode == 0
    assert noop.stdout.strip() == f"no change: already linked {a} -> {b} (relates_to)"
    assert _event_count(repo, a) == before, "re-linking an identical edge writes nothing"

    un = _cli("unlink", a, b, repo=repo)
    assert un.returncode == 0, un.stderr
    assert un.stdout.strip() == f"unlinked {a} -/-> {b} (relates_to)"


def _redirect_pair(repo: Path) -> tuple[str, str, str]:
    """An (epic, other-epic, child-task) triple whose task→epic blocking link the
    hierarchy resolver promotes to epic→epic, emitting a REDIRECT record."""
    e1 = _create(repo, "epic", "epic one")
    e2 = _create(repo, "epic", "epic two")
    t1 = _create(repo, "task", "child task", "--parent", e1)
    return e1, e2, t1


def test_link_redirect_record_is_byte_identical_and_sole_stdout_result(repo: Path) -> None:
    e1, e2, t1 = _redirect_pair(repo)
    golden = (
        json.dumps(
            {
                "redirected": True,
                "original": {"source": t1, "target": e2},
                "resolved": {"source": e1, "target": e2},
            }
        )
        + "\n"
    )
    write = _cli("link", t1, e2, "blocks", repo=repo)
    assert write.returncode == 0, write.stderr
    assert write.stdout == golden, "REDIRECT record byte-identical on the write path"

    noop = _cli("link", t1, e2, "blocks", repo=repo)
    assert noop.returncode == 0
    assert noop.stdout == golden, "byte-identical on the no-op path"

    _cli("unlink", e1, e2, repo=repo)
    quiet = _cli("link", t1, e2, "blocks", "--quiet", repo=repo)
    assert quiet.returncode == 0
    assert quiet.stdout == golden, "--quiet never suppresses machine data"


def test_link_redirect_nests_in_json_envelope(repo: Path) -> None:
    e1, e2, t1 = _redirect_pair(repo)
    proc = _cli("link", t1, e2, "blocks", "-o", "json", repo=repo)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)  # ONE valid JSON document on stdout
    assert doc["outcome"] == "linked"
    assert doc["redirect"] == {
        "redirected": True,
        "original": {"source": t1, "target": e2},
        "resolved": {"source": e1, "target": e2},
    }


def test_add_dependency_redirect_return_contract_unchanged(repo: Path) -> None:
    """The consumed ``dict | None`` return keeps its type and meaning; the parallel
    ``on_outcome`` channel reports wrote-vs-noop exactly once per call."""
    e1, e2, t1 = _redirect_pair(repo)
    tracker = str(repo / ".tickets-tracker")
    calls: list[dict] = []
    record = add_dependency(t1, e2, tracker, "blocks", on_outcome=calls.append)
    assert record == {
        "redirected": True,
        "original": {"source": t1, "target": e2},
        "resolved": {"source": e1, "target": e2},
    }
    assert calls == [{"wrote": True, "source": e1, "target": e2, "relation": "blocks"}]

    calls.clear()
    again = add_dependency(t1, e2, tracker, "blocks", on_outcome=calls.append)
    assert again == record, "no-op path still returns the REDIRECT record"
    assert calls == [{"wrote": False, "source": e1, "target": e2, "relation": "blocks"}]

    a, b = _create(repo, title="plain-a"), _create(repo, title="plain-b")
    assert add_dependency(a, b, tracker, "relates_to") is None, "no redirect → None"


# ── edit (field NAMES only, never values) ─────────────────────────────────────
def test_edit_confirms_field_names_never_values(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli(
        "edit", tid, "--title", "SECRETVALUE", "--priority", "2", "--add-tag=x,y", repo=repo
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"edited {tid}: title, priority, add-tag"
    assert "SECRETVALUE" not in proc.stdout


def test_edit_json_envelope(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("edit", tid, "--priority", "1", "-o", "json", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "outcome": "edited",
        "subject": tid,
        "detail": "priority",
    }


# ── revert (normalized; keeps uuid + id) ──────────────────────────────────────
def test_revert_confirmation_keeps_event_uuid_and_id(repo: Path) -> None:
    tid = _create(repo)
    _cli("comment", tid, "to be reverted", repo=repo)
    events = sorted((repo / ".tickets-tracker" / tid).glob("*COMMENT.json"))
    uuid = json.loads(events[-1].read_text())["uuid"]
    proc = _cli("revert", tid, uuid, repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"reverted {tid}: event {uuid}"


# ── set-file-impact / set-verify-commands / attach-commits ────────────────────
def test_file_impact_and_verify_commands_lines(repo: Path) -> None:
    tid = _create(repo)
    fi = _cli("set-file-impact", tid, '[{"path":"a.py","reason":"r"}]', repo=repo)
    assert fi.returncode == 0, fi.stderr
    assert fi.stdout.strip() == f"impact set on {tid}: 1 paths"

    none = _cli("set-file-impact", tid, "--none", "touches no repository file", repo=repo)
    assert none.returncode == 0, none.stderr
    assert none.stdout.strip() == f"impact set on {tid}: none declared"

    vc = _cli(
        "set-verify-commands",
        tid,
        '[{"dd_id":"DD1","dd_text":"t","command":"true"}]',
        repo=repo,
    )
    assert vc.returncode == 0, vc.stderr
    assert vc.stdout.strip() == f"verify-commands set on {tid}: 1"


def test_attach_commits_confirmation_keeps_count_and_id(repo: Path) -> None:
    tid = _create(repo)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "seed"], cwd=repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    proc = _cli("attach-commits", tid, sha, repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"commits attached to {tid}: 1"


# ── session-log (normalized; keeps id, alias, created flag) ───────────────────
def test_session_log_start_and_append_lines(repo: Path) -> None:
    start = _cli("session-log", "start", repo=repo)
    assert start.returncode == 0, start.stderr
    m = re.fullmatch(r"session-log started (\S+) \(([0-9a-f-]{19})\)", start.stdout.strip())
    assert m, start.stdout
    log_id = m.group(2)
    assert _alias_of(repo, log_id) == m.group(1)

    append = _cli("session-log", "append", "an entry", repo=repo)
    assert append.returncode == 0, append.stderr
    assert append.stdout.strip() == f"session-log appended to {log_id}"

    js = _cli("session-log", "append", "another", "-o", "json", repo=repo)
    assert js.returncode == 0, js.stderr
    doc = json.loads(js.stdout)
    assert doc["outcome"] == "session-log-appended"
    assert doc["subject"] == log_id


# ── transition / claim / reopen (normalized; UNBLOCKED datum preserved) ───────
def test_transition_line_preserves_unblocked_ids(repo: Path) -> None:
    blocker = _create(repo, title="blocker")
    blocked = _create(repo, title="blocked")
    _cli("link", blocker, blocked, "blocks", repo=repo)
    _cli("transition", blocker, "open", "in_progress", repo=repo)
    proc = _cli("transition", blocker, "in_progress", "closed", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert (
        proc.stdout.strip()
        == f"transitioned {blocker}: in_progress -> closed; unblocked: {blocked}"
    )


def test_transition_success_and_noop_and_json_golden(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("transition", tid, "open", "in_progress", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"transitioned {tid}: open -> in_progress; unblocked: none"

    noop = _cli("transition", tid, "in_progress", "in_progress", repo=repo)
    assert noop.returncode == 0
    assert noop.stdout.strip() == f"no change: {tid} already in_progress"

    js = _cli("transition", tid, "in_progress", "closed", "--output", "json", repo=repo)
    assert js.returncode == 0, js.stderr
    assert json.loads(js.stdout) == {
        "ticket_id": tid,
        "from": "in_progress",
        "to": "closed",
        "newly_unblocked": [],
    }, "the pre-existing JSON shape is byte-compatible"


def test_reopen_line_and_unarchive_transition_line(repo: Path) -> None:
    tid = _create(repo)
    _cli("transition", tid, "open", "in_progress", repo=repo)
    _cli("transition", tid, "in_progress", "closed", repo=repo)
    proc = _cli("reopen", tid, repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"reopened {tid}: closed -> open"

    other = _create(repo, title="to archive")
    _cli("archive", other, repo=repo)
    un = _cli("transition", other, "archived", "open", repo=repo)
    assert un.returncode == 0, un.stderr
    m = re.fullmatch(
        rf"transitioned {re.escape(other)}: archived -> open \(reverted event ([0-9a-f-]+)\)",
        un.stdout.strip(),
    )
    assert m, un.stdout


def test_claim_line_keeps_assignee_and_json_shape(repo: Path) -> None:
    tid = _create(repo)
    proc = _cli("claim", tid, "--assignee", "dana@example.com", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"claimed {tid}: open -> in_progress (assignee dana@example.com)"

    other = _create(repo, title="claim json")
    js = _cli("claim", other, "--output", "json", repo=repo)
    assert js.returncode == 0, js.stderr
    doc = json.loads(js.stdout)
    assert doc["ticket_id"] == other and doc["status"] == "in_progress"


# ── the shared contract: quiet, stdout/stderr split ───────────────────────────
def test_quiet_suppresses_every_text_confirmation(repo: Path) -> None:
    proc = _cli("create", "task", "quiet create", "-q", repo=repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "the confirmation is the only stdout content to suppress"


def test_confirmations_go_to_stdout_and_logs_to_stderr(repo: Path) -> None:
    proc = _cli("create", "task", "split check", repo=repo)
    assert proc.stdout.strip().startswith("created ")
    assert "Warning" not in proc.stdout, "advisory chatter stays on stderr"
    assert "no file_impact" in proc.stderr
