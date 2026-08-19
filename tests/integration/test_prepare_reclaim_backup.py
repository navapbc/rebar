from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from _topology_template import clone_topology_template

SCRIPT = Path(__file__).parents[2] / "infra" / "scripts" / "prepare_reclaim_backup.py"
HELPER_PREFIX = "refs/reclaim-backup/"


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    )


def _bare_git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
    )


def _write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _remote_snapshot(repo: Path) -> str:
    return _git(repo, "ls-remote", "--heads", "--tags", "origin").stdout


def _helper_refs(repo: Path) -> dict[str, str]:
    output = _git(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        HELPER_PREFIX,
    ).stdout
    return dict(line.split(" ", 1) for line in output.splitlines() if line)


@dataclass(frozen=True)
class BackupFixture:
    remote: Path
    repo: Path
    old_tip: str
    ancestor_tip: str
    descendant_tip: str
    diverged_tip: str
    unrelated_tip: str
    annotated_tag_object: str
    blob_oid: str
    local_pin_tip: str


def _clone(remote: Path, destination: Path, local_pin_tip: str) -> Path:
    subprocess.run(
        ["git", "clone", "-q", f"file://{remote}", str(destination)],
        check=True,
    )
    _git(destination, "config", "user.name", "Backup Test")
    _git(destination, "config", "user.email", "backup@example.com")
    _git(destination, "tag", "local-pin", local_pin_tip)
    return destination


def _build_backup_fixture(workspace: Path, topology: Path) -> BackupFixture:
    seed = workspace / "seed"
    remote = topology / "source.git"
    repo = topology / "repo"
    topology.mkdir()
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.name", "Backup Test")
    _git(seed, "config", "user.email", "backup@example.com")

    _write(seed, "events/base.json", '{"event": "base"}\n')
    ancestor_tip = _commit(seed, "base")
    _git(seed, "branch", "ancestor", ancestor_tip)
    _git(seed, "branch", "tickets", ancestor_tip)

    _git(seed, "switch", "-q", "tickets")
    _write(seed, "events/ticket.json", '{"event": "tickets"}\n')
    old_tip = _commit(seed, "tickets tip")

    _git(seed, "switch", "-q", "-c", "descendant")
    _write(seed, "events/descendant.json", '{"event": "descendant"}\n')
    descendant_tip = _commit(seed, "descendant")

    _git(seed, "switch", "-q", "-c", "diverged", ancestor_tip)
    _write(seed, "events/diverged.json", '{"event": "diverged"}\n')
    diverged_tip = _commit(seed, "diverged")

    _git(seed, "switch", "-q", "--orphan", "unrelated")
    _write(seed, "events/unrelated.json", '{"event": "unrelated"}\n')
    unrelated_tip = _commit(seed, "unrelated root")

    _git(seed, "tag", "-a", "annotated-pin", ancestor_tip, "-m", "annotated pin")
    annotated_tag_object = _git(seed, "rev-parse", "refs/tags/annotated-pin").stdout.strip()
    blob_oid = _git(seed, "hash-object", "-w", "--stdin", input_text="blob pin\n").stdout.strip()
    _git(seed, "update-ref", "refs/tags/blob-pin", blob_oid)

    remote.mkdir()
    _git(remote, "init", "-q", "--bare")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "--all", "origin")
    _git(seed, "push", "-q", "--tags", "origin")
    _bare_git(remote, "symbolic-ref", "HEAD", "refs/heads/tickets")

    _clone(remote, repo, diverged_tip)
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == old_tip
    assert _remote_snapshot(repo)
    return BackupFixture(
        remote=remote,
        repo=repo,
        old_tip=old_tip,
        ancestor_tip=ancestor_tip,
        descendant_tip=descendant_tip,
        diverged_tip=diverged_tip,
        unrelated_tip=unrelated_tip,
        annotated_tag_object=annotated_tag_object,
        blob_oid=blob_oid,
        local_pin_tip=diverged_tip,
    )


@pytest.fixture(scope="session")
def _backup_fixture_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, BackupFixture]:
    workspace = tmp_path_factory.mktemp("backup-fixture-template")
    topology = workspace / "topology"
    return topology, _build_backup_fixture(workspace, topology)


@pytest.fixture
def backup_fixture(
    _backup_fixture_template: tuple[Path, BackupFixture],
    tmp_path: Path,
) -> BackupFixture:
    template, fixture = _backup_fixture_template
    topology = clone_topology_template(template, tmp_path / "backup-topology")
    return BackupFixture(
        remote=topology / "source.git",
        repo=topology / "repo",
        old_tip=fixture.old_tip,
        ancestor_tip=fixture.ancestor_tip,
        descendant_tip=fixture.descendant_tip,
        diverged_tip=fixture.diverged_tip,
        unrelated_tip=fixture.unrelated_tip,
        annotated_tag_object=fixture.annotated_tag_object,
        blob_oid=fixture.blob_oid,
        local_pin_tip=fixture.local_pin_tip,
    )


def _run(
    fixture: BackupFixture,
    bundle: Path,
    manifest: Path,
    *,
    repo: Path | None = None,
    env: dict[str, str] | None = None,
    old_tip: str | None = None,
    pins: tuple[str, ...] = (
        "refs/tags/annotated-pin",
        "refs/tags/local-pin",
    ),
) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.is_file(), "prepare_reclaim_backup.py is not implemented"
    command = [
        sys.executable,
        str(SCRIPT),
        "--repo",
        str(repo or fixture.repo),
        "--old-tip",
        old_tip or fixture.old_tip,
        "--bundle",
        str(bundle),
        "--manifest",
        str(manifest),
    ]
    for pin in pins:
        command.extend(("--pin-ref", pin))
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _bundle_heads(repo: Path, bundle: Path) -> dict[str, str]:
    output = _git(repo, "bundle", "list-heads", str(bundle)).stdout
    return dict(line.split(" ", 1)[::-1] for line in output.splitlines() if line)


def test_prepares_manifest_bundle_and_exact_restore_without_remote_mutation(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    output = tmp_path / "off-repo"
    bundle = output / "tickets.bundle"
    manifest = output / "tickets-manifest.json"
    remote_before = _remote_snapshot(fixture.repo)

    completed = _run(fixture, bundle, manifest)

    assert completed.returncode == 0, completed.stderr
    assert "BACKUP READY" in completed.stdout
    assert bundle.is_file()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["old_tip"] == fixture.old_tip
    refs = {item["ref"]: item for item in data["remote_refs"]}
    assert refs["refs/heads/tickets"]["relationship"] == "same"
    assert refs["refs/heads/ancestor"]["relationship"] == "ref-is-ancestor"
    assert refs["refs/heads/descendant"]["relationship"] == "old-tip-is-ancestor"
    assert refs["refs/heads/diverged"]["relationship"] == "diverged-with-merge-base"
    assert refs["refs/heads/unrelated"]["relationship"] == "unrelated"
    annotated = refs["refs/tags/annotated-pin"]
    assert annotated["direct_oid"] == fixture.annotated_tag_object
    assert annotated["peeled_oid"] == fixture.ancestor_tip
    assert annotated["relationship"] == "ref-is-ancestor"
    assert refs["refs/tags/blob-pin"]["relationship"] == "non-commit"

    expected_oids = {
        fixture.old_tip,
        fixture.ancestor_tip,
        fixture.local_pin_tip,
    }
    assert set(data["bundle_refs"].values()) == expected_oids
    assert set(_bundle_heads(fixture.repo, bundle).values()) == expected_oids
    assert _git(fixture.repo, "bundle", "verify", str(bundle)).returncode == 0

    restored = tmp_path / "restored.git"
    restored.mkdir()
    _git(restored, "init", "-q", "--bare")
    for bundle_ref, oid in data["bundle_refs"].items():
        restored_ref = "refs/restored/" + bundle_ref.removeprefix(HELPER_PREFIX)
        _bare_git(restored, "fetch", "-q", str(bundle), f"{bundle_ref}:{restored_ref}")
        assert _bare_git(restored, "rev-parse", restored_ref).stdout.strip() == oid

    assert _remote_snapshot(fixture.repo) == remote_before
    assert _helper_refs(fixture.repo) == {}
    assert _git(fixture.repo, "status", "--porcelain").stdout == ""


def test_refuses_partial_clone_before_creating_refs_or_artifacts(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    _git(fixture.repo, "config", "remote.origin.promisor", "true")
    _git(fixture.repo, "config", "remote.origin.partialclonefilter", "blob:none")
    bundle = tmp_path / "partial.bundle"
    manifest = tmp_path / "partial.json"

    completed = _run(fixture, bundle, manifest)

    assert completed.returncode != 0
    assert "fresh unfiltered clone" in completed.stderr.lower()
    assert not bundle.exists()
    assert not manifest.exists()
    assert _helper_refs(fixture.repo) == {}


def test_refuses_worktree_gitdir_and_existing_output_destinations(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    sibling = tmp_path / "sibling"
    _git(fixture.repo, "worktree", "add", "-q", "-b", "sibling", str(sibling), fixture.old_tip)
    outside = tmp_path / "outside"

    inside_worktree = _run(
        fixture,
        sibling / "backup.bundle",
        outside / "manifest-a.json",
    )
    assert inside_worktree.returncode != 0
    assert "registered worktree" in inside_worktree.stderr.lower()

    git_dir = Path(_git(fixture.repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    inside_gitdir = _run(
        fixture,
        outside / "backup-b.bundle",
        git_dir / "manifest.json",
    )
    assert inside_gitdir.returncode != 0
    assert "git directory" in inside_gitdir.stderr.lower()

    outside.mkdir(exist_ok=True)
    existing_bundle = outside / "existing.bundle"
    existing_bundle.write_text("operator data\n", encoding="utf-8")
    existing = _run(fixture, existing_bundle, outside / "manifest-c.json")
    assert existing.returncode != 0
    assert existing_bundle.read_text(encoding="utf-8") == "operator data\n"

    existing_manifest = outside / "existing.json"
    existing_manifest.write_text("operator metadata\n", encoding="utf-8")
    new_bundle = outside / "backup-d.bundle"
    existing = _run(fixture, new_bundle, existing_manifest)
    assert existing.returncode != 0
    assert existing_manifest.read_text(encoding="utf-8") == "operator metadata\n"
    assert not new_bundle.exists()
    assert _helper_refs(fixture.repo) == {}


def test_records_no_push_and_preserves_exact_remote_snapshot(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    record = tmp_path / "git-calls"
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GIT_RECORD"\nexec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = {
        "PATH": f"{wrapper_dir}:{Path(real_git).parent}:/usr/bin:/bin",
        "REAL_GIT": real_git,
        "GIT_RECORD": str(record),
        "LC_ALL": "C",
    }
    remote_before = _remote_snapshot(fixture.repo)

    completed = _run(
        fixture,
        tmp_path / "recorded.bundle",
        tmp_path / "recorded.json",
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    calls = record.read_text(encoding="utf-8").splitlines()
    assert not [
        call
        for call in calls
        if re.search(r"(?:^|\s)(?:push|update-ref\s+refs/remotes/)(?:\s|$)", call)
    ]
    assert _remote_snapshot(fixture.repo) == remote_before


def test_verification_failure_cleans_owned_state_and_retry_succeeds(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *'bundle verify'*) printf '%s\\n' 'injected verify failure' >&2; exit 42 ;;\n"
        "esac\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = {
        "PATH": f"{wrapper_dir}:{Path(real_git).parent}:/usr/bin:/bin",
        "REAL_GIT": real_git,
        "LC_ALL": "C",
    }
    bundle = tmp_path / "retry.bundle"
    manifest = tmp_path / "retry.json"

    failed = _run(fixture, bundle, manifest, env=environment)

    assert failed.returncode != 0
    assert "verify" in failed.stderr.lower()
    assert not bundle.exists()
    assert not manifest.exists()
    assert _helper_refs(fixture.repo) == {}

    retried = _run(fixture, bundle, manifest)
    assert retried.returncode == 0, retried.stderr
    assert bundle.is_file()
    assert manifest.is_file()


def test_stale_helper_ref_is_preserved_and_fresh_clone_retry_succeeds(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    stale_ref = HELPER_PREFIX + "abandoned/old-tip"
    _git(fixture.repo, "update-ref", stale_ref, fixture.old_tip)
    bundle = tmp_path / "stale.bundle"
    manifest = tmp_path / "stale.json"

    refused = _run(fixture, bundle, manifest)

    assert refused.returncode != 0
    assert "fresh" in refused.stderr.lower()
    assert _helper_refs(fixture.repo) == {stale_ref: fixture.old_tip}
    assert not bundle.exists()
    assert not manifest.exists()

    fresh_repo = _clone(fixture.remote, tmp_path / "fresh", fixture.local_pin_tip)
    retried = _run(fixture, bundle, manifest, repo=fresh_repo)
    assert retried.returncode == 0, retried.stderr
    assert _helper_refs(fresh_repo) == {}


def test_refuses_stale_old_tip_before_creating_state(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    bundle = tmp_path / "stale-tip.bundle"
    manifest = tmp_path / "stale-tip.json"

    completed = _run(
        fixture,
        bundle,
        manifest,
        old_tip=fixture.ancestor_tip,
    )

    assert completed.returncode != 0
    assert "old-tip differs" in completed.stderr.lower()
    assert not bundle.exists()
    assert not manifest.exists()
    assert _helper_refs(fixture.repo) == {}


def test_refuses_noncommit_and_nonref_pins(backup_fixture: BackupFixture, tmp_path: Path) -> None:
    fixture = backup_fixture

    noncommit = _run(
        fixture,
        tmp_path / "blob.bundle",
        tmp_path / "blob.json",
        pins=("refs/tags/blob-pin",),
    )
    assert noncommit.returncode != 0
    assert "not a commit" in noncommit.stderr.lower()

    nonref = _run(
        fixture,
        tmp_path / "oid.bundle",
        tmp_path / "oid.json",
        pins=(fixture.old_tip,),
    )
    assert nonref.returncode != 0
    assert "must name a ref" in nonref.stderr.lower()
    assert _helper_refs(fixture.repo) == {}


def test_refuses_dirty_and_shallow_clones_before_creating_state(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    dirty_marker = fixture.repo / "operator-data.txt"
    dirty_marker.write_text("untracked\n", encoding="utf-8")

    dirty = _run(
        fixture,
        tmp_path / "dirty.bundle",
        tmp_path / "dirty.json",
    )
    assert dirty.returncode != 0
    assert "not clean" in dirty.stderr.lower()
    assert _helper_refs(fixture.repo) == {}
    dirty_marker.unlink()

    shallow_repo = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "tickets",
            f"file://{fixture.remote}",
            str(shallow_repo),
        ],
        check=True,
    )
    assert _git(shallow_repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"

    shallow = _run(
        fixture,
        tmp_path / "shallow.bundle",
        tmp_path / "shallow.json",
        repo=shallow_repo,
    )
    assert shallow.returncode != 0
    assert "fresh unfiltered clone" in shallow.stderr.lower()
    assert _helper_refs(shallow_repo) == {}


def test_remote_snapshot_change_cleans_outputs_and_owned_refs(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    counter = tmp_path / "ls-remote-count"
    wrapper.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *'ls-remote --heads --tags origin'*)\n"
        "    count=0\n"
        '    if [ -f "$COUNT_FILE" ]; then count=$(cat "$COUNT_FILE"); fi\n'
        "    count=$((count + 1))\n"
        '    printf \'%s\\n\' "$count" > "$COUNT_FILE"\n'
        '    if [ "$count" -eq 2 ]; then\n'
        '      "$REAL_GIT" -C "$REMOTE_REPO" update-ref '
        'refs/heads/tickets "$NEXT_TIP"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = {
        "PATH": f"{wrapper_dir}:{Path(real_git).parent}:/usr/bin:/bin",
        "REAL_GIT": real_git,
        "COUNT_FILE": str(counter),
        "REMOTE_REPO": str(fixture.remote),
        "NEXT_TIP": fixture.descendant_tip,
        "LC_ALL": "C",
    }
    bundle = tmp_path / "moving.bundle"
    manifest = tmp_path / "moving.json"

    completed = _run(fixture, bundle, manifest, env=environment)

    assert completed.returncode != 0
    assert "snapshot changed" in completed.stderr.lower()
    assert not bundle.exists()
    assert not manifest.exists()
    assert _helper_refs(fixture.repo) == {}


def test_refuses_identical_artifact_paths_before_creating_state(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    artifact = tmp_path / "same-output"

    completed = _run(fixture, artifact, artifact)

    assert completed.returncode != 0
    assert "paths must differ" in completed.stderr.lower()
    assert not artifact.exists()
    assert _helper_refs(fixture.repo) == {}


def test_refuses_empty_and_ticketsless_origins(
    backup_fixture: BackupFixture, tmp_path: Path
) -> None:
    fixture = backup_fixture
    empty_remote = tmp_path / "empty.git"
    empty_remote.mkdir()
    _git(empty_remote, "init", "-q", "--bare")
    _git(fixture.repo, "remote", "set-url", "origin", str(empty_remote))

    empty = _run(
        fixture,
        tmp_path / "empty.bundle",
        tmp_path / "empty.json",
        pins=(),
    )
    assert empty.returncode != 0
    assert "no heads or tags" in empty.stderr.lower()
    assert _helper_refs(fixture.repo) == {}

    _git(fixture.repo, "remote", "set-url", "origin", str(fixture.remote))
    _bare_git(fixture.remote, "update-ref", "-d", "refs/heads/tickets")
    ticketsless = _run(
        fixture,
        tmp_path / "ticketsless.bundle",
        tmp_path / "ticketsless.json",
        pins=(),
    )
    assert ticketsless.returncode != 0
    assert "no refs/heads/tickets" in ticketsless.stderr.lower()
    assert _helper_refs(fixture.repo) == {}
