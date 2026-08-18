"""Behavioral contracts for the repository criteria-vocabulary guard (ticket 5f4d)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_criteria_vocabulary.py"
ALLOWLIST = REPO_ROOT / "scripts" / "criteria-vocabulary-allowlist.txt"


def _legacy_heading() -> str:
    return "## Success " + "Criteria"


def _legacy_singular() -> str:
    return "success " + "criterion"


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _allowlist_row(relative: str, count: int, reason: str = "fixture") -> str:
    return f"{relative}\t{count}\t{reason}\n"


def _run(root: Path, allowlist_text: str = "") -> subprocess.CompletedProcess[str]:
    allowlist = root / "allowlist.txt"
    allowlist.write_text(allowlist_text, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            "--allowlist",
            str(allowlist),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


# Happy path exposed to the implementation worker: a live violation is rejected with
# an actionable diagnostic through the real CLI entry point.
def test_live_phrase_is_rejected_with_path_and_line(tmp_path: Path) -> None:
    _write(tmp_path, "src/rebar/live.py", f'HEADING = "{_legacy_heading()}"\n')

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "src/rebar/live.py:1" in result.stderr
    assert _legacy_heading() in result.stderr


# Held-out phrase-family and clean-tree contracts.
@pytest.mark.parametrize(
    "text",
    [
        "Success " + "Criteria",
        "SUCCESS_" + "CRITERIA",
        "success-" + "criterion",
    ],
)
def test_phrase_family_is_case_and_separator_insensitive(tmp_path: Path, text: str) -> None:
    _write(tmp_path, "README.md", text)
    assert _run(tmp_path).returncode == 1


def test_clean_tree_passes(tmp_path: Path) -> None:
    _write(tmp_path, "src/rebar/live.py", "Acceptance criteria are canonical.\n")
    assert _run(tmp_path).returncode == 0


# Held-out path-scope contrast: bare abbreviations are forbidden only where they can
# denote the migrated ticket-heading concept.
@pytest.mark.parametrize(
    "relative",
    [
        "src/rebar/llm/reviewers/prompt.md",
        "src/rebar/_guides/guide.md",
        ".rebar/criteria_routing.json",
        "src/rebar/llm/plan_review/criteria_routing.json",
        "docs/plan-review-criteria-guide.md",
    ],
)
def test_bare_sc_is_rejected_in_scoped_paths(tmp_path: Path, relative: str) -> None:
    _write(tmp_path, relative, "Use SC for this heading.\n")
    assert _run(tmp_path).returncode == 1


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("src/rebar/core.py", "SC is an unrelated token.\n"),
        ("tests/unit/test_example.py", "SC is test prose.\n"),
        ("docs/adr/example.md", "O-RAN-SC is a consortium.\n"),
    ],
)
def test_bare_sc_outside_scoped_paths_is_ignored(tmp_path: Path, relative: str, text: str) -> None:
    _write(tmp_path, relative, text)
    assert _run(tmp_path).returncode == 0


# Held-out allowlist integrity: exemptions are exact-count attestations, not path-wide
# blind spots.
def test_allowlisted_exact_count_passes(tmp_path: Path) -> None:
    relative = "tests/fixture.txt"
    _write(tmp_path, relative, _legacy_heading())
    assert _run(tmp_path, _allowlist_row(relative, 1, "recorded fixture")).returncode == 0


@pytest.mark.parametrize(("expected", "actual"), [(1, 2), (2, 1)])
def test_allowlist_count_drift_fails(tmp_path: Path, expected: int, actual: int) -> None:
    relative = "tests/fixture.txt"
    _write(tmp_path, relative, "\n".join([_legacy_heading()] * actual))
    result = _run(tmp_path, _allowlist_row(relative, expected))
    assert result.returncode == 1
    assert relative in result.stderr
    assert f"expected {expected}" in result.stderr
    assert f"found {actual}" in result.stderr


@pytest.mark.parametrize(
    "allowlist_text",
    [
        "missing-fields\n",
        "file.txt\tnot-an-int\treason\n",
        "file.txt\t0\treason\n",
        "file.txt\t1\treason\nfile.txt\t1\tagain\n",
        "absent.txt\t1\treason\n",
        "../outside.txt\t1\treason\n",
        "/absolute.txt\t1\treason\n",
    ],
)
def test_malformed_duplicate_or_absent_allowlist_rows_fail(
    tmp_path: Path, allowlist_text: str
) -> None:
    _write(tmp_path, "file.txt", _legacy_heading())
    assert _run(tmp_path, allowlist_text).returncode == 1


def test_comment_and_blank_allowlist_lines_are_accepted(tmp_path: Path) -> None:
    relative = "fixture.txt"
    _write(tmp_path, relative, _legacy_singular())
    allowlist = f"# path<TAB>count<TAB>reason\n\n{_allowlist_row(relative, 1)}"
    assert _run(tmp_path, allowlist).returncode == 0


# Held-out exclusions preserve frozen history and the minified vendor bundle.
@pytest.mark.parametrize(
    "relative",
    [
        "docs/archive/history.md",
        "docs/experiments/trial.md",
        "src/rebar/llm/workflow/editor_assets/dist/editor.js",
    ],
)
def test_explicitly_excluded_paths_are_not_scanned(tmp_path: Path, relative: str) -> None:
    _write(tmp_path, relative, _legacy_heading())
    assert _run(tmp_path).returncode == 0


def test_symlinks_are_not_followed_or_reported(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text(f"SECRET={_legacy_heading()}\n", encoding="utf-8")
    (tmp_path / "leak.txt").symlink_to(outside)

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "SECRET" not in result.stderr


def test_symlink_into_excluded_tree_is_not_rescanned(tmp_path: Path) -> None:
    archived = tmp_path / "docs" / "archive" / "history.md"
    _write(tmp_path, "docs/archive/history.md", _legacy_heading())
    (tmp_path / "live-link.md").symlink_to(archived)
    assert _run(tmp_path).returncode == 0


# Held-out git-scope contract (bug d5ae): when the root IS a git repository the guard must
# consider exactly the files git would — so a gitignored tree (`.claude/worktrees/...`,
# `bridge_state/`) is invisible to it, while tracked and untracked-but-not-ignored files are
# scanned exactly as before. Before this contract the guard walked the whole filesystem and a
# single agent worktree produced tens of thousands of violations, permanently reddening the
# `make lint` commit gate in any checkout that had ever created one.
def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)


def _track_everything(root: Path) -> None:
    subprocess.run(["git", "add", "--all"], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize(
    "relative",
    [
        ".claude/worktrees/agent-1/docs/experiments/runs.jsonl",
        "bridge_state/snapshots/2026-06-29T16-50-45.json",
        "ignored-notes.md",
    ],
)
def test_gitignored_paths_are_not_scanned(tmp_path: Path, relative: str) -> None:
    _write(tmp_path, ".gitignore", ".claude/\nbridge_state/\nignored-notes.md\n")
    _write(tmp_path, relative, _legacy_heading())
    _init_git_repo(tmp_path)

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert relative not in result.stderr


@pytest.mark.parametrize("track", [False, True])
def test_unignored_files_in_a_git_repo_are_still_scanned(tmp_path: Path, track: bool) -> None:
    _write(tmp_path, ".gitignore", ".claude/\n")
    _write(tmp_path, "src/rebar/live.py", f'HEADING = "{_legacy_heading()}"\n')
    _init_git_repo(tmp_path)
    if track:
        _track_everything(tmp_path)

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "src/rebar/live.py:1" in result.stderr


def test_curated_exclusions_still_apply_inside_a_git_repo(tmp_path: Path) -> None:
    _write(tmp_path, "docs/archive/history.md", _legacy_heading())
    _init_git_repo(tmp_path)
    _track_everything(tmp_path)

    assert _run(tmp_path).returncode == 0


def test_allowlist_counts_are_unchanged_inside_a_git_repo(tmp_path: Path) -> None:
    relative = "tests/fixture.txt"
    _write(tmp_path, relative, _legacy_heading())
    _init_git_repo(tmp_path)
    _track_everything(tmp_path)

    assert _run(tmp_path, _allowlist_row(relative, 1, "recorded fixture")).returncode == 0


def test_missing_git_binary_falls_back_to_the_filesystem_walk(tmp_path: Path) -> None:
    _write(tmp_path, "src/rebar/live.py", f'HEADING = "{_legacy_heading()}"\n')
    _init_git_repo(tmp_path)

    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("", encoding="utf-8")
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    env = os.environ.copy()
    env["PATH"] = str(empty_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--allowlist", str(allowlist)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "src/rebar/live.py:1" in result.stderr


def _write_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


# End-to-end gate ownership: execute the committed root Makefile in an isolated tree.
def test_make_lint_rejects_live_vocabulary_through_guard(tmp_path: Path) -> None:
    shutil.copy2(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    (scripts / "criteria-vocabulary-allowlist.txt").write_text("", encoding="utf-8")
    (scripts / "check_dco_identity.py").write_text("", encoding="utf-8")
    # `make lint` also runs the complexity-baseline gate (story c9f7) and the
    # config-ownership + config-read gates (RP-04 S7.2) before the vocabulary guard
    # under test; stub them as no-ops so this sandbox reaches the guard.
    (scripts / "check_complexity_baseline.py").write_text("", encoding="utf-8")
    (scripts / "check_config_ownership.py").write_text("", encoding="utf-8")
    (scripts / "check_config_reads.py").write_text("", encoding="utf-8")
    _write(tmp_path, "src/rebar/live.py", _legacy_heading())

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for command in ("ruff", "zizmor", "actionlint"):
        _write_executable(stub_bin / command)
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [
            "make",
            "lint",
            "sources=src",
            f"LOCAL_BIN={stub_bin}",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "src/rebar/live.py:1" in result.stderr


# Decode policy (ticket 8b6c): an authored text format that cannot be decoded is a HOLE in the
# guard — every occurrence inside it would be silently skipped — so those suffixes fail closed,
# while genuine binaries stay tolerated so the gate does not redden on legitimate assets.
@pytest.mark.parametrize("relative", ["src/rebar/live.py", "README.md", "data.json", "conf.toml"])
def test_undecodable_source_file_fails_closed(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# " + _legacy_heading().encode() + b"\n\xff\xfe invalid\n")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert relative in result.stderr
    assert "not valid UTF-8" in result.stderr


@pytest.mark.parametrize("relative", ["assets/logo.png", "vendor/blob.bin", "archive.tar.gz"])
def test_undecodable_binary_file_is_tolerated(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01binary payload")

    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr


# Diagnostic excerpts are attacker-influenced text bound for a CI log: escaped, and capped.
def test_control_characters_in_a_matching_line_are_escaped(tmp_path: Path) -> None:
    _write(tmp_path, "src/rebar/live.py", f"x = 1\x00\x1b[31m {_legacy_heading()}\n")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "\x00" not in result.stderr
    assert "\x1b" not in result.stderr
    assert "\\x00" in result.stderr
    assert "\\x1b" in result.stderr


def test_long_matching_line_is_truncated(tmp_path: Path) -> None:
    _write(tmp_path, "src/rebar/live.py", f"{_legacy_heading()} {'A' * 5000}\n")

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "[truncated]" in result.stderr
    assert "A" * 400 not in result.stderr
    assert max(len(line) for line in result.stderr.splitlines()) < 400


# The committed inventory and all live migrations are validated through the real CLI.
def test_current_repository_vocabulary_is_clean() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ALLOWLIST.is_file()
