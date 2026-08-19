from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "infra" / "scripts" / "reclaim_bridge_history.py"
PROTECTED_TAG = "refs/tags/pre-heal-a118-20260710T005736Z"
PACK_CEILING = 150 * 1024 * 1024


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _bare_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass(frozen=True)
class ReclaimFixture:
    remote: Path
    scratch: Path
    old_tip: str
    protected_tag_tip: str
    state_files: dict[str, str]


@pytest.fixture
def reclaim_fixture(tmp_path: Path) -> ReclaimFixture:
    seed = tmp_path / "seed"
    remote = tmp_path / "source.git"
    scratch = tmp_path / "scratch"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.name", "Reclaim Test")
    _git(seed, "config", "user.email", "reclaim@example.com")
    _write(seed, "tickets/base.json", '{"base": true}\n')
    _commit(seed, "base")
    _git(seed, "branch", "-M", "tickets")

    state_files = {
        ".bridge_state/bindings.json": '{"bindings": ["current"]}\n',
        ".bridge_state/prev_snapshot.json": '{"keys": ["current"]}\n',
        ".bridge_state/get_cursor.json": '{"cursor": "current"}\n',
        ".bridge_state/get_rotation.json": '{"rotation": "current"}\n',
    }
    for path, content in state_files.items():
        _write(seed, path, content.replace("current", "historical"))
    _write(seed, ".bridge_state.bak-retarget/prev_snapshot.json", "large historical backup\n")
    protected_tag_tip = _commit(seed, "add historical bridge caches")

    _git(seed, "switch", "-q", "-c", "side")
    _write(seed, "side-ticket/1-EVENT.json", '{"side": true}\n')
    _commit(seed, "side event")
    _git(seed, "switch", "-q", "tickets")
    _write(seed, "main-ticket/1-EVENT.json", '{"main": true}\n')
    _write(seed, ".bridge_state/bindings.json", '{"bindings": ["middle"]}\n')
    _commit(seed, "main event and cache churn")
    _git(seed, "merge", "-q", "--no-ff", "side", "-m", "merge side event")

    shutil.rmtree(seed / ".bridge_state.bak-retarget")
    for path, content in state_files.items():
        _write(seed, path, content)
    _write(seed, "tickets/final.json", '{"final": true}\n')
    old_tip = _commit(
        seed,
        "M 100644 deadbeef .bridge_state/message-bytes-must-survive.json\n\nbody",
    )
    assert not (seed / ".bridge_state.bak-retarget").exists()

    remote.mkdir()
    _git(remote, "init", "-q", "--bare")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "tickets:tickets")
    _git(seed, "tag", PROTECTED_TAG.removeprefix("refs/tags/"), protected_tag_tip)
    _git(seed, "push", "-q", "origin", PROTECTED_TAG)
    _bare_git(remote, "symbolic-ref", "HEAD", "refs/heads/tickets")

    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-tags",
            "--single-branch",
            "--branch",
            "tickets",
            f"file://{remote}",
            str(scratch),
        ],
        check=True,
    )
    _git(scratch, "config", "user.name", "Reclaim Test")
    _git(scratch, "config", "user.email", "reclaim@example.com")
    assert _git(scratch, "rev-parse", "HEAD").stdout.strip() == old_tip
    assert (
        _git(
            scratch, "ls-tree", "-r", "--name-only", old_tip, "--", ".bridge_state.bak-retarget/"
        ).stdout.strip()
        == ""
    )
    assert _git(scratch, "show-ref", "--verify", "--quiet", PROTECTED_TAG, check=False).returncode
    assert _git(scratch, "config", "--get", "remote.origin.promisor", check=False).returncode
    return ReclaimFixture(remote, scratch, old_tip, protected_tag_tip, state_files)


def _run_script(
    fixture: ReclaimFixture,
    *,
    max_pack_bytes: int = PACK_CEILING,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), "reclaim_bridge_history.py is not implemented"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(fixture.scratch),
            "--source-ref",
            "tickets",
            "--output-ref",
            "rewritten-tickets",
            "--max-pack-bytes",
            str(max_pack_bytes),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _ref(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _bare_ref(repo: Path, ref: str) -> str:
    return _bare_git(repo, "rev-parse", ref).stdout.strip()


def _count(repo: Path, ref: str, *, merges: bool = False) -> int:
    args = ["rev-list", "--count"]
    if merges:
        args.append("--merges")
    args.append(ref)
    return int(_git(repo, *args).stdout.strip())


def _pack_bytes(repo: Path) -> int:
    _git(repo, "repack", "-Ad")
    return sum(path.stat().st_size for path in (repo / ".git" / "objects" / "pack").glob("*.pack"))


def test_dry_run_preserves_graph_and_head_tree_without_publishing(
    reclaim_fixture: ReclaimFixture, tmp_path: Path
) -> None:
    fixture = reclaim_fixture
    source_before = _bare_ref(fixture.remote, "refs/heads/tickets")
    tag_before = _bare_ref(fixture.remote, PROTECTED_TAG)
    old_commits = _count(fixture.scratch, fixture.old_tip)
    old_merges = _count(fixture.scratch, fixture.old_tip, merges=True)
    old_tree = _ref(fixture.scratch, f"{fixture.old_tip}^{{tree}}")

    completed = _run_script(fixture)

    assert completed.returncode == 0, completed.stderr
    assert "DRY RUN SUCCESS" in completed.stdout
    rewritten = _ref(fixture.scratch, "rewritten-tickets")
    assert _count(fixture.scratch, rewritten) == old_commits
    assert _count(fixture.scratch, rewritten, merges=True) == old_merges
    assert _ref(fixture.scratch, f"{rewritten}^{{tree}}") == old_tree
    assert (
        _git(
            fixture.scratch,
            "log",
            "--format=%H",
            rewritten,
            "--",
            ".bridge_state.bak-retarget/",
        ).stdout.strip()
        == ""
    )
    for path, content in fixture.state_files.items():
        assert _git(fixture.scratch, "show", f"{rewritten}:{path}").stdout == content
    assert _bare_ref(fixture.remote, "refs/heads/tickets") == source_before == fixture.old_tip
    assert _bare_ref(fixture.remote, PROTECTED_TAG) == tag_before == fixture.protected_tag_tip
    assert (
        _git(
            fixture.scratch, "show-ref", "--verify", "--quiet", PROTECTED_TAG, check=False
        ).returncode
        != 0
    )

    measurement = tmp_path / "measurement"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-tags",
            "--single-branch",
            "--branch",
            "rewritten-tickets",
            f"file://{fixture.scratch}",
            str(measurement),
        ],
        check=True,
    )
    assert _pack_bytes(measurement) <= PACK_CEILING


def test_partial_clone_refusal_names_fresh_unfiltered_clone_remedy(
    reclaim_fixture: ReclaimFixture,
) -> None:
    fixture = reclaim_fixture
    _git(fixture.scratch, "config", "remote.origin.promisor", "true")
    _git(fixture.scratch, "config", "remote.origin.partialclonefilter", "blob:none")

    completed = _run_script(fixture)

    assert completed.returncode != 0
    assert "fresh unfiltered clone" in completed.stderr.lower()
    assert _git(
        fixture.scratch,
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/rewritten-tickets",
        check=False,
    ).returncode


def test_all_assertions_run_before_success_is_reported(
    reclaim_fixture: ReclaimFixture,
) -> None:
    completed = _run_script(reclaim_fixture, max_pack_bytes=1)

    assert completed.returncode != 0
    assert "pack" in completed.stderr.lower()
    assert "DRY RUN SUCCESS" not in completed.stdout


def test_cli_never_invokes_push_and_leaves_source_refs_exact(
    reclaim_fixture: ReclaimFixture, tmp_path: Path
) -> None:
    fixture = reclaim_fixture
    git_executable = shutil.which("git")
    assert git_executable is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    record = tmp_path / "git-calls"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GIT_RECORD"\nexec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    source_before = _bare_ref(fixture.remote, "refs/heads/tickets")
    tag_before = _bare_ref(fixture.remote, PROTECTED_TAG)
    environment = {
        "PATH": f"{wrapper_dir}:{Path(git_executable).parent}:/usr/bin:/bin",
        "REAL_GIT": git_executable,
        "GIT_RECORD": str(record),
        "LC_ALL": "C",
    }

    completed = _run_script(fixture, env=environment)

    assert completed.returncode == 0, completed.stderr
    calls = record.read_text(encoding="utf-8").splitlines()
    assert not [call for call in calls if re.match(r"(?:-C\s+\S+\s+)?push(?:\s|$)", call)]
    assert _bare_ref(fixture.remote, "refs/heads/tickets") == source_before
    assert _bare_ref(fixture.remote, PROTECTED_TAG) == tag_before


def test_unrelated_remote_ref_movement_does_not_fail_dry_run(
    reclaim_fixture: ReclaimFixture, tmp_path: Path
) -> None:
    fixture = reclaim_fixture
    git_executable = shutil.which("git")
    assert git_executable is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    counter = tmp_path / "ls-remote-count"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *'ls-remote origin'*)\n"
        "    count=0\n"
        '    test ! -f "$LS_REMOTE_COUNT" || count=$(cat "$LS_REMOTE_COUNT")\n'
        "    count=$((count + 1))\n"
        '    printf \'%s\\n\' "$count" > "$LS_REMOTE_COUNT"\n'
        '    if test "$count" -eq 2; then\n'
        '      "$REAL_GIT" -C "$REMOTE_REPO" update-ref refs/heads/unrelated "$UNRELATED_TIP"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    source_before = _bare_ref(fixture.remote, "refs/heads/tickets")
    tag_before = _bare_ref(fixture.remote, PROTECTED_TAG)
    environment = {
        "PATH": f"{wrapper_dir}:{Path(git_executable).parent}:/usr/bin:/bin",
        "REAL_GIT": git_executable,
        "REMOTE_REPO": str(fixture.remote),
        "UNRELATED_TIP": fixture.protected_tag_tip,
        "LS_REMOTE_COUNT": str(counter),
        "LC_ALL": "C",
    }

    completed = _run_script(fixture, env=environment)

    assert _bare_ref(fixture.remote, "refs/heads/unrelated") == fixture.protected_tag_tip
    assert completed.returncode == 0, completed.stderr
    assert _bare_ref(fixture.remote, "refs/heads/tickets") == source_before
    assert _bare_ref(fixture.remote, PROTECTED_TAG) == tag_before


def test_rewritten_branch_accepts_small_representative_writer_push(
    reclaim_fixture: ReclaimFixture, tmp_path: Path
) -> None:
    fixture = reclaim_fixture
    completed = _run_script(fixture)
    assert completed.returncode == 0, completed.stderr
    rewritten = _ref(fixture.scratch, "rewritten-tickets")
    assert _git(
        fixture.scratch,
        "merge-base",
        "--is-ancestor",
        fixture.old_tip,
        rewritten,
        check=False,
    ).returncode

    published = tmp_path / "published.git"
    writer = tmp_path / "writer"
    published.mkdir()
    _git(published, "init", "-q", "--bare")
    _git(
        fixture.scratch,
        "push",
        "-q",
        str(published),
        "refs/heads/rewritten-tickets:refs/heads/tickets",
    )
    _bare_git(published, "symbolic-ref", "HEAD", "refs/heads/tickets")
    _bare_git(published, "repack", "-Ad")
    before = (
        int(
            next(
                line.split(": ", 1)[1]
                for line in _bare_git(published, "count-objects", "-v").stdout.splitlines()
                if line.startswith("size-pack: ")
            )
        )
        * 1024
    )
    subprocess.run(["git", "clone", "-q", f"file://{published}", str(writer)], check=True)
    _git(writer, "config", "user.name", "Review Bot Probe")
    _git(writer, "config", "user.email", "joeoakhart+bot@navapbc.com")
    _write(
        writer,
        "review-probe/1-00000000-0000-4000-8000-000000000000-CODE_REVIEW.json",
        '{"event_type": "CODE_REVIEW", "probe": true}\n',
    )
    _commit(writer, "append representative review-bot event")
    _git(writer, "push", "-q", "origin", "tickets")
    _bare_git(published, "repack", "-Ad")
    after = (
        int(
            next(
                line.split(": ", 1)[1]
                for line in _bare_git(published, "count-objects", "-v").stdout.splitlines()
                if line.startswith("size-pack: ")
            )
        )
        * 1024
    )
    # ``count-objects size-pack`` is reported in whole KiB, so a representative
    # sub-KiB event may legitimately round to zero growth.
    assert 0 <= after - before < 1024 * 1024
