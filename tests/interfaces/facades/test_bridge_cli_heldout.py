"""Held-out behavioral oracle for the staged ``rebar bridge`` surface."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import _cli


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["REBAR_ROOT"] = str(repo)
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


@pytest.mark.parametrize(
    ("args", "expected_rc", "usage"),
    [
        (("bridge",), 0, "usage: rebar bridge"),
        (("bridge", "--help"), 0, "usage: rebar bridge"),
        (("bridge", "preview", "--help"), 0, "usage: rebar bridge preview"),
        (("bridge", "sync", "--help"), 0, "usage: rebar bridge sync"),
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
    for future_verb in ("status", "resolve", "probe", "fsck", "bind", "unbind"):
        assert not re.search(rf"(?<![-a-z]){future_verb}(?![-a-z])", combined)


def test_unknown_nested_verb_never_falls_through_to_top_level(rebar_repo: Path) -> None:
    completed = _run_cli(rebar_repo, "bridge", "doctor")
    assert completed.returncode != 0
    assert "doctor" in completed.stderr.lower()
    assert "preview" in completed.stderr.lower()
    assert "sync" in completed.stderr.lower()
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
        mode = argv[argv.index("--mode") + 1]
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
    assert calls[0][-2:] == ["--mode", "live"]


def test_bridge_help_avoids_internal_vocabulary_and_numeric_exit_codes(
    rebar_repo: Path,
) -> None:
    forbidden = ("mode", "rank", "gate", "lock", "cap", "pass", "binding store")
    for args in (
        ("bridge", "--help"),
        ("bridge", "preview", "--help"),
        ("bridge", "sync", "--help"),
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
