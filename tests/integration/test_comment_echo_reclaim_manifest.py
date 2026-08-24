from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[2] / "infra" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import comment_echo_reclaim_manifest as manifest_builder  # noqa: E402
import prepare_reclaim_backup as backup_builder  # noqa: E402

from rebar.reducer import reduce_ticket  # noqa: E402

TICKET = "1111-2222-3333-4444"
JIRA_KEY = "REB-1"
BODY = "synthetic reconciler echo body"
BODY_HASH = hashlib.sha256(BODY.encode()).hexdigest()
GROUPS = {TICKET: (BODY_HASH,)}
CLEANUP_AUTHORITY = {
    (TICKET, BODY_HASH): {
        "jira_key": JIRA_KEY,
        "survivor_id": "200",
        "delete_id": "300",
        "author_account_id": "reconciler-account",
        "post_run_id": "synthetic-post-run",
        "import_run_id": "synthetic-import-run",
        "import_commit": "a" * 40,
    }
}


def _adf(text: str) -> dict[str, object]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


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


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manifest_builder._canonical(value))


def _event(
    event_type: str,
    timestamp: int,
    uuid: str,
    data: dict[str, object],
    *,
    author: str = "fixture",
    env_id: str = "fixture",
    signed: bool = False,
) -> dict[str, object]:
    event: dict[str, object] = {
        "author": author,
        "author_email": "fixture@example.com",
        "author_id": "fixture-author",
        "data": data,
        "env_id": env_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "uuid": uuid,
    }
    if signed:
        event["author_sig"] = "synthetic-signature"
    return event


def _event_path(repo: Path, event: dict[str, object], *, retired: bool = False) -> Path:
    suffix = ".retired" if retired else ""
    return (
        repo / TICKET / f"{event['timestamp']}-{event['uuid']}-{event['event_type']}.json{suffix}"
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _snapshot(
    repo: Path,
    timestamp: int,
    uuid: str,
    folded_events: list[dict[str, object]],
) -> dict:
    state = reduce_ticket(str(repo / TICKET))
    assert state is not None and state["status"] == "open"
    state = {key: value for key, value in state.items() if key not in {"updated_at", "signature"}}
    state["authorship_ledger"] = [
        {
            "content_hash": hashlib.sha256(manifest_builder._canonical(event)).hexdigest(),
            "event_uuid": event["uuid"],
            "position": {
                "commit_sha": "a" * 40,
                "position": f"{event['timestamp']}-{event['uuid']}",
            },
            "signature": event["author_sig"],
            "signer_pubkey": "synthetic-key",
        }
        for event in folded_events
        if event.get("author_sig")
    ]
    cache = repo / TICKET / ".cache.json"
    cache.unlink(missing_ok=True)
    return _event(
        "SNAPSHOT",
        timestamp,
        uuid,
        {
            "compacted_at": timestamp,
            "compiled_state": state,
            "source_event_uuids": [event["uuid"] for event in folded_events],
        },
    )


@dataclass
class Fixture:
    repo: Path
    remote: Path
    tip: str
    bundle: Path
    backup_manifest: Path
    inventory: Path
    output: Path
    events: list[dict[str, object]]


def _build_fixture(tmp_path: Path, *, conflict_mapping: bool = False) -> Fixture:
    repo = tmp_path / "source"
    remote = tmp_path / "origin.git"
    artifacts = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    remote.mkdir()
    artifacts.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Manifest Fixture")
    _git(repo, "config", "user.email", "fixture@example.com")
    _git(remote, "init", "-q", "--bare")
    _bare_git(remote, "config", "maintenance.auto", "0")
    _bare_git(remote, "config", "gc.auto", "0")
    _git(repo, "remote", "add", "origin", str(remote))

    create = _event(
        "CREATE",
        1,
        "00000000-0000-4000-8000-000000000001",
        {"ticket_type": "task", "title": "fixture"},
    )
    source_comment = _event(
        "COMMENT",
        2,
        "00000000-0000-4000-8000-000000000002",
        {"body": BODY},
        author="human",
        env_id="human",
    )
    echo_oldest = _event(
        "COMMENT",
        3,
        "00000000-0000-4000-8000-000000000003",
        {"body": BODY, "jira_comment_id": "100"},
        author="reconciler",
        env_id="reconciler",
        signed=True,
    )
    echo_survivor = _event(
        "COMMENT",
        4,
        "00000000-0000-4000-8000-000000000004",
        {"body": BODY, "jira_comment_id": "200"},
        author="reconciler",
        env_id="reconciler",
        signed=True,
    )
    first_events = [create, source_comment, echo_oldest, echo_survivor]
    for event in first_events:
        _write_json(_event_path(repo, event), event)
    _write_json(
        repo / ".store-compat.json",
        {"format_version": 1, "required_capabilities": ["multi-project-bridge"]},
    )
    comment_ids = (
        {"2": manifest_builder.UNKNOWN_COMMENT_ID, "3": "300"}
        if conflict_mapping
        else {"2": manifest_builder.UNKNOWN_COMMENT_ID, "4": "200"}
    )
    _write_json(
        repo / ".bridge_state" / "bindings.json",
        {
            "bindings": {
                TICKET: {
                    "created_at": "2026-08-01T00:00:00Z",
                    "jira_key": JIRA_KEY,
                    "state": "confirmed",
                    "updated_at": "2026-08-01T00:00:00Z",
                }
            },
            "comment_ids": comment_ids,
            "reverse": {JIRA_KEY: TICKET},
            "version": 1,
        },
    )
    _commit(repo, "initial events")

    snapshot_one = _snapshot(repo, 10, "00000000-0000-4000-8000-000000000010", first_events)
    for event in first_events:
        _event_path(repo, event).rename(_event_path(repo, event, retired=True))
    _write_json(_event_path(repo, snapshot_one), snapshot_one)
    _commit(repo, "first snapshot")

    echo_same_live_id = _event(
        "COMMENT",
        11,
        "00000000-0000-4000-8000-000000000011",
        {"body": BODY, "jira_comment_id": "200"},
        author="reconciler",
        env_id="reconciler",
        signed=True,
    )
    echo_deleted_id = _event(
        "COMMENT",
        12,
        "00000000-0000-4000-8000-000000000012",
        {"body": BODY, "jira_comment_id": "300"},
        author="reconciler",
        env_id="reconciler",
        signed=True,
    )
    later = [echo_same_live_id, echo_deleted_id]
    for event in later:
        _write_json(_event_path(repo, event), event)
    _commit(repo, "later imports")

    all_events = [*first_events, *later]
    # A recurrent SNAPSHOT inherits the prior compiled state, but its source and
    # authorship provenance enumerate only the raw events folded in this pass.
    snapshot_two = _snapshot(repo, 20, "00000000-0000-4000-8000-000000000020", later)
    _event_path(repo, snapshot_one).rename(_event_path(repo, snapshot_one, retired=True))
    for event in later:
        _event_path(repo, event).rename(_event_path(repo, event, retired=True))
    _write_json(_event_path(repo, snapshot_two), snapshot_two)
    tip = _commit(repo, "second snapshot")
    _git(repo, "branch", "-M", "tickets")
    _git(repo, "push", "-q", "-u", "origin", "tickets")
    _bare_git(remote, "symbolic-ref", "HEAD", "refs/heads/tickets")

    bundle = artifacts / "backup.bundle"
    backup_manifest = artifacts / "backup.json"
    backup_builder.prepare(
        argparse.Namespace(
            repo=repo,
            old_tip=tip,
            bundle=bundle,
            manifest=backup_manifest,
            pin_ref=[],
        )
    )
    inventory = artifacts / "jira.json"
    _write_json(
        inventory,
        {
            "issues": [
                {
                    "key": JIRA_KEY,
                    "pages": [
                        {
                            "comments": [
                                {
                                    "author": {"accountId": "reconciler-account"},
                                    "body": _adf("unrelated"),
                                    "id": "50",
                                },
                                {
                                    "author": {"accountId": "reconciler-account"},
                                    "body": _adf(BODY),
                                    "id": "200",
                                },
                            ],
                            "maxResults": 2,
                            "startAt": 0,
                            "total": 4,
                        },
                        {
                            "comments": [
                                {
                                    "author": {"accountId": "reconciler-account"},
                                    "body": _adf(BODY),
                                    "id": "300",
                                },
                                {
                                    "author": {"accountId": "human-account"},
                                    "body": _adf("also unrelated"),
                                    "id": "400",
                                },
                            ],
                            "maxResults": 2,
                            "startAt": 2,
                            "total": 4,
                        },
                    ],
                }
            ],
            "schema_version": 1,
            "source": "jira-cloud-rest-v3",
        },
    )
    return Fixture(
        repo,
        remote,
        tip,
        bundle,
        backup_manifest,
        inventory,
        artifacts / "manifest.json",
        all_events,
    )


def _args(fixture: Fixture, output: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        repo=fixture.repo,
        source_ref="tickets",
        source_tip=fixture.tip,
        backup_bundle=fixture.bundle,
        backup_manifest=fixture.backup_manifest,
        jira_inventory=fixture.inventory,
        output=output or fixture.output,
    )


def _build_manifest(fixture: Fixture, output: Path | None = None) -> dict[str, object]:
    return manifest_builder.build_manifest(
        _args(fixture, output),
        incident_groups=GROUPS,
        cleanup_authority=CLEANUP_AUTHORITY,
    )


def _refresh_backup(fixture: Fixture) -> None:
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


def test_manifest_binds_the_authorized_jira_cleanup_pair(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)

    manifest = _build_manifest(fixture)

    action = manifest["groups"][0]["jira_cleanup"]
    assert (action["delete"]["id"], action["survivor"]["id"]) == ("300", "200")


def test_manifest_uses_the_incident_cleanup_authority_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _build_fixture(tmp_path)
    monkeypatch.setattr(manifest_builder, "INCIDENT_CLEANUP_AUTHORITY", CLEANUP_AUTHORITY)

    manifest = manifest_builder.build_manifest(_args(fixture), incident_groups=GROUPS)

    assert manifest["groups"][0]["jira_cleanup"]["delete"]["id"] == "300"


def test_manifest_is_deterministic_and_selects_the_live_mapped_identity(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    remote_before = _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip()

    first = _build_manifest(fixture)
    second_output = fixture.output.with_name("manifest-second.json")
    second = _build_manifest(fixture, second_output)

    assert first == second
    assert fixture.output.read_bytes() == second_output.read_bytes()
    assert first["expected_delta"]["groups"] == 1
    assert first["expected_delta"]["removed_events"] == 3
    group = first["groups"][0]
    assert group["candidate_count"] == 4
    assert group["survivor"]["jira_comment_id"] == "200"
    assert group["survivor"]["timestamp"] == 4  # Not the oldest candidate (timestamp 3).
    assert group["selection"] == "bridge-comment-position-and-live-jira-id"
    assert {item["jira_comment_id"] for item in group["removed"]} == {"100", "200", "300"}
    assert len(first["snapshot_transforms"]) == 2
    assert [len(item["remove_event_uuids"]) for item in first["snapshot_transforms"]] == [1, 2]
    assert [len(item["remove_comment_timestamps"]) for item in first["snapshot_transforms"]] == [
        1,
        3,
    ]
    assert first["bridge"]["unknown_id_sentinel"] == manifest_builder.UNKNOWN_COMMENT_ID
    assert first["store_compat_transform"]["epoch"].startswith("comment-echo-reclaim-v1-")
    assert (
        _bare_git(fixture.remote, "rev-parse", "refs/heads/tickets").stdout.strip() == remote_before
    )
    assert _git(fixture.repo, "status", "--porcelain").stdout == ""


def test_manifest_selects_the_live_identity_without_candidate_position_protection(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    bridge_path = fixture.repo / ".bridge_state" / "bindings.json"
    bridge = json.loads(bridge_path.read_bytes())
    bridge["comment_ids"] = {"2": manifest_builder.UNKNOWN_COMMENT_ID}
    _write_json(bridge_path, bridge)
    fixture.tip = _commit(fixture.repo, "leave only source-comment position protection")
    _git(fixture.repo, "push", "-q", "origin", "tickets")
    _refresh_backup(fixture)

    manifest = _build_manifest(fixture)

    group = manifest["groups"][0]
    assert group["selection"] == "live-jira-id-within-identity-lexicographic-tiebreak"
    assert group["survivor"]["jira_comment_id"] == "200"
    assert group["survivor"]["timestamp"] == 11


def test_manifest_normalizes_raw_rest_v3_adf_with_the_production_inbound_path(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)

    manifest = _build_manifest(fixture)

    live = manifest["groups"][0]["live_jira_comment"]
    assert live["id"] == "200"
    assert live["body"]["type"] == "doc"
    assert live["normalized_body"] == BODY


def test_import_does_not_preload_the_engine_top_level_package() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(SCRIPTS)!r}); "
                "import comment_echo_reclaim_manifest; "
                "assert 'rebar_reconciler' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize("replacement", [BODY, {"rendered": f"<p>{BODY}</p>"}])
def test_manifest_refuses_non_adf_or_rendered_jira_body_material(
    tmp_path: Path, replacement: object
) -> None:
    fixture = _build_fixture(tmp_path)
    inventory = json.loads(fixture.inventory.read_bytes())
    inventory["issues"][0]["pages"][0]["comments"][1]["body"] = replacement
    _write_json(fixture.inventory, inventory)

    with pytest.raises(manifest_builder.ReclaimError, match="Jira"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


@pytest.mark.parametrize("failure", ["incomplete", "ambiguous"])
def test_manifest_refuses_incomplete_or_ambiguous_live_jira_population(
    tmp_path: Path, failure: str
) -> None:
    fixture = _build_fixture(tmp_path)
    inventory = json.loads(fixture.inventory.read_bytes())
    pages = inventory["issues"][0]["pages"]
    if failure == "incomplete":
        pages.pop()
    else:
        pages[-1]["comments"][0] = {
            "author": {"accountId": "reconciler-account"},
            "body": _adf(BODY),
            "id": "201",
        }
    _write_json(fixture.inventory, inventory)

    with pytest.raises(manifest_builder.ReclaimError, match="Jira"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


def test_manifest_refuses_a_bridge_mapping_that_would_protect_the_wrong_copy(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path, conflict_mapping=True)

    with pytest.raises(manifest_builder.ReclaimError, match="bridge"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


def test_manifest_refuses_promisor_dirty_shared_blob_and_moving_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirty = _build_fixture(tmp_path / "dirty")
    (dirty.repo / "untracked").write_text("dirty", encoding="utf-8")
    with pytest.raises(manifest_builder.ReclaimError, match="not clean"):
        _build_manifest(dirty)

    promisor = _build_fixture(tmp_path / "promisor")
    _git(promisor.repo, "config", "remote.origin.promisor", "true")
    with pytest.raises(manifest_builder.ReclaimError, match="partial/promisor"):
        _build_manifest(promisor)

    shared = _build_fixture(tmp_path / "shared")
    snapshot = next((shared.repo / TICKET).glob("*-SNAPSHOT.json"))
    copied = shared.repo / "unrelated" / "copied-snapshot.json"
    copied.parent.mkdir()
    shutil.copyfile(snapshot, copied)
    shared.tip = _commit(shared.repo, "share target snapshot blob")
    _git(shared.repo, "push", "-q", "origin", "tickets")
    shared.bundle.unlink()
    shared.backup_manifest.unlink()
    backup_builder.prepare(
        argparse.Namespace(
            repo=shared.repo,
            old_tip=shared.tip,
            bundle=shared.bundle,
            manifest=shared.backup_manifest,
            pin_ref=[],
        )
    )
    with pytest.raises(manifest_builder.ReclaimError, match="shared"):
        _build_manifest(shared)

    moving = _build_fixture(tmp_path / "moving")
    original_snapshot = manifest_builder._remote_snapshot
    calls = 0

    def move_on_final(repo: Path) -> bytes:
        nonlocal calls
        calls += 1
        value = original_snapshot(repo)
        return value if calls == 1 else value + b"movement"

    monkeypatch.setattr(manifest_builder, "_remote_snapshot", move_on_final)
    with pytest.raises(manifest_builder.ReclaimError, match="moved"):
        _build_manifest(moving)
    assert not moving.output.exists()


@pytest.mark.parametrize("duplicate", ["source", "ledger"])
def test_manifest_refuses_duplicate_snapshot_provenance(tmp_path: Path, duplicate: str) -> None:
    fixture = _build_fixture(tmp_path)
    snapshot_path = next((fixture.repo / TICKET).glob("*-SNAPSHOT.json"))
    snapshot = json.loads(snapshot_path.read_bytes())
    data = snapshot["data"]
    removed_uuid = "00000000-0000-4000-8000-000000000011"
    if duplicate == "source":
        data["source_event_uuids"].append(removed_uuid)
    else:
        ledger = data["compiled_state"]["authorship_ledger"]
        ledger.append(next(item for item in ledger if item["event_uuid"] == removed_uuid))
    _write_json(snapshot_path, snapshot)
    fixture.tip = _commit(fixture.repo, f"duplicate snapshot {duplicate} provenance")
    _git(fixture.repo, "push", "-q", "origin", "tickets")
    _refresh_backup(fixture)

    with pytest.raises(manifest_builder.ReclaimError, match="duplicate"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


def test_manifest_refuses_snapshot_comment_representation_drift(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    snapshot_path = next((fixture.repo / TICKET).glob("*-SNAPSHOT.json"))
    snapshot = json.loads(snapshot_path.read_bytes())
    comments = snapshot["data"]["compiled_state"]["comments"]
    next(item for item in comments if item["timestamp"] == 11)["body"] = "drifted body"
    _write_json(snapshot_path, snapshot)
    fixture.tip = _commit(fixture.repo, "drift snapshot comment representation")
    _git(fixture.repo, "push", "-q", "origin", "tickets")
    _refresh_backup(fixture)

    with pytest.raises(manifest_builder.ReclaimError, match="representation drifted"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


def test_manifest_refuses_duplicate_backup_remote_census_rows(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    backup = json.loads(fixture.backup_manifest.read_bytes())
    backup["remote_refs"].append(copy.deepcopy(backup["remote_refs"][0]))
    _write_json(fixture.backup_manifest, backup)

    with pytest.raises(manifest_builder.ReclaimError, match="remote-ref census"):
        _build_manifest(fixture)

    assert not fixture.output.exists()


def test_manifest_refuses_a_symlinked_input_before_writing_output(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path / "fixture")
    inventory_link = fixture.inventory.with_name("jira-link.json")
    inventory_link.symlink_to(fixture.inventory)
    fixture.inventory = inventory_link

    with pytest.raises(manifest_builder.ReclaimError, match="must not be a symlink"):
        _build_manifest(fixture)

    assert not fixture.output.exists()
