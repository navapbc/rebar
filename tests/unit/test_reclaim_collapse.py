"""RED-first oracle for the S1 offline reclaim-collapse engine.

The fixtures build disposable ticket-branch git repositories under pytest's sandbox and
mark them as reclaim shadow clones. They never mount or mutate the live rebar tracker.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env


def _git(cwd: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_all(tracker: Path, message: str, when: datetime) -> str:
    return _commit_all_with_dates(tracker, message, when.isoformat(), when.isoformat())


def _commit_all_with_dates(
    tracker: Path, message: str, author_date: str, committer_date: str
) -> str:
    env = subprocess_env(
        {
            "GIT_AUTHOR_DATE": author_date,
            "GIT_COMMITTER_DATE": committer_date,
        }
    )
    subprocess.run(["git", "-C", str(tracker), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(tracker), "commit", "-q", "-m", message],
        check=True,
        env=env,
    )
    return _git(tracker, "rev-parse", "HEAD")


def _write_event(
    tracker: Path,
    ticket_id: str,
    timestamp: int,
    event_uuid: str,
    event_type: str,
    data: dict,
    *,
    author_sig: str | None = None,
    author_id: str | None = None,
) -> Path:
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "uuid": event_uuid,
        "author": "Test Bot",
        "env_id": "test-env",
        "data": data,
    }
    if author_sig is not None:
        event["author_sig"] = author_sig
    if author_id is not None:
        event["author_id"] = author_id
    ticket_dir = tracker / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    path = ticket_dir / f"{timestamp}-{event_uuid}-{event_type}.json"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def _make_shadow_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "shadow-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "dev@example.com")
    _git(tracker, "config", "user.name", "Dev")
    marker_dir = tracker / ".rebar"
    marker_dir.mkdir()
    (marker_dir / "reclaim-shadow-clone").write_text("disposable test clone\n")
    return tracker


def _build_collapsible_store(tmp_path: Path) -> tuple[Path, str, str, str]:
    tracker = _make_shadow_tracker(tmp_path)
    old = datetime.now(UTC) - timedelta(days=45)
    recent = datetime.now(UTC) - timedelta(days=1)
    ticket_id = "aaaa-bbbb-cccc-dddd"

    create = _write_event(
        tracker,
        ticket_id,
        1000,
        "11111111-1111-4111-8111-111111111111",
        "CREATE",
        {
            "ticket_type": "task",
            "title": "folded",
            "description": "",
            "creation_channel": "python",
        },
    )
    _commit_all(tracker, "ticket: CREATE folded", old)
    _write_event(
        tracker,
        ticket_id,
        1001,
        "22222222-2222-4222-8222-222222222222",
        "COMMENT",
        {"body": "below horizon"},
    )
    _commit_all(tracker, "ticket: COMMENT folded", old + timedelta(seconds=1))
    compiled = {
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "title": "folded",
        "status": "open",
        "comments": [{"body": "below horizon", "author": "Test Bot", "timestamp": 1001}],
        "description": "",
        "tags": [],
        "authorship_ledger": [],
        "file_impact": [],
        "file_impact_scope": "undeclared",
        "no_file_impact_reason": "",
        "deps": [],
        "managed_refs": [],
        "attestations": {},
        "keyring": [],
        "keys": [],
        "creation_channel": "python",
    }
    _write_event(
        tracker,
        ticket_id,
        1002,
        "33333333-3333-4333-8333-333333333333",
        "SNAPSHOT",
        {"compiled_state": compiled, "source_event_uuids": [], "compacted_at": 1002},
    )
    create.rename(create.with_suffix(create.suffix + ".retired"))
    boundary = _commit_all(tracker, "ticket: SNAPSHOT folded", old + timedelta(seconds=2))
    _write_event(
        tracker,
        ticket_id,
        2000,
        "44444444-4444-4444-8444-444444444444",
        "COMMENT",
        {"body": "above horizon"},
    )
    tip = _commit_all(tracker, "ticket: COMMENT retained", recent)
    return tracker, ticket_id, boundary, tip


def _engine():
    try:
        from rebar._commands import reclaim_collapse
    except ImportError as exc:  # pragma: no cover - RED before implementation
        raise AssertionError("reclaim_collapse engine module is missing") from exc
    return reclaim_collapse


def test_apply_collapses_below_horizon_history_and_preserves_reduced_state(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, original_tip = _build_collapsible_store(tmp_path)
    before = _engine().reduce_shadow_tracker(tracker)

    result = _engine().collapse_shadow_tracker(
        tracker,
        boundary_commit=boundary,
        apply=True,
        now=datetime.now(UTC),
    )

    after = _engine().reduce_shadow_tracker(tracker)
    assert after == before
    assert result.original_tip == original_tip
    assert result.rewritten_tip == _git(tracker, "rev-parse", "HEAD")
    assert result.rewritten_tip != original_tip
    roots = _git(tracker, "rev-list", "--max-parents=0", "HEAD").splitlines()
    assert roots == [result.checkpoint_sha]
    assert int(_git(tracker, "rev-list", "--count", "HEAD")) < 4
    assert result.used_fast_import is True


def test_rejects_boundary_that_is_not_ancestor_of_tip(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, tip = _build_collapsible_store(tmp_path)
    _git(tracker, "checkout", "-q", "-b", "side", boundary)
    _write_event(
        tracker,
        "side-ticket",
        2500,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "CREATE",
        {"ticket_type": "task", "title": "side", "creation_channel": "python"},
    )
    non_ancestor = _commit_all(tracker, "ticket: CREATE side", datetime.now(UTC))
    _git(tracker, "checkout", "-q", "tickets")

    with pytest.raises(_engine().ReclaimCollapseError, match="not an ancestor"):
        _engine().collapse_shadow_tracker(
            tracker,
            boundary_commit=non_ancestor,
            branch=tip,
            apply=False,
        )


def test_rewrites_snapshot_ledger_positions_to_new_checkpoint_sha(tmp_path: Path) -> None:
    tracker = _make_shadow_tracker(tmp_path)
    old = datetime.now(UTC) - timedelta(days=45)
    ticket_id = "bbbb-cccc-dddd-eeee"
    event_uuid = "55555555-5555-4555-8555-555555555555"
    _write_event(
        tracker,
        ticket_id,
        3000,
        event_uuid,
        "CREATE",
        {"ticket_type": "task", "title": "signed", "creation_channel": "python"},
        author_sig="not-a-real-signature",
        author_id="identity-ticket",
    )
    old_event_sha = _commit_all(tracker, "ticket: CREATE signed", old)
    snapshot = _write_event(
        tracker,
        ticket_id,
        3001,
        "66666666-6666-4666-8666-666666666666",
        "SNAPSHOT",
        {
            "compiled_state": {
                "ticket_id": ticket_id,
                "ticket_type": "task",
                "title": "signed",
                "status": "open",
                "comments": [],
                "description": "",
                "tags": [],
                "authorship_ledger": [
                    {
                        "event_uuid": event_uuid,
                        "content_hash": "abc123",
                        "signature": "not-a-real-signature",
                        "signer_pubkey": None,
                        "position": {
                            "position": f"3000-{event_uuid}",
                            "commit_sha": old_event_sha,
                        },
                    }
                ],
            },
            "source_event_uuids": [event_uuid],
            "compacted_at": 3001,
        },
    )
    boundary = _commit_all(tracker, "ticket: SNAPSHOT signed", old + timedelta(seconds=1))

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    rewritten = json.loads((tracker / ticket_id / snapshot.name).read_text(encoding="utf-8"))
    ledger = rewritten["data"]["compiled_state"]["authorship_ledger"]
    assert ledger[0]["position"]["position"] == f"3000-{event_uuid}"
    assert ledger[0]["position"]["commit_sha"] == result.checkpoint_sha
    assert old_event_sha not in json.dumps(rewritten)


def test_fast_import_preserves_metadata_timezone_and_raw_message_bytes(tmp_path: Path) -> None:
    tracker = _make_shadow_tracker(tmp_path)
    ticket_id = "bbbb-cccc-dddd-ffff"
    _write_event(
        tracker,
        ticket_id,
        3100,
        "12121212-1212-4121-8121-121212121212",
        "CREATE",
        {"ticket_type": "task", "title": "tz", "creation_channel": "python"},
    )
    boundary = _commit_all_with_dates(
        tracker,
        "ticket: CREATE tz",
        "2026-01-02T03:04:05 +0530",
        "2026-01-02T03:04:06 -0700",
    )
    _write_event(
        tracker,
        ticket_id,
        3101,
        "34343434-3434-4343-8343-343434343434",
        "COMMENT",
        {"body": "after boundary"},
    )
    _commit_all(tracker, "ticket: COMMENT tz", datetime.now(UTC))

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    assert _git(tracker, "show", "-s", "--format=%ai", result.checkpoint_sha).endswith(" +0530")
    assert _git(tracker, "show", "-s", "--format=%ci", result.checkpoint_sha).endswith(" -0700")

    assert _engine()._commit_message(tracker, result.checkpoint_sha) == b"ticket: CREATE tz\n"
    raw_message = b"raw subject\n\nbody with non-utf8 byte: \xff\n"
    tree = _git(tracker, "rev-parse", "HEAD^{tree}")
    raw_commit = (
        f"tree {tree}\n"
        "author Dev <dev@example.com> 1760000000 +0000\n"
        "committer Dev <dev@example.com> 1760000000 +0000\n"
        "\n"
    ).encode() + raw_message
    created = subprocess.run(
        ["git", "-C", str(tracker), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=raw_commit,
        capture_output=True,
        check=True,
    )
    raw_message_sha = created.stdout.decode().strip()
    assert _engine()._commit_message(tracker, raw_message_sha) == raw_message


def test_remote_horizon_ref_makes_boundary_eligibility_blocking(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, tip = _build_collapsible_store(tmp_path)
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", tip)

    with pytest.raises(_engine().ReclaimCollapseError, match="remote-anchored reclaim horizon"):
        _engine().collapse_shadow_tracker(
            tracker,
            boundary_commit=boundary,
            apply=False,
            now=datetime.fromtimestamp(1003, UTC),
        )


def test_enforce_since_reanchor_makes_verify_authorship_clean(tmp_path: Path) -> None:
    tracker = _make_shadow_tracker(tmp_path)
    old = datetime.now(UTC) - timedelta(days=45)
    ticket_id = "cccc-dddd-eeee-ffff"
    raw = _write_event(
        tracker,
        ticket_id,
        4000,
        "77777777-7777-4777-8777-777777777777",
        "CREATE",
        {"ticket_type": "task", "title": "unsigned folded", "creation_channel": "python"},
    )
    _commit_all(tracker, "ticket: CREATE unsigned", old)
    _write_event(
        tracker,
        ticket_id,
        4001,
        "88888888-8888-4888-8888-888888888888",
        "SNAPSHOT",
        {
            "compiled_state": {
                "ticket_id": ticket_id,
                "ticket_type": "task",
                "title": "unsigned folded",
                "status": "open",
                "comments": [],
                "description": "",
                "tags": [],
                "authorship_ledger": [],
            },
            "source_event_uuids": [],
            "compacted_at": 4001,
        },
    )
    raw.rename(raw.with_suffix(raw.suffix + ".retired"))
    boundary = _commit_all(tracker, "ticket: SNAPSHOT unsigned", old + timedelta(seconds=1))

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    env = subprocess_env(
        {
            "REBAR_ROOT": str(tmp_path),
            "REBAR_TRACKER_DIR": str(tracker),
            "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
            "REBAR_IDENTITY_ENFORCE_SINCE": result.enforce_since,
        }
    )
    completed = subprocess.run(
        ["rebar", "verify-authorship", "--all"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "OK" in (completed.stdout + completed.stderr)


def test_cli_error_path_returns_two_for_shadow_safety_refusal(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)
    (tracker / ".rebar" / "reclaim-shadow-clone").unlink()

    result = subprocess.run(
        [
            "rebar",
            "reclaim-collapse",
            "--shadow-tracker",
            str(tracker),
            "--boundary",
            boundary,
        ],
        cwd=tmp_path,
        env=subprocess_env({"REBAR_ROOT": str(tmp_path)}),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "reclaim-collapse:" in result.stderr


def test_refuses_non_shadow_or_push_capable_targets(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)
    marker = tracker / ".rebar" / "reclaim-shadow-clone"
    marker.unlink()
    engine = _engine()
    with pytest.raises(engine.ShadowSafetyError, match="shadow"):
        engine.collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=False)

    marker.write_text("disposable test clone\n")
    _git(tracker, "remote", "add", "origin", "https://example.invalid/prod/tickets.git")
    with pytest.raises(engine.ShadowSafetyError, match=r"push|remote"):
        engine.collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=False)


def test_cli_dry_run_reports_plan_without_updating_head(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, tip = _build_collapsible_store(tmp_path)
    result = subprocess.run(
        [
            "rebar",
            "reclaim-collapse",
            "--shadow-tracker",
            str(tracker),
            "--boundary",
            boundary,
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=subprocess_env({"REBAR_ROOT": str(tmp_path)}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["original_tip"] == tip
    assert payload["checkpoint_sha"]
    assert _git(tracker, "rev-parse", "HEAD") == tip
