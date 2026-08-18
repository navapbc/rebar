"""Coverage for the attach-commits repair surface + the DET file-impact-vs-diff check.

The DET half is exercised through the pure helpers in ``_engine_support.commit_impact`` and
through ``close_precheck._check_file_impact_vs_diff`` directly, so the assertions pin the
decision logic rather than a full close's surrounding gate machinery.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from rebar._commands import close_precheck
from rebar._commands._seam import CommandError
from rebar._engine_support import commit_impact


# ── Path matching ────────────────────────────────────────────────────────────
def test_glob_match_double_star_matches_at_repo_root():
    # bare fnmatch misses the root case; the `**/`-stripping fallback is the whole point
    assert commit_impact.glob_match("README.md", "**/*.md")
    assert commit_impact.glob_match("docs/deep/notes.md", "**/*.md")


def test_exempt_globs_cover_tests_docs_and_markdown():
    assert commit_impact.is_exempt("tests/unit/test_x.py")
    assert commit_impact.is_exempt("docs/user-guide.md")
    assert commit_impact.is_exempt("CHANGELOG.md")
    assert commit_impact.is_exempt("README.md")  # repo-root markdown
    assert not commit_impact.is_exempt("src/rebar/_lib_writes.py")


def test_impact_covers_is_exact_or_directory_prefix_never_substring():
    assert commit_impact.impact_covers("src/rebar/a.py", "src/rebar/a.py")
    assert commit_impact.impact_covers("src/rebar/", "src/rebar/deep/b.py")
    assert not commit_impact.impact_covers("src/rebar/a.py", "src/rebar/a.py.bak")
    assert not commit_impact.impact_covers("src/reb", "src/rebar/a.py")


def test_undeclared_paths_reports_only_offenders():
    changed = ["src/rebar/x.py", "tests/unit/test_x.py", "src/other/y.py", "README.md"]
    assert commit_impact.undeclared_paths(changed, ["src/rebar/x.py"]) == ["src/other/y.py"]


def test_undeclared_paths_empty_when_all_declared_or_exempt():
    assert commit_impact.undeclared_paths(["src/x.py", "docs/a.md"], ["src/x.py"]) == []


# ── git-backed helpers ───────────────────────────────────────────────────────
def _git(repo, *args, **kw):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **kw
    )


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e",
        "commit",
        "-qm",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "f.txt").write_text("hi\n")
    return tmp_path, _commit(tmp_path, "seed")


def test_invalid_commit_shas_flags_only_unresolvable(git_repo):
    repo, head = git_repo
    assert commit_impact.invalid_commit_shas([head], str(repo)) == []
    assert commit_impact.invalid_commit_shas([head, "deadbeef"], str(repo)) == ["deadbeef"]


def test_changed_paths_reads_diff_and_returns_none_on_error(git_repo):
    repo, head = git_repo
    assert commit_impact.changed_paths(head, str(repo)) == ["f.txt"]
    # a SHA that is not in this repo is unreadable -> None (distinct from an empty commit)
    assert commit_impact.changed_paths("deadbeef", str(repo)) is None


def test_is_merge_commit_detects_two_parents(git_repo):
    repo, base = git_repo
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("s\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("m\n")
    _commit(repo, "main change")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "merge", "-q", "--no-ff", "side")
    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert commit_impact.is_merge_commit(merge_sha, str(repo))
    assert not commit_impact.is_merge_commit(base, str(repo))


# ── The DET close check ──────────────────────────────────────────────────────
def test_work_landed_skips_commit_discovery_when_scope_has_no_impact(monkeypatch):
    from rebar._engine_support import field_reads

    monkeypatch.setattr(close_precheck, "_union_file_impact", lambda *a, **k: [])
    monkeypatch.setattr(field_reads, "file_impact", lambda *a, **k: [])

    def unexpected_discovery(*args, **kwargs):
        raise AssertionError("empty accepted scope must not scan commit history")

    monkeypatch.setattr(close_precheck, "_referencing_commits", unexpected_discovery)

    close_precheck._check_work_landed("ticket", "ticket", {"ticket"}, "/tracker", "/code")


def test_work_landed_still_discovers_commits_for_descendant_impact(monkeypatch):
    from rebar._engine_support import field_reads

    discovered: list[tuple[set[str], str, str]] = []
    checked: list[tuple[set[str], list[str], str, str]] = []
    accepted = {"parent", "child"}

    monkeypatch.setattr(close_precheck, "_union_file_impact", lambda *a, **k: ["src/child.py"])
    monkeypatch.setattr(field_reads, "file_impact", lambda *a, **k: [])
    monkeypatch.setattr(
        close_precheck,
        "_referencing_commits",
        lambda ids, tracker, root: discovered.append((ids, tracker, root)) or ["child-sha"],
    )
    monkeypatch.setattr(
        close_precheck,
        "_check_file_impact_vs_diff",
        lambda ids, commits, tracker, root: checked.append((ids, commits, tracker, root)),
    )

    close_precheck._check_work_landed("parent", "parent", accepted, "/tracker", "/code")

    assert discovered == [(accepted, "/tracker", "/code")]
    assert checked == [(accepted, ["child-sha"], "/tracker", "/code")]


@pytest.fixture
def det(monkeypatch):
    """Drive `_check_file_impact_vs_diff` with injected impact/commit data."""

    def run(*, impact, attached=(), referencing=(), paths=None, merges=(), unreadable=()):
        monkeypatch.setattr(close_precheck, "_union_file_impact", lambda *a, **k: list(impact))
        monkeypatch.setattr(close_precheck, "_attached_commit_shas", lambda *a, **k: list(attached))
        monkeypatch.setattr(commit_impact, "is_merge_commit", lambda sha, root: sha in set(merges))
        monkeypatch.setattr(
            commit_impact,
            "changed_paths",
            lambda sha, root: None if sha in set(unreadable) else list((paths or {}).get(sha, [])),
        )
        close_precheck._check_file_impact_vs_diff({"t"}, list(referencing), "/tracker", "/code")

    return run


def test_undeclared_path_blocks_and_names_commit_and_path(det):
    with pytest.raises(CommandError) as exc:
        det(impact=["src/a.py"], referencing=["sha1"], paths={"sha1": ["src/rogue.py"]})
    assert "src/rogue.py" in exc.value.message
    assert "sha1" in exc.value.message
    assert "set-file-impact" in exc.value.message  # the documented remedy


def test_conforming_diff_does_not_block(det):
    det(impact=["src/a.py"], referencing=["sha1"], paths={"sha1": ["src/a.py"]})


def test_exempt_path_does_not_block(det):
    det(impact=["src/a.py"], referencing=["sha1"], paths={"sha1": ["README.md", "tests/t.py"]})


def test_no_declared_impact_skips_the_check(det):
    det(impact=[], referencing=["sha1"], paths={"sha1": ["anything/at/all.py"]})


def test_merge_commit_is_skipped_not_read_as_empty(det):
    # would block if its (empty) combined diff were trusted or its paths read
    det(
        impact=["src/a.py"],
        referencing=["m1"],
        merges=["m1"],
        paths={"m1": ["src/rogue.py"]},
    )


def test_unreadable_referencing_commit_fails_closed(det):
    with pytest.raises(CommandError) as exc:
        det(impact=["src/a.py"], referencing=["sha1"], unreadable=["sha1"])
    assert "sha1" in exc.value.message


def test_unreadable_attached_sha_is_skipped(det):
    # an attached SHA may live in another clone — absence is not evidence of a problem
    det(impact=["src/a.py"], attached=["foreign"], unreadable=["foreign"])


# ── Refactor contract: the bool wrapper still behaves ────────────────────────
def test_referencing_commit_exists_wraps_referencing_commits(monkeypatch):
    monkeypatch.setattr(close_precheck, "_referencing_commits", lambda *a, **k: ["abc"])
    assert close_precheck._referencing_commit_exists({"t"}, "/tracker", "/code") is True
    monkeypatch.setattr(close_precheck, "_referencing_commits", lambda *a, **k: [])
    assert close_precheck._referencing_commit_exists({"t"}, "/tracker", "/code") is False


# ── One implementation of the glob rule; no LLM in the DET path ──────────────
def test_both_llm_copies_delegate_to_the_shared_helper(monkeypatch):
    from rebar.llm.code_review import registry
    from rebar.llm.prompting import prompts

    monkeypatch.setattr(commit_impact, "glob_match", lambda path, pattern: "SENTINEL")
    assert prompts._glob_match("a", "b") == "SENTINEL"
    assert registry._glob_match("a", "b") == "SENTINEL"


def test_det_path_imports_no_llm_module():
    code = (
        "import sys;"
        "from rebar._engine_support import commit_impact;"
        "commit_impact.undeclared_paths(['a/b.py'], ['a/b.py']);"
        "leaked=[m for m in sys.modules if m.startswith('rebar.llm')];"
        "assert leaked == [], leaked"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# ── Docs name the verb + the remedy ──────────────────────────────────────────
def _repo_file(name):
    from pathlib import Path

    return (Path(__file__).resolve().parents[2] / name).read_text(encoding="utf-8")


def test_docs_mcp_reference_names_the_tool():
    assert "attach_commits" in _repo_file("docs/mcp-reference.md")


def test_docs_guide_verb():
    assert "rebar attach-commits" in _repo_file("src/rebar/_guides/commit-ticket-trailer.md")


def test_docs_guide_remedy():
    assert "rebar set-file-impact" in _repo_file("src/rebar/_guides/commit-ticket-trailer.md")
