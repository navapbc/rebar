"""Held-out behavioral oracle for the staged ``rebar bridge`` surface."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar import _cli


def _run_cli(
    repo: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(repo)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tracker_snapshot(repo: Path) -> dict[str, bytes]:
    tracker = repo / ".tickets-tracker"
    return {
        str(path.relative_to(tracker)): path.read_bytes()
        for path in tracker.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _configure_origin(repo: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", "HEAD"],
        check=True,
        capture_output=True,
    )
    return remote


def _remote_blob(remote: Path, ref: str = "refs/reconciler/gate") -> bytes | None:
    oid = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if oid.returncode != 0:
        return None
    return subprocess.run(
        ["git", "--git-dir", str(remote), "cat-file", "blob", oid.stdout.strip()],
        capture_output=True,
        check=True,
    ).stdout


def _remote_oid(remote: Path, ref: str = "refs/reconciler/gate") -> str | None:
    completed = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _plant_remote_blob(remote: Path, raw: bytes, ref: str = "refs/reconciler/gate") -> str:
    oid = (
        subprocess.run(
            ["git", "--git-dir", str(remote), "hash-object", "-w", "--stdin"],
            input=raw,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "--git-dir", str(remote), "update-ref", ref, oid],
        capture_output=True,
        check=True,
    )
    return oid


@pytest.mark.parametrize(
    ("args", "expected_rc", "usage"),
    [
        (("bridge",), 0, "usage: rebar bridge"),
        (("bridge", "--help"), 0, "usage: rebar bridge"),
        (("bridge", "preview", "--help"), 0, "usage: rebar bridge preview"),
        (("bridge", "sync", "--help"), 0, "usage: rebar bridge sync"),
        (("bridge", "status", "--help"), 0, "usage: rebar bridge status"),
        (("bridge", "pause", "--help"), 0, "usage: rebar bridge pause"),
        (("bridge", "resume", "--help"), 0, "usage: rebar bridge resume"),
        (("bridge", "fsck", "--help"), 0, "usage: rebar bridge fsck"),
        (
            ("bridge", "check-access", "--help"),
            0,
            "usage: rebar bridge check-access",
        ),
        (("bridge", "setup", "--help"), 0, "usage: rebar bridge setup"),
    ],
)
def test_bridge_discovery_contracts(
    rebar_repo: Path, args: tuple[str, ...], expected_rc: int, usage: str
) -> None:
    completed = _run_cli(rebar_repo, *args)
    assert completed.returncode == expected_rc
    assert usage in completed.stdout.lower()
    assert completed.stderr == ""
    combined = (completed.stdout + completed.stderr).lower()
    for future_verb in ("resolve", "probe", "bind", "unbind"):
        assert not re.search(rf"(?<![-a-z]){future_verb}(?![-a-z])", combined)


def test_unknown_nested_verb_never_falls_through_to_top_level(rebar_repo: Path) -> None:
    completed = _run_cli(rebar_repo, "bridge", "doctor")
    assert completed.returncode != 0
    assert "doctor" in completed.stderr.lower()
    assert "preview" in completed.stderr.lower()
    assert "sync" in completed.stderr.lower()
    assert "pause" in completed.stderr.lower()
    assert "resume" in completed.stderr.lower()
    assert "Health Score" not in (completed.stdout + completed.stderr)


@pytest.mark.parametrize("verb", ["preview", "sync"])
@pytest.mark.parametrize(
    "bad_args",
    [
        ("--mode", "live"),
        ("--mode=live",),
        ("--filter-local-ids", "abc"),
        ("--filter-local-ids=abc",),
        ("trailing",),
    ],
)
def test_bridge_verbs_reject_child_arguments(
    rebar_repo: Path, monkeypatch, capsys, verb: str, bad_args: tuple[str, ...]
) -> None:
    def forbidden_call(*_args, **_kwargs) -> int:
        raise AssertionError("reconciler must not launch after argument rejection")

    monkeypatch.setattr(subprocess, "call", forbidden_call)
    rc = _cli.main(["bridge", verb, *bad_args])
    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err.lower()
    assert bad_args[0] in captured.err


def test_preview_is_read_only_and_sync_persists_the_binding(
    rebar_repo: Path, monkeypatch, tmp_path: Path
) -> None:
    """A stateful child-boundary fake discriminates the two production modes."""
    unrelated = rebar_repo / ".tickets-tracker" / "unrelated-contract.bin"
    unrelated.write_bytes(b"preserve-me-byte-for-byte")
    transport_log = tmp_path / "jira-transport.jsonl"
    bridge_state = rebar_repo / ".rebar" / "bridge-state" / "bindings.json"

    def fake_reconciler(argv: list[str], *, env=None) -> int:
        route = argv[3]
        mode = {"preview": "dry-run", "sync": "live"}[route]
        with transport_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": "search", "mode": mode}) + "\n")
        if mode == "dry-run":
            print(json.dumps({"mode": mode, "plan": [{"jira_key": "DIG-42"}]}))
            return 0
        bridge_state.parent.mkdir(parents=True, exist_ok=True)
        bridge_state.write_text(
            json.dumps({"DIG-42": {"local_id": "jira-dig-42"}}) + "\n",
            encoding="utf-8",
        )
        with transport_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"operation": "bind", "jira_key": "DIG-42"}) + "\n")
        return 0

    monkeypatch.setattr(subprocess, "call", fake_reconciler)
    before = _tracker_snapshot(rebar_repo)
    assert _cli.main(["bridge", "preview"]) == 0
    assert _tracker_snapshot(rebar_repo) == before
    assert not bridge_state.exists()
    preview_log = transport_log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["operation"] for line in preview_log] == ["search"]

    assert _cli.main(["bridge", "sync"]) == 0
    assert json.loads(bridge_state.read_text(encoding="utf-8")) == {
        "DIG-42": {"local_id": "jira-dig-42"}
    }
    assert unrelated.read_bytes() == b"preserve-me-byte-for-byte"
    sync_log = transport_log.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["operation"] for line in sync_log] == [
        "search",
        "search",
        "bind",
    ]


def test_sync_launches_live_and_preserves_child_exit_code(rebar_repo: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def phase_gate(argv: list[str], *, env=None) -> int:
        calls.append(argv)
        return 4

    monkeypatch.setattr(subprocess, "call", phase_gate)
    assert _cli.main(["bridge", "sync"]) == 4
    assert calls[0][3] == "sync"
    assert "--mode" not in calls[0]


def test_bridge_help_avoids_internal_vocabulary_and_numeric_exit_codes(
    rebar_repo: Path,
) -> None:
    forbidden = ("mode", "rank", "gate", "lock", "cap", "pass", "binding store")
    for args in (
        ("bridge", "--help"),
        ("bridge", "preview", "--help"),
        ("bridge", "sync", "--help"),
        ("bridge", "pause", "--help"),
        ("bridge", "resume", "--help"),
    ):
        completed = _run_cli(rebar_repo, *args)
        assert completed.returncode == 0
        text = (completed.stdout + completed.stderr).lower()
        for token in forbidden:
            assert token not in text
        assert re.search(r"\b(?:[0-9]+)\b", text) is None


def test_legacy_reconcile_keeps_dry_run_default(rebar_repo: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "call",
        lambda argv, *, env=None: calls.append(argv) or 0,
    )
    assert _cli.main(["reconcile"]) == 0
    assert calls[0][-2:] == ["--mode", "dry-run"]


def test_pause_and_resume_dispatch_happy_path(rebar_repo: Path, tmp_path: Path) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    assert _cli.main(["bridge", "pause", "planned maintenance"]) == 0
    state = json.loads(_remote_blob(remote) or b"{}")
    assert state["reason"] == "planned maintenance"
    assert state["who"] == "test@example.com"
    assert state["gated_mode"] == "reconcile-check"
    assert state["paused"] is True
    assert _cli.main(["bridge", "resume"]) == 0
    assert _remote_blob(remote) is None


def test_pause_and_resume_real_cli_subprocess_round_trip(rebar_repo: Path, tmp_path: Path) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)

    paused = _run_cli(rebar_repo, "bridge", "pause", 'release "cutover"\nphase two')
    assert paused.returncode == 0
    state = json.loads(_remote_blob(remote) or b"{}")
    assert state == {
        "gated_mode": "reconcile-check",
        "paused": True,
        "reason": 'release "cutover"\nphase two',
        "who": "test@example.com",
        "paused_at": state["paused_at"],
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", state["paused_at"])

    resumed = _run_cli(rebar_repo, "bridge", "resume")
    assert resumed.returncode == 0
    assert _remote_blob(remote) is None


def test_pause_refuses_blank_reason_and_missing_git_email_without_write(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)

    blank = _run_cli(rebar_repo, "bridge", "pause", "")
    assert blank.returncode != 0
    assert "Error:" in blank.stderr
    assert "Traceback" not in blank.stderr
    assert _remote_blob(remote) is None

    subprocess.run(
        ["git", "-C", str(rebar_repo), "config", "user.email", ""],
        capture_output=True,
        check=True,
    )
    missing = _run_cli(rebar_repo, "bridge", "pause", "planned maintenance")
    assert missing.returncode != 0
    assert "user.email" in missing.stderr
    assert _remote_blob(remote) is None


def test_repause_preserves_metadata_or_refuses_different_reason(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    assert _run_cli(rebar_repo, "bridge", "pause", "database cutover").returncode == 0
    first_oid = _remote_oid(remote)
    first_raw = _remote_blob(remote)

    subprocess.run(
        ["git", "-C", str(rebar_repo), "config", "user.email", "second@example.com"],
        capture_output=True,
        check=True,
    )
    assert _run_cli(rebar_repo, "bridge", "pause", "database cutover").returncode == 0
    assert _remote_oid(remote) == first_oid
    assert _remote_blob(remote) == first_raw

    refused = _run_cli(rebar_repo, "bridge", "pause", "network maintenance")
    assert refused.returncode != 0
    assert "database cutover" in refused.stderr
    assert _remote_oid(remote) == first_oid
    assert _remote_blob(remote) == first_raw


def test_resume_absent_gate_is_success(rebar_repo: Path, tmp_path: Path) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    resumed = _run_cli(rebar_repo, "bridge", "resume")
    assert resumed.returncode == 0
    assert "already" in (resumed.stdout + resumed.stderr).lower()
    assert _remote_blob(remote) is None


def test_resume_clears_corrupt_blob_without_decoding_then_pause_works(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    _plant_remote_blob(remote, b"not-json\x00gate")
    assert _remote_blob(remote) == b"not-json\x00gate"

    resumed = _run_cli(rebar_repo, "bridge", "resume")
    assert resumed.returncode == 0
    assert _remote_blob(remote) is None

    paused = _run_cli(rebar_repo, "bridge", "pause", "post-recovery maintenance")
    assert paused.returncode == 0
    assert json.loads(_remote_blob(remote) or b"{}")["reason"] == "post-recovery maintenance"


def test_pause_missing_origin_refuses_local_only_gate(rebar_repo: Path) -> None:
    refused = _run_cli(rebar_repo, "bridge", "pause", "planned maintenance")
    assert refused.returncode != 0
    assert "origin" in refused.stderr.lower()
    local = subprocess.run(
        [
            "git",
            "-C",
            str(rebar_repo),
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/reconciler/gate",
        ],
        capture_output=True,
        check=False,
    )
    assert local.returncode != 0


def test_real_paused_reconciler_is_benign_and_mutates_no_ticket_or_lock(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    assert _run_cli(rebar_repo, "bridge", "pause", "database cutover").returncode == 0
    before = _tracker_snapshot(rebar_repo)

    completed = _run_cli(rebar_repo, "reconcile", "--mode", "live")

    assert completed.returncode == 0
    assert completed.stdout == ""
    state = json.loads(_remote_blob(remote) or b"{}")
    marker = json.dumps(
        {key: state[key] for key in ("paused", "reason", "who", "paused_at")},
        separators=(",", ":"),
    )
    assert completed.stderr == f"BRIDGE_PAUSED: {marker}\n"
    assert _tracker_snapshot(rebar_repo) == before
    assert _remote_blob(remote, "refs/reconciler/lock") is None


def test_real_corrupt_gate_has_stable_error_without_traceback_or_mutation(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    _plant_remote_blob(remote, b"{corrupt")
    before = _tracker_snapshot(rebar_repo)

    completed = _run_cli(rebar_repo, "reconcile", "--mode", "live")

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.splitlines()[0] == (
        "ERROR: refs/reconciler/gate is corrupt; run 'rebar bridge resume' to clear it"
    )
    assert "Traceback" not in completed.stderr
    assert _tracker_snapshot(rebar_repo) == before
    assert _remote_blob(remote, "refs/reconciler/lock") is None


def test_real_legacy_gate_keeps_old_error_contract(rebar_repo: Path, tmp_path: Path) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    _plant_remote_blob(remote, b'{"gated_mode":"reconcile-check"}\n')
    before = _tracker_snapshot(rebar_repo)

    completed = _run_cli(rebar_repo, "reconcile", "--mode", "live")

    assert completed.returncode == 4
    assert "blocks advancement" in completed.stderr
    assert "BRIDGE_PAUSED" not in completed.stderr
    assert _tracker_snapshot(rebar_repo) == before
    assert _remote_blob(remote, "refs/reconciler/lock") is None


def test_real_canonical_gate_is_benign_while_legacy_stays_4(
    rebar_repo: Path, tmp_path: Path
) -> None:
    remote = _configure_origin(rebar_repo, tmp_path)
    _plant_remote_blob(remote, b'{"gated_mode":"reconcile-check"}\n')
    before = _tracker_snapshot(rebar_repo)

    canonical = _run_cli(rebar_repo, "bridge", "sync")

    assert canonical.returncode == 0
    assert canonical.stdout == ""
    assert canonical.stderr == "BRIDGE_STATE: legacy-gated\n"
    assert _tracker_snapshot(rebar_repo) == before
    assert _remote_blob(remote, "refs/reconciler/lock") is None

    legacy = _run_cli(rebar_repo, "reconcile", "--mode", "live")
    assert legacy.returncode == 4
    assert "blocks advancement" in legacy.stderr
    assert _tracker_snapshot(rebar_repo) == before
    assert _remote_blob(remote, "refs/reconciler/lock") is None
