from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
SCRIPTS = Path(__file__).parents[2] / "infra" / "scripts"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(SCRIPTS))
import comment_echo_reclaim_manifest as manifest_builder  # noqa: E402
import prepare_reclaim_backup as backup_builder  # noqa: E402
import test_comment_echo_reclaim_manifest as manifest_cases  # noqa: E402
from _subprocess_env import subprocess_env  # noqa: E402

from rebar.reducer import reduce_ticket  # noqa: E402

SCRIPT = SCRIPTS / "reclaim_comment_echo_history.py"
OUTPUT_REF = "refs/heads/reclaim-candidate/comment-echo-test"
OTHER_TICKET = "9999-aaaa-bbbb-cccc"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _bare_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _ref(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _count(repo: Path, ref: str, *, merges: bool = False) -> int:
    arguments = ["rev-list", "--count"]
    if merges:
        arguments.append("--merges")
    arguments.append(ref)
    return int(_git(repo, *arguments).stdout.strip())


def _run(
    fixture: manifest_cases.Fixture,
    *,
    output_ref: str = OUTPUT_REF,
    commit_map: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(fixture.repo),
            "--manifest",
            str(fixture.output),
            "--output-ref",
            output_ref,
            "--commit-map",
            str(commit_map or fixture.output.with_name("commit-map.json")),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _refresh_backup(fixture: manifest_cases.Fixture) -> None:
    fixture.bundle.unlink()
    fixture.backup_manifest.unlink()
    backup_builder.prepare(
        argparse.Namespace(
            repo=fixture.repo,
            old_tip=fixture.tip,
            bundle=fixture.bundle,
            manifest=fixture.backup_manifest,
            pin_ref=[],
        )
    )


def _augment_with_merge(fixture: manifest_cases.Fixture) -> None:
    repo = fixture.repo
    _git(repo, "switch", "-q", "-c", "side")
    create = manifest_cases._event(
        "CREATE",
        30,
        "00000000-0000-4000-8000-000000000030",
        {"ticket_type": "task", "title": "unaffected ticket"},
    )
    comment = manifest_cases._event(
        "COMMENT",
        31,
        "00000000-0000-4000-8000-000000000031",
        {"body": "unaffected comment"},
    )
    for event in (create, comment):
        manifest_cases._write_json(
            repo
            / OTHER_TICKET
            / f"{event['timestamp']}-{event['uuid']}-{event['event_type']}.json",
            event,
        )
    manifest_cases._commit(repo, "add unaffected side ticket")
    _git(repo, "switch", "-q", "tickets")
    manifest_cases._write_json(repo / ".audit" / "unrelated.json", {"retained": True})
    manifest_cases._commit(repo, "add unrelated mainline audit data")
    _git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge side ticket")
    fixture.tip = _ref(repo, "HEAD")
    _git(repo, "push", "-q", "origin", "tickets")
    _refresh_backup(fixture)


def _tree(repo: Path, ref: str) -> dict[str, str]:
    output = _git(
        repo,
        "ls-tree",
        "-r",
        "--format=%(objectname)%x09%(path)",
        ref,
    ).stdout
    return dict(line.split("\t", 1)[::-1] for line in output.splitlines())


def _commit_record(repo: Path, commit: str) -> tuple[dict[str, str], list[str], str]:
    raw = _git(repo, "cat-file", "commit", commit).stdout
    headers, message = raw.split("\n\n", 1)
    fields: dict[str, str] = {}
    parents: list[str] = []
    for line in headers.splitlines():
        key, _, value = line.partition(" ")
        if key == "parent":
            parents.append(value)
        elif key in {"author", "committer", "encoding"}:
            fields[key] = value
    return fields, parents, message


def _expected_target_state(
    state: dict[str, object], manifest: dict[str, object]
) -> dict[str, object]:
    expected = copy.deepcopy(state)
    groups = manifest["groups"]
    assert isinstance(groups, list)
    removed = [item for group in groups for item in group["removed"]]
    removed_ids = {item["event_uuid"] for item in removed}
    removed_timestamps = {item["timestamp"] for item in removed}
    expected["comments"] = [
        item for item in expected["comments"] if item.get("timestamp") not in removed_timestamps
    ]
    expected["authorship_ledger"] = [
        item for item in expected["authorship_ledger"] if item.get("event_uuid") not in removed_ids
    ]
    for signed in (True, False):
        bucket = "signed" if signed else "unsigned"
        expected["authorship"][bucket] -= sum(item["signed"] is signed for item in removed)
    return expected


def _git_wrapper(tmp_path: Path) -> tuple[Path, Path, str]:
    git_executable = shutil.which("git")
    assert git_executable is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    record = tmp_path / "git-calls"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$GIT_RECORD"\n'
        'case " $* " in\n'
        "  *' fast-import '*)\n"
        '    if test -n "$FAIL_FAST_IMPORT_ONCE" && test ! -e "$FAIL_MARKER"; then\n'
        '      : > "$FAIL_MARKER"\n'
        "      exit 91\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper_dir, record, git_executable


def test_rewrite_builds_an_exact_local_candidate_without_moving_source_or_remote(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path)
    manifest = manifest_cases._build_manifest(fixture)
    source_before = _ref(fixture.repo, "refs/heads/tickets")
    remote_before = _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip()
    source_commits = _count(fixture.repo, fixture.tip)
    source_merges = _count(fixture.repo, fixture.tip, merges=True)
    bridge_oid = _ref(fixture.repo, f"{fixture.tip}:.bridge_state/bindings.json")

    completed = _run(fixture)

    assert completed.returncode == 0, completed.stderr
    assert "no push was performed" in completed.stdout
    rewritten = _ref(fixture.repo, OUTPUT_REF)
    commit_map_path = fixture.output.with_name("commit-map.json")
    commit_map = json.loads(commit_map_path.read_bytes())
    assert commit_map["source_tip"] == fixture.tip
    assert commit_map["output_tip"] == rewritten
    assert commit_map["commits"][fixture.tip] == rewritten
    assert len(commit_map["commits"]) == source_commits
    assert _count(fixture.repo, rewritten) == source_commits
    assert _count(fixture.repo, rewritten, merges=True) == source_merges

    group = manifest["groups"][0]
    assert (
        _ref(fixture.repo, f"{rewritten}:{group['survivor']['path']}")
        == group["survivor"]["blob_oid"]
    )
    for removed in group["removed"]:
        for path in removed["path_aliases"]:
            assert (
                _git(
                    fixture.repo,
                    "cat-file",
                    "-e",
                    f"{rewritten}:{path}",
                    check=False,
                ).returncode
                != 0
            )
    for transform in manifest["snapshot_transforms"]:
        assert _ref(fixture.repo, f"{rewritten}:{transform['path']}") == transform["new_blob_oid"]
    compat = manifest["store_compat_transform"]
    assert _ref(fixture.repo, f"{rewritten}:{compat['path']}") == compat["new_blob_oid"]
    assert _ref(fixture.repo, f"{rewritten}:.bridge_state/bindings.json") == bridge_oid
    assert _ref(fixture.repo, "refs/heads/tickets") == source_before == fixture.tip
    assert (
        _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip() == remote_before
    )
    assert _git(fixture.repo, "status", "--porcelain").stdout == ""


def test_rewrite_preserves_graph_metadata_retained_blobs_and_full_replay(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path)
    _augment_with_merge(fixture)
    manifest = manifest_cases._build_manifest(fixture)
    old_target = reduce_ticket(str(fixture.repo / manifest_cases.TICKET))
    old_other = reduce_ticket(str(fixture.repo / OTHER_TICKET))
    assert old_target is not None and old_other is not None
    for cache in fixture.repo.glob("*/.cache.json"):
        cache.unlink()
    assert _git(fixture.repo, "status", "--porcelain").stdout == ""

    completed = _run(fixture)

    assert completed.returncode == 0, completed.stderr
    rewritten = _ref(fixture.repo, OUTPUT_REF)
    commit_map = json.loads(fixture.output.with_name("commit-map.json").read_bytes())["commits"]
    old_commits = _git(
        fixture.repo, "rev-list", "--reverse", "--topo-order", fixture.tip
    ).stdout.splitlines()
    assert set(commit_map) == set(old_commits)
    for old in old_commits:
        old_fields, old_parents, old_message = _commit_record(fixture.repo, old)
        new_fields, new_parents, new_message = _commit_record(fixture.repo, commit_map[old])
        assert new_fields == old_fields
        assert new_message == old_message
        assert new_parents == [commit_map[parent] for parent in old_parents]

    allowed = {".store-compat.json"}
    for group in manifest["groups"]:
        for removed in group["removed"]:
            allowed.update(removed["path_aliases"])
    for transform in manifest["snapshot_transforms"]:
        allowed.update(transform["path_aliases"])
    for old, new in commit_map.items():
        changed = set(
            _git(
                fixture.repo,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                old,
                new,
            ).stdout.splitlines()
        )
        assert changed <= allowed
        if old != fixture.tip:
            assert ".store-compat.json" not in changed

    old_tree = _tree(fixture.repo, fixture.tip)
    new_tree = _tree(fixture.repo, rewritten)
    assert {path: oid for path, oid in old_tree.items() if path not in allowed} == {
        path: oid for path, oid in new_tree.items() if path not in allowed
    }
    assert old_tree[".bridge_state/bindings.json"] == new_tree[".bridge_state/bindings.json"]

    after = tmp_path / "after"
    _git(fixture.repo, "worktree", "add", "-q", "--detach", str(after), rewritten)
    try:
        new_target = reduce_ticket(str(after / manifest_cases.TICKET))
        new_other = reduce_ticket(str(after / OTHER_TICKET))
        assert new_target == _expected_target_state(old_target, manifest)
        assert new_other == old_other
    finally:
        _git(fixture.repo, "worktree", "remove", "--force", str(after))
    _git(fixture.repo, "fsck", "--strict", rewritten)


def test_failed_import_cleans_local_refs_and_retry_succeeds_without_push(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest_cases._build_manifest(fixture)
    wrapper_dir, record, git_executable = _git_wrapper(tmp_path)
    fail_marker = tmp_path / "failed-once"
    environment = subprocess_env(
        {
            "PATH": f"{wrapper_dir}:{Path(git_executable).parent}:/usr/bin:/bin",
            "REAL_GIT": git_executable,
            "GIT_RECORD": str(record),
            "FAIL_FAST_IMPORT_ONCE": "1",
            "FAIL_MARKER": str(fail_marker),
            "LC_ALL": "C",
        }
    )
    remote_before = _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip()

    failed = _run(fixture, env=environment)

    assert failed.returncode != 0
    assert not fixture.output.with_name("commit-map.json").exists()
    assert (
        _git(
            fixture.repo,
            "show-ref",
            "--verify",
            "--quiet",
            OUTPUT_REF,
            check=False,
        ).returncode
        != 0
    )
    assert not _git(
        fixture.repo, "for-each-ref", "--format=%(refname)", "refs/reclaim-comment-echo/"
    ).stdout.splitlines()

    succeeded = _run(fixture, env=environment)

    assert succeeded.returncode == 0, succeeded.stderr
    calls = record.read_text(encoding="utf-8").splitlines()
    assert not [call for call in calls if re.search(r"(?:^|\s)push(?:\s|$)", call)]
    assert not [call for call in calls if " cat-file commit " in f" {call} "]
    assert sum(" cat-file --batch" in call for call in calls) == 1
    assert (
        _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip() == remote_before
    )


def test_manifest_source_and_remote_drift_refuse_without_a_candidate(
    tmp_path: Path,
) -> None:
    invalid = manifest_cases._build_fixture(tmp_path / "invalid")
    manifest_cases._build_manifest(invalid)
    document = json.loads(invalid.output.read_bytes())
    document["manifest_digest"] = "0" * 64
    invalid.output.write_bytes(manifest_builder._canonical(document) + b"\n")
    invalid_result = _run(invalid)
    assert invalid_result.returncode != 0
    assert "manifest" in invalid_result.stderr.lower()

    stale = manifest_cases._build_fixture(tmp_path / "stale")
    manifest_cases._build_manifest(stale)
    manifest_cases._write_json(stale.repo / ".audit" / "later.json", {"later": True})
    manifest_cases._commit(stale.repo, "advance source after manifest")
    stale_result = _run(stale)
    assert stale_result.returncode != 0
    assert "tip" in stale_result.stderr.lower()

    moved = manifest_cases._build_fixture(tmp_path / "moved")
    manifest_cases._build_manifest(moved)
    _bare_git(moved.remote, "update-ref", "refs/heads/unrelated", f"{moved.tip}^")
    moved_result = _run(moved)
    assert moved_result.returncode != 0
    assert "remote" in moved_result.stderr.lower()

    for fixture in (invalid, stale, moved):
        assert not fixture.output.with_name("commit-map.json").exists()
        assert (
            _git(
                fixture.repo,
                "show-ref",
                "--verify",
                "--quiet",
                OUTPUT_REF,
                check=False,
            ).returncode
            != 0
        )


def test_remote_movement_during_rewrite_cleans_candidate_and_map(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest_cases._build_manifest(fixture)
    wrapper_dir, _, git_executable = _git_wrapper(tmp_path)
    counter = tmp_path / "ls-remote-count"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        "  *' ls-remote --heads --tags origin '*)\n"
        "    count=0\n"
        '    test ! -f "$LS_REMOTE_COUNT" || count=$(cat "$LS_REMOTE_COUNT")\n'
        "    count=$((count + 1))\n"
        '    printf "%s\\n" "$count" > "$LS_REMOTE_COUNT"\n'
        '    if test "$count" -eq 2; then\n'
        '      "$REAL_GIT" --git-dir "$REMOTE_REPO" update-ref refs/heads/unrelated "$MOVE_TIP"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    environment = subprocess_env(
        {
            "PATH": f"{wrapper_dir}:{Path(git_executable).parent}:/usr/bin:/bin",
            "REAL_GIT": git_executable,
            "REMOTE_REPO": str(fixture.remote),
            "MOVE_TIP": f"{fixture.tip}^",
            "LS_REMOTE_COUNT": str(counter),
            "LC_ALL": "C",
        }
    )

    completed = _run(fixture, env=environment)

    assert completed.returncode != 0
    assert "moved" in completed.stderr.lower()
    assert not fixture.output.with_name("commit-map.json").exists()
    assert (
        _git(
            fixture.repo,
            "show-ref",
            "--verify",
            "--quiet",
            OUTPUT_REF,
            check=False,
        ).returncode
        != 0
    )


def test_backup_bundle_restores_the_exact_old_tip_after_candidate(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path)
    manifest_cases._build_manifest(fixture)
    completed = _run(fixture)
    assert completed.returncode == 0, completed.stderr
    published = tmp_path / "published.git"
    published.mkdir()
    _git(published, "init", "-q", "--bare")
    _bare_git(
        published,
        "fetch",
        "-q",
        str(fixture.repo),
        f"{OUTPUT_REF}:refs/heads/tickets",
    )
    assert _bare_git(published, "rev-parse", "refs/heads/tickets").stdout.strip() != fixture.tip
    backup = json.loads(fixture.backup_manifest.read_bytes())
    old_tip_ref = next(ref for ref in backup["bundle_refs"] if ref.endswith("/old-tip"))

    _bare_git(
        published,
        "fetch",
        "-q",
        "--force",
        str(fixture.bundle),
        f"{old_tip_ref}:refs/heads/tickets",
    )

    assert _bare_git(published, "rev-parse", "refs/heads/tickets").stdout.strip() == fixture.tip
    _bare_git(published, "fsck", "--strict")
