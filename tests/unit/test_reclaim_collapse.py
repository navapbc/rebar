"""RED-first oracle for the S1 offline reclaim-collapse engine.

The fixtures build disposable ticket-branch git repositories under pytest's sandbox and
mark them as reclaim shadow clones. They never mount or mutate the live rebar tracker.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    path.write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return path


def _make_shadow_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "shadow-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "dev@example.com")
    _git(tracker, "config", "user.name", "Dev")
    marker_dir = tracker / ".rebar"
    marker_dir.mkdir()
    (marker_dir / "reclaim-shadow-clone").write_text("disposable test clone\n", encoding="utf-8")
    return tracker


def _set_horizon_ref(tracker: Path, commit: str) -> None:
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", _git(tracker, "rev-parse", commit))


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
    _set_horizon_ref(tracker, tip)
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
    assert result.collapsed_commits == 3
    assert result.replayed_commits == 1
    assert result.ledger_entries_rewritten == 0
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
    _set_horizon_ref(tracker, boundary)

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    rewritten = json.loads((tracker / ticket_id / snapshot.name).read_text(encoding="utf-8"))
    ledger = rewritten["data"]["compiled_state"]["authorship_ledger"]
    assert ledger[0]["position"]["position"] == f"3000-{event_uuid}"
    assert ledger[0]["position"]["commit_sha"] == result.checkpoint_sha
    assert result.ledger_entries_rewritten == 1
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
    _set_horizon_ref(tracker, "HEAD")

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
    _set_horizon_ref(tracker, tip)

    with pytest.raises(_engine().ReclaimCollapseError, match="remote-anchored reclaim horizon"):
        _engine().collapse_shadow_tracker(
            tracker,
            boundary_commit=boundary,
            apply=False,
            now=datetime.fromtimestamp(1003, UTC),
        )


def test_missing_remote_horizon_ref_is_rejected(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)
    _git(tracker, "update-ref", "-d", "refs/remotes/origin/tickets")

    with pytest.raises(_engine().ReclaimCollapseError, match="missing required remote horizon"):
        _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=False)


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
    _set_horizon_ref(tracker, boundary)

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
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
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
            sys.executable,
            "-m",
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

    marker.write_text("disposable test clone\n", encoding="utf-8")
    _git(tracker, "remote", "add", "origin", "https://example.invalid/prod/tickets.git")
    with pytest.raises(engine.ShadowSafetyError, match=r"push|remote"):
        engine.collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=False)


def test_cli_dry_run_reports_plan_without_updating_head(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, tip = _build_collapsible_store(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
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


# --- streamed engine (silk-unpleasant-jackal) -------------------------------------------


def _ledger_entry(event_uuid: str, timestamp: int, commit_sha: str) -> dict:
    return {
        "event_uuid": event_uuid,
        "content_hash": "h",
        "signature": "s",
        "signer_pubkey": None,
        "position": {"position": f"{timestamp}-{event_uuid}", "commit_sha": commit_sha},
    }


def _snapshot_data(ticket_id: str, ledger: list[dict]) -> dict:
    return {
        "compiled_state": {
            "ticket_id": ticket_id,
            "ticket_type": "task",
            "title": "t",
            "status": "open",
            "comments": [],
            "description": "",
            "tags": [],
            "authorship_ledger": ledger,
        },
        "source_event_uuids": [entry["event_uuid"] for entry in ledger],
        "compacted_at": 1,
    }


def _build_branchy_store(tmp_path: Path) -> tuple[Path, str, str]:
    """Below-boundary SNAPSHOT, a side branch forked below and merged above the boundary,
    a deletion, an event and its SNAPSHOT in one commit, and a retired SNAPSHOT."""
    tracker = _make_shadow_tracker(tmp_path)
    old = datetime.now(UTC) - timedelta(days=45)
    recent = datetime.now(UTC) - timedelta(days=1)
    t1, t2 = "aaaa-0000-0000-0001", "aaaa-0000-0000-0002"
    e1 = "a1111111-1111-4111-8111-111111111111"
    _write_event(tracker, t1, 100, e1, "CREATE", {"ticket_type": "task", "title": "a"})
    c1 = _commit_all(tracker, "s: create t1", old)
    _write_event(
        tracker,
        t1,
        101,
        "a2222222-2222-4222-8222-222222222222",
        "SNAPSHOT",
        _snapshot_data(t1, [_ledger_entry(e1, 100, c1)]),
    )
    fork = _commit_all(tracker, "s: snapshot t1", old + timedelta(seconds=1))
    _write_event(tracker, t2, 200, "a3333333-3333-4333-8333-333333333333", "COMMENT", {"b": 1})
    doomed = _write_event(
        tracker, t2, 201, "a4444444-4444-4444-8444-444444444444", "COMMENT", {"b": 2}
    )
    boundary = _commit_all(tracker, "s: boundary", old + timedelta(seconds=2))

    # Side branch from below the boundary.
    _git(tracker, "checkout", "-q", "-b", "side", fork)
    e5 = "a5555555-5555-4555-8555-555555555555"
    _write_event(tracker, t1, 300, e5, "COMMENT", {"side": True})
    _write_event(
        tracker,
        t1,
        301,
        "a6666666-6666-4666-8666-666666666666",
        "SNAPSHOT",
        _snapshot_data(t1, [_ledger_entry(e5, 300, "0" * 40)]),
    )
    side = _commit_all(tracker, "s: side event and snapshot", recent)
    _git(tracker, "checkout", "-q", "tickets")

    doomed.unlink()
    _commit_all(tracker, "s: delete", recent + timedelta(seconds=1))
    retired = _write_event(
        tracker,
        t2,
        400,
        "a7777777-7777-4777-8777-777777777777",
        "SNAPSHOT",
        _snapshot_data(t2, [_ledger_entry(e1, 100, c1)]),
    )
    retired.rename(retired.with_suffix(retired.suffix + ".retired"))
    _commit_all(tracker, "s: retired snapshot", recent + timedelta(seconds=2))
    env = subprocess_env(
        {"GIT_AUTHOR_DATE": recent.isoformat(), "GIT_COMMITTER_DATE": recent.isoformat()}
    )
    subprocess.run(
        ["git", "-C", str(tracker), "merge", "-q", "--no-ff", "-m", "s: merge side", side],
        check=True,
        env=env,
    )
    tip = _git(tracker, "rev-parse", "HEAD")
    _set_horizon_ref(tracker, tip)
    return tracker, boundary, tip


def _commits_by_subject(tracker: Path, rev: str) -> dict[str, str]:
    out = _git(tracker, "log", "--format=%H%x00%s", rev)
    return {subject: sha for sha, subject in (line.split("\x00") for line in out.splitlines())}


def _added_by(tracker: Path, rev: str) -> dict[str, str]:
    """Event position -> the original commit that added that event file."""
    out = _git(
        tracker,
        "log",
        "--diff-filter=A",
        "--full-history",
        "--no-merges",
        "--no-renames",
        "--format=%x1e%H",
        "--name-only",
        rev,
    )
    positions: dict[str, str] = {}
    for record in out.split("\x1e"):
        lines = [line for line in record.split("\n") if line.strip()]
        if not lines:
            continue
        for path in lines[1:]:
            base = Path(path).name.split(".json")[0]
            positions[base.rsplit("-", 1)[0]] = lines[0]
    return positions


def _blob_map(tracker: Path, commit: str) -> dict[str, str]:
    out = _git(tracker, "ls-tree", "-r", commit)
    return {line.split("\t", 1)[1]: line.split()[2] for line in out.splitlines()}


def test_streamed_engine_matches_the_rewrite_oracle(tmp_path: Path) -> None:
    tracker, boundary, tip = _build_branchy_store(tmp_path)
    original = _commits_by_subject(tracker, tip)
    added = _added_by(tracker, tip)
    below = set(_git(tracker, "rev-list", boundary).split())

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    rewritten = _commits_by_subject(tracker, result.rewritten_tip)
    old_to_new = {original[s]: rewritten[s] for s in original if s in rewritten}
    old_to_new[boundary] = result.checkpoint_sha
    assert result.replayed_commits == 4

    def expected_sha(position: str) -> str:
        old = added[position]
        return old_to_new[boundary] if old in below else old_to_new[old]

    for subject, old in original.items():
        if old in below:
            continue
        new = rewritten[subject]
        old_meta = _git(tracker, "show", "-s", "--format=%an|%ae|%ad|%cn|%ce|%cd|%B", old)
        assert _git(tracker, "show", "-s", "--format=%an|%ae|%ad|%cn|%ce|%cd|%B", new) == old_meta
        old_parents = _git(tracker, "show", "-s", "--format=%P", old).split()
        new_parents = _git(tracker, "show", "-s", "--format=%P", new).split()
        assert len(new_parents) == len(old_parents)
        old_blobs, new_blobs = _blob_map(tracker, old), _blob_map(tracker, new)
        assert set(new_blobs) == set(old_blobs), subject
        for path, oid in old_blobs.items():
            try:
                body = json.loads(_git(tracker, "cat-file", "blob", oid))
            except json.JSONDecodeError:
                body = {}
            if body.get("event_type") != "SNAPSHOT":
                assert new_blobs[path] == oid, (subject, path)
                continue
            new_body = json.loads(_git(tracker, "cat-file", "blob", new_blobs[path]))
            old_body = body["data"]["compiled_state"]["authorship_ledger"]
            for entry, before in zip(
                new_body["data"]["compiled_state"]["authorship_ledger"], old_body, strict=True
            ):
                position = entry["position"]["position"]
                # A commit cannot name its own new SHA, so a self-reference stays as written.
                want = (
                    before["position"]["commit_sha"]
                    if added[position] == old
                    else expected_sha(position)
                )
                assert entry["position"]["commit_sha"] == want, (subject, path)
    merge = rewritten["s: merge side"]
    side_new = rewritten["s: side event and snapshot"]
    assert _git(tracker, "show", "-s", "--format=%P", merge).split()[1] == side_new
    # The same-commit SNAPSHOT is re-resolved in the first later commit that carries it.
    merged = json.loads(
        _git(
            tracker,
            "show",
            f"{merge}:aaaa-0000-0000-0001/301-a6666666-6666-4666-8666-666666666666-SNAPSHOT.json",
        )
    )
    assert merged["data"]["compiled_state"]["authorship_ledger"][0]["position"]["commit_sha"] == (
        side_new
    )


def test_streamed_engine_runs_one_fast_import_and_one_batch_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _make_shadow_tracker(tmp_path)
    old = datetime.now(UTC) - timedelta(days=45)
    recent = datetime.now(UTC) - timedelta(days=1)
    for n in range(200):
        _write_event(
            tracker,
            f"cccc-0000-0000-{n:04d}",
            n,
            f"{n:08x}-0000-4000-8000-{n:012x}",
            "COMMENT",
            {"n": n},
        )
    boundary = _commit_all(tracker, "bulk", old)
    for n in range(50):
        _write_event(
            tracker,
            "dddd-0000-0000-0000",
            10_000 + n,
            f"{n:08x}-1111-4111-8111-{n:012x}",
            "COMMENT",
            {"n": n},
        )
        _commit_all(tracker, f"replay {n}", recent + timedelta(seconds=n))
    _set_horizon_ref(tracker, _git(tracker, "rev-parse", "HEAD"))

    engine = _engine()
    stream = __import__("rebar._commands.reclaim_collapse_stream", fromlist=["x"])
    spawned: list[list[str]] = []
    real_popen = subprocess.Popen

    def counting_popen(argv, *args, **kwargs):
        spawned.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    git_calls: list[tuple[str, ...]] = []
    real_git = engine._git

    def counting_git(tracker_path, *args, **kwargs):
        git_calls.append(args)
        return real_git(tracker_path, *args, **kwargs)

    monkeypatch.setattr(stream.subprocess, "Popen", counting_popen)
    monkeypatch.setattr(engine, "_git", counting_git)

    result = engine.collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    assert result.replayed_commits == 50
    assert sum("fast-import" in argv for argv in spawned) == 1
    assert sum("--batch" in argv for argv in spawned) == 1
    assert not [args for args in git_calls if args[:2] == ("cat-file", "blob")]
    assert not [args for args in git_calls if args and args[0] == "fast-import"]


def test_stream_failure_raises_and_leaves_the_branch_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker, _ticket_id, boundary, tip = _build_collapsible_store(tmp_path)
    engine = _engine()
    monkeypatch.setattr(engine, "_derived_identity", lambda: b"not a valid identity")

    with pytest.raises(engine.ReclaimCollapseError):
        engine.collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    assert _git(tracker, "rev-parse", "HEAD") == tip
    assert _git(tracker, "for-each-ref", "refs/rebar/reclaim-collapse/") == ""


# --- ledger anchor on the rewritten branch (euhedral-svelte-chimpanzee) ------------------


def test_ledger_anchor_is_on_the_rewritten_branch_when_commits_replay(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    assert result.replayed_commits >= 1
    ancestry = subprocess.run(
        [
            "git",
            "-C",
            str(tracker),
            "merge-base",
            "--is-ancestor",
            result.ledger_anchor_sha,
            "HEAD",
        ],
        check=False,
    )
    assert ancestry.returncode == 0
    assert result.enforce_since == result.ledger_anchor_sha
    first_replayed = _git(
        tracker, "rev-list", "--reverse", "--ancestry-path", f"{result.ledger_anchor_sha}..HEAD"
    ).split()[0]
    assert _git(tracker, "rev-parse", f"{first_replayed}^1") == result.ledger_anchor_sha


def test_enforce_since_enforces_unsigned_replayed_events(tmp_path: Path) -> None:
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)

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
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    lines = output.splitlines()
    replayed = [line for line in lines if "2000-44444444" in line]
    below = [line for line in lines if "1001-22222222" in line]
    assert replayed and "grandfathered" not in replayed[0], output
    assert below and "grandfathered" in below[0], output


def test_streamed_engine_does_not_need_pass_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CPython rejects ``pass_fds`` on Windows (sphalerite-demurrable-crossbill); the
    engine must read fast-import's mark replies without passing an extra descriptor."""
    tracker, _ticket_id, boundary, _tip = _build_collapsible_store(tmp_path)
    stream = __import__("rebar._commands.reclaim_collapse_stream", fromlist=["x"])
    real_popen = subprocess.Popen

    def windows_like_popen(argv, *args, **kwargs):
        if kwargs.get("pass_fds"):
            raise AssertionError("pass_fds not supported on Windows.")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(stream.subprocess, "Popen", windows_like_popen)

    result = _engine().collapse_shadow_tracker(tracker, boundary_commit=boundary, apply=True)

    assert result.replayed_commits >= 1
    assert _git(tracker, "rev-parse", "HEAD") == result.rewritten_tip
