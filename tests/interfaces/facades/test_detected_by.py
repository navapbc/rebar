"""detected-by capture at bug creation — happy path (ticket d3ed-88e3-86a3-4918).

Automated filers export ``REBAR_DETECTED_BY``; ``rebar create`` stamps a
``detected_by`` field into the genesis CREATE event and the compiled state.
An optional ``--detected-by`` CLI flag overrides the env var. The read lives at
the ``create_core`` seam so every ingress (CLI, library, MCP) gets the stamp.

Observable oracle only: persisted ``CREATE.data`` bytes and the show projection.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

import rebar


def _create_data(rebar_repo: Path, ticket_id: str) -> dict:
    tracker = rebar_repo / ".tickets-tracker"
    matches = sorted(tracker.glob(f"{ticket_id}/*-CREATE.json"))
    assert len(matches) == 1, f"expected one CREATE for {ticket_id}, got {matches}"
    return json.loads(matches[0].read_bytes())["data"]


_CANONICAL_ID_RE = re.compile(r"\b[0-9a-f]{4}(?:-[0-9a-f]{4}){3}\b")


def _extract_id(stdout: str) -> str:
    """Ticket id from CLI stdout: JSON ``{"id": ...}`` line, else the canonical
    id embedded in the one-line mutation confirmation."""
    for ln in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "id" in obj:
            return obj["id"]
        m = _CANONICAL_ID_RE.search(ln)
        if m:
            return m.group(0)
    raise AssertionError(f"could not parse ticket id from: {stdout!r}")


def _cli_id(*args: str, env: dict | None = None) -> str:

    merged = subprocess_env({**(env or {})})
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args], capture_output=True, text=True, env=merged
    )
    assert cp.returncode == 0, f"cli {args} failed: {cp.stderr}"
    return _extract_id(cp.stdout)


def _assert_detected(rebar_repo: Path, ticket_id: str, expected: str) -> None:
    """Both the persisted CREATE.data AND the projected state report ``expected``."""
    assert _create_data(rebar_repo, ticket_id).get("detected_by") == expected
    state = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    assert state.get("detected_by") == expected


def test_env_var_stamps_detected_by_via_cli(rebar_repo: Path):
    tid = _cli_id("create", "bug", "env-stamped bug", env={"REBAR_DETECTED_BY": "canary"})
    _assert_detected(rebar_repo, tid, "canary")


def test_flag_overrides_env(rebar_repo: Path):
    tid = _cli_id(
        "create",
        "bug",
        "flag-over-env bug",
        "--detected-by=ci",
        env={"REBAR_DETECTED_BY": "canary"},
    )
    _assert_detected(rebar_repo, tid, "ci")


def test_library_create_ticket_reads_env(rebar_repo: Path, monkeypatch):
    """The env read lives at the create_core seam, so the non-CLI library ingress
    gets the stamp too (no CLI arg parser involved)."""
    monkeypatch.setenv("REBAR_DETECTED_BY", "review-bot")
    tid = rebar.create_ticket("bug", "lib env bug", repo_root=str(rebar_repo))
    _assert_detected(rebar_repo, tid, "review-bot")
