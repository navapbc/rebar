"""The repo-snapshot process under non-default environments (epic raze-vet-ditch).

- (9af8) a GitHub-Actions-style checkout: shallow (depth 1) + detached HEAD, the
  default shape of actions/checkout. Confirms attested materialize works there, and
  pins review-code's documented dependency on clone depth (HEAD~1 needs history).
- (efd0) an end-to-end attested gate run against a NON-rebar sample project, confirming
  the process has no rebar-specific path assumptions and reads the snapshot (not the
  checkout), with the pinned verified_at_sha that backs a signature.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm  # noqa: F401
from rebar._snapshot import repo_snapshot as rs
from rebar.llm.runner import FakeRunner


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


class _Capturing(FakeRunner):
    def __init__(self):
        super().__init__([])
        self.repo_path = None
        self.files: dict[str, str] = {}

    def run(self, req):
        self.repo_path = req.config.repo_path
        root = Path(req.config.repo_path)
        for rel in ("src/app.py", "package.json", "README.md"):
            p = root / rel
            if p.exists():
                self.files[rel] = p.read_text()
        return super().run(req)


# --------------------------------------------------------------------------------------
# 9af8 — GitHub Actions runner shape: shallow (depth 1) + detached HEAD
# --------------------------------------------------------------------------------------
@pytest.fixture
def gha_checkout(tmp_path, monkeypatch):
    """A shallow, detached-HEAD clone — the actions/checkout default."""
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "g"))
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@e.com")
    _git(origin, "config", "user.name", "T")
    _git(origin, "config", "commit.gpgsign", "false")
    (origin / "f.txt").write_text("v1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "c1")
    (origin / "f.txt").write_text("v2\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "c2")
    head_sha = _git(origin, "rev-parse", "HEAD")

    gha = tmp_path / "gha"
    # actions/checkout default: --depth 1 (shallow), then a detached checkout of the SHA.
    subprocess.run(
        ["git", "clone", "--depth", "1", "--no-tags", "-q", origin.as_uri(), str(gha)],
        check=True,
        capture_output=True,
    )
    _git(gha, "checkout", "-q", "--detach", "HEAD")
    return gha, head_sha


def test_attested_materialize_works_in_shallow_detached_checkout(gha_checkout):
    gha, head_sha = gha_checkout
    # Detached + shallow: resolving HEAD (no fetch) and materializing the committed tree
    # works from the local object DB — the close gate's `ref=HEAD, fetch=False` path.
    handle = rs.materialize("HEAD", source_mode="attested", repo_root=str(gha), fetch=False)
    assert handle.sha == head_sha
    assert (handle.path / "f.txt").read_text() == "v2\n"


def test_review_code_under_shallow_clone_surfaces_clear_error(gha_checkout, monkeypatch):
    gha, _ = gha_checkout
    # A depth-1 clone has no HEAD~1, so a base..head diff cannot resolve. With the capability
    # ENABLED, review-code must surface a clear error (not a silent empty review) — documents the
    # fetch-depth need. review_code is OFF by default + inert (WS4), so force-enable the gate here
    # so it actually reaches the git-range resolution rather than short-circuiting to empty.
    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    with pytest.raises(Exception) as exc:
        rebar.llm.review_code(
            base="HEAD~1", head="HEAD", source="local", runner=FakeRunner([]), repo_root=str(gha)
        )
    msg = str(exc.value).lower()
    assert "head~1" in msg or "ambiguous" in msg or "git diff" in msg or "diff_text" in msg


# --------------------------------------------------------------------------------------
# efd0 — end-to-end attested gate against a NON-rebar sample project
# --------------------------------------------------------------------------------------
def test_attested_gate_against_non_rebar_sample_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "g"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    # A sample project with NONE of rebar's structure (no src/rebar, no .tickets-tracker
    # in the committed tree) — a generic JS/Python-ish repo.
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "package.json").write_text('{"name":"sample","version":"1.0.0"}\n')
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def main():\n    return 'sample-project'\n")
    (repo / "README.md").write_text("# Sample (not rebar)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sample project")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    main_sha = _git(repo, "rev-parse", "origin/main")

    # rebar tracks a ticket here (the store is rebar's; the CODE is the sample project).
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "review the sample", repo_root=str(repo))

    runner = _Capturing()
    result = rebar.llm.review_ticket(
        tid,
        "ticket-quality",
        ref="origin/main",
        source="attested",
        runner=runner,
        repo_root=str(repo),
    )
    # Attested verdict pins the SHA that backs a signature (signing itself is repo-agnostic,
    # covered by the S4 signing tests).
    assert result["verified_at_sha"] == main_sha
    assert result["signable"] is True
    # The gate read the SNAPSHOT of the sample project (no rebar-specific assumptions): the
    # snapshot byte-matches the committed sample files, and is NOT the rebar source tree.
    assert Path(runner.repo_path) != repo
    assert runner.files.get("package.json") == '{"name":"sample","version":"1.0.0"}\n'
    assert "sample-project" in runner.files.get("src/app.py", "")
    assert not (Path(runner.repo_path) / "src" / "rebar").exists()
