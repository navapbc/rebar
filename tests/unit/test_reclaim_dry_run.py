"""The reclaim dry-run harness on a synthetic tickets store (story S3).

The source is a normal repository under ``tmp_path``; the harness makes every other
repository inside its own workdir. Nothing here reaches a real store.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._commands import reclaim_dry_run

pytestmark = pytest.mark.unit

TICKET = "eeee-0000-0000-0001"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _event(repo: Path, ts: int, event_uuid: str, event_type: str, data: dict) -> Path:
    path = repo / TICKET / f"{ts}-{event_uuid}-{event_type}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": event_type,
        "timestamp": ts,
        "uuid": event_uuid,
        "author": "Test Bot",
        "env_id": "test-env",
        "data": data,
    }
    path.write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return path


def _commit(repo: Path, message: str, when: datetime) -> str:
    env = subprocess_env(
        {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--no-verify", "-m", message],
        check=True,
        env=env,
    )
    return _git(repo, "rev-parse", "HEAD")


def _source(tmp_path: Path, *, old_days: int) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    old = datetime.now(UTC) - timedelta(days=old_days)
    create_uuid = "11111111-1111-4111-8111-111111111111"
    create = _event(
        repo,
        1000,
        "11111111-1111-4111-8111-111111111111",
        "CREATE",
        {"ticket_type": "task", "title": "t", "description": "", "creation_channel": "python"},
    )
    create_sha = _commit(repo, "ticket: CREATE", old)
    for n in range(5):
        _event(
            repo,
            1001 + n,
            f"2222222{n}-2222-4222-8222-222222222222",
            "COMMENT",
            {"body": f"old {n}"},
        )
        _commit(repo, f"ticket: COMMENT old {n}", old + timedelta(minutes=n + 1))
    compiled = {
        "ticket_id": TICKET,
        "ticket_type": "task",
        "title": "t",
        "status": "open",
        "comments": [
            {"body": f"old {n}", "author": "Test Bot", "timestamp": 1001 + n} for n in range(5)
        ],
        "description": "",
        "tags": [],
        # A ledger entry whose commit_sha the reclaim must re-resolve to the checkpoint.
        "authorship_ledger": [
            {
                "event_uuid": create_uuid,
                "content_hash": "h",
                "signature": None,
                "signer_pubkey": None,
                "position": {"position": f"1000-{create_uuid}", "commit_sha": create_sha},
            }
        ],
        "creation_channel": "python",
    }
    _event(
        repo,
        1010,
        "33333333-3333-4333-8333-333333333333",
        "SNAPSHOT",
        {"compiled_state": compiled, "source_event_uuids": [], "compacted_at": 1010},
    )
    create.rename(create.with_suffix(create.suffix + ".retired"))
    _commit(repo, "ticket: SNAPSHOT", old + timedelta(minutes=10))
    recent = datetime.now(UTC) - timedelta(days=1)
    _event(repo, 2000, "44444444-4444-4444-8444-444444444444", "COMMENT", {"body": "recent"})
    _commit(repo, "ticket: COMMENT recent", recent)
    return repo


def test_dry_run_writes_a_passing_evidence_packet(tmp_path: Path) -> None:
    source = _source(tmp_path, old_days=45)
    tip_before = _git(source, "rev-parse", "tickets")
    work = tmp_path / "work"

    packet = reclaim_dry_run.run_dry_run(source, "tickets", work, horizon_days=30)

    on_disk = json.loads((work / "evidence.json").read_text(encoding="utf-8"))
    assert on_disk == packet
    assert packet["ok"] is True
    assert packet["reducer_equal"] is True
    # The synthetic events are unsigned, so enforcement fails both before and after; the
    # reclaim must leave every verdict count unchanged and keep enforce_since on the branch.
    assert packet["identity_counts_equal"] is True
    assert packet["identity_before"]["counts"] == packet["identity_after"]["counts"]
    assert packet["identity_before"]["counts"]["unsigned"] == 7
    assert packet["identity_before"]["counts"]["signer_not_on_keyring"] == 0
    assert packet["enforce_since_on_branch"] is True
    assert packet["source_ref_unchanged"] is True
    assert packet["pack_kib_after"] <= packet["pack_kib_before"]
    assert packet["delta_kib"] == packet["pack_kib_before"] - packet["pack_kib_after"]
    assert packet["predicted_mib"] == [365, 380]
    assert packet["commits_after"] < packet["commits_before"]
    assert packet["published_tip"] == packet["new_tip"]
    fake = work / "fake-remote.git"
    assert (
        subprocess.run(
            ["git", "--git-dir", str(fake), "rev-parse", "refs/heads/tickets"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == packet["new_tip"]
    )
    heads = subprocess.run(
        ["git", "--git-dir", str(fake), "for-each-ref", "--format=%(refname)", "refs/heads/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert heads == ["refs/heads/tickets"]
    assert _git(source, "rev-parse", "tickets") == tip_before


def test_dry_run_without_an_eligible_boundary_fails_with_a_packet(tmp_path: Path) -> None:
    source = _source(tmp_path, old_days=2)
    tip_before = _git(source, "rev-parse", "tickets")
    work = tmp_path / "work"

    with pytest.raises(reclaim_dry_run.DryRunError, match="no eligible boundary"):
        reclaim_dry_run.run_dry_run(source, "tickets", work, horizon_days=30)

    packet = json.loads((work / "evidence.json").read_text(encoding="utf-8"))
    assert packet["ok"] is False
    assert "no eligible boundary" in packet["error"]
    assert packet["source_ref_unchanged"] is True
    assert _git(source, "rev-parse", "tickets") == tip_before


def test_dry_run_refuses_a_non_empty_workdir(tmp_path: Path) -> None:
    source = _source(tmp_path, old_days=45)
    work = tmp_path / "work"
    work.mkdir()
    (work / "leftover").write_text("x", encoding="utf-8")

    with pytest.raises(reclaim_dry_run.DryRunError, match="non-empty workdir"):
        reclaim_dry_run.run_dry_run(source, "tickets", work, horizon_days=30)

    assert not (work / "evidence.json").exists()


def test_main_returns_nonzero_on_failure(tmp_path: Path) -> None:
    source = _source(tmp_path, old_days=2)

    code = reclaim_dry_run.main(
        ["--source", str(source), "--ref", "tickets", "--workdir", str(tmp_path / "w")]
    )

    assert code == 1
