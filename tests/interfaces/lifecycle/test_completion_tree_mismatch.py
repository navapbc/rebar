"""A completion close names the tree it verifies and refuses a tree that lacks the work.

Bug wieldable-runtish-moorhen: an MCP close verified the SERVER process's checkout (HEAD ``D``)
while the work had landed as commit ``C`` that ``D`` does not contain. The close precheck still
accepted ``C`` (its stale-clone refresh rescans the upstream refs), so the verifier ran against
``D`` and reported confident, line-cited "not implemented" findings for landed work. The FAIL
message named no tree, so the wrong-tree read looked like genuinely unmet criteria.

Contract pinned here:

* when no commit the precheck accepted is contained in the verification tree, the close
  refuses BEFORE the verifier runs and names both the tree and the landed commits;
* ``ref=<landed commit>`` is the remedy, and closes signed;
* a genuine FAIL names the verified tree's full SHA and where it came from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._snapshot.repo_snapshot import resolve_ref

_DESC = "Body.\n\n## Acceptance Criteria\n- [x] done\n\n## Context\nc\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _ticket_with_impact(repo: Path) -> str:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")
    tid = rebar.create_ticket("task", "tree mismatch", description=_DESC, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    rebar.set_file_impact(tid, [{"path": "src/x.py", "reason": "touched"}], repo_root=str(repo))
    return tid


def _stub_verifier(monkeypatch: pytest.MonkeyPatch, verdict: str, calls: list) -> None:
    """Offline verifier that resolves the ref it is handed, as the attested verifier does."""

    def fake(ticket_id, *, ref=None, repo_root=None, **kwargs):
        sha = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        calls.append(sha)
        findings = []
        if verdict != "PASS":
            findings = [{"criterion": "AC1", "detail": "still defined at x.py:200"}]
        return {
            "verdict": verdict,
            "findings": findings,
            "runner": "fake",
            "model": "fake",
            "verified_at_sha": sha,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", fake)


def _land_upstream_only(repo: Path, tmp_path: Path, tid: str) -> tuple[str, str]:
    """Server checkout at D; the work C exists only on its remote. Returns (D, C)."""
    _git(repo, "commit", "--allow-empty", "-q", "-m", "unrelated side commit")
    server_head = _git(repo, "rev-parse", "HEAD")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {tid}")
    landed = _git(upstream, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(upstream))
    return server_head, landed


def test_close_refuses_tree_lacking_the_landed_commit_before_verifying(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _ticket_with_impact(rebar_repo)
    server_head, landed = _land_upstream_only(rebar_repo, tmp_path, tid)
    calls: list[str] = []
    _stub_verifier(monkeypatch, "FAIL", calls)

    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    err = ei.value.stderr
    assert calls == []  # no per-criterion findings were produced from the wrong tree
    assert "unmet" not in err and "x.py:200" not in err, err
    assert server_head in err and landed in err, err
    assert "ref" in err, err
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress"


def test_ref_to_the_landed_commit_is_the_remedy(
    rebar_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _ticket_with_impact(rebar_repo)
    _server_head, landed = _land_upstream_only(rebar_repo, tmp_path, tid)
    _git(rebar_repo, "fetch", "-q", "origin")
    calls: list[str] = []
    _stub_verifier(monkeypatch, "PASS", calls)

    rebar.transition(tid, "in_progress", "closed", ref=landed, repo_root=str(rebar_repo))

    assert calls == [landed]
    signature = rebar.verify_signature(tid, kind="completion-verifier", repo_root=str(rebar_repo))
    assert signature["verdict"] == "certified", signature


def test_fail_names_the_verified_head_and_its_source(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _ticket_with_impact(rebar_repo)
    _git(rebar_repo, "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {tid}")
    head = _git(rebar_repo, "rev-parse", "HEAD")
    calls: list[str] = []
    _stub_verifier(monkeypatch, "FAIL", calls)

    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    assert calls == [head]
    assert f"{head} (HEAD of " in ei.value.stderr, ei.value.stderr


def test_fail_names_an_explicit_ref_as_the_source(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = _ticket_with_impact(rebar_repo)
    _git(rebar_repo, "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {tid}")
    target = _git(rebar_repo, "rev-parse", "HEAD")
    _git(rebar_repo, "commit", "--allow-empty", "-q", "-m", "later")
    calls: list[str] = []
    _stub_verifier(monkeypatch, "FAIL", calls)

    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", ref=target[:12], repo_root=str(rebar_repo))

    assert calls == [target]
    assert f"{target} (explicit ref '{target[:12]}')" in ei.value.stderr, ei.value.stderr


def test_commits_absent_from_the_clone_are_left_out_of_the_comparison(rebar_repo: Path) -> None:
    from rebar._commands import close_scope
    from rebar._commands._seam import CommandError

    _git(rebar_repo, "commit", "--allow-empty", "-q", "-m", "side")
    side = _git(rebar_repo, "rev-parse", "HEAD")
    _git(rebar_repo, "checkout", "-q", "HEAD~1")
    tree = _git(rebar_repo, "rev-parse", "HEAD")
    absent = "f" * 40

    close_scope.ensure_tree_contains_landed("T-1", [absent], str(rebar_repo), tree, "HEAD of x")
    with pytest.raises(CommandError) as ei:
        close_scope.ensure_tree_contains_landed(
            "T-1", [absent, side], str(rebar_repo), tree, "HEAD of x"
        )
    assert side in str(ei.value) and absent not in str(ei.value)


def test_an_unreadable_verification_tree_refuses_rather_than_passing(rebar_repo: Path) -> None:
    from rebar._commands import close_scope
    from rebar._commands._seam import CommandError

    _git(rebar_repo, "commit", "--allow-empty", "-q", "-m", "work")
    work = _git(rebar_repo, "rev-parse", "HEAD")

    with pytest.raises(CommandError):
        close_scope.ensure_tree_contains_landed(
            "T-1", [work], str(rebar_repo), "e" * 40, "explicit ref 'x'"
        )
