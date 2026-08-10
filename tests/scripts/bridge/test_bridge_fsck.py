"""Happy-path oracle for the offline bridge fsck contract (ticket 030f)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.scripts]

_ENV_ID = "bbbbbbbb-0000-4000-8000-000000000002"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _write_event(tracker: Path, local_id: str, event_type: str = "CREATE") -> None:
    ticket_dir = tracker / local_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_type": event_type,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "timestamp": 1_800_000_000_000_000_000,
        "author": "test-author",
        "env_id": _ENV_ID,
        "data": {
            "ticket_type": "task",
            "title": "Known ticket",
            "parent_id": None,
        },
    }
    (ticket_dir / f"1-{payload['uuid']}-{event_type}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _init_committed_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "test@example.com")
    _git(tracker, "config", "user.name", "Test")
    return tracker


def _commit(tracker: Path) -> None:
    _git(tracker, "add", ".")
    _git(tracker, "commit", "-q", "-m", "fixture")


def _write_consistent_binding(tracker: Path, local_id: str, jira_key: str) -> None:
    bridge_state = tracker / ".bridge_state"
    bridge_state.mkdir()
    (bridge_state / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    local_id: {
                        "state": "confirmed",
                        "jira_key": jira_key,
                        "baseline": {},
                    }
                },
                "reverse": {jira_key: local_id},
            }
        ),
        encoding="utf-8",
    )


def test_clean_committed_store_returns_only_the_new_contract(tmp_path: Path) -> None:
    """Known committed events plus consistent indexes are exactly clean."""
    from rebar._engine_support import bridge_fsck

    tracker = _init_committed_tracker(tmp_path)
    _write_event(tracker, "loc-clean")
    _write_consistent_binding(tracker, "loc-clean", "REB-1")
    _commit(tracker)

    findings = bridge_fsck.audit_bridge_mappings(tracker)

    assert set(findings) == {"unknown_event_types", "binding_drift", "store_integrity"}
    assert findings["unknown_event_types"] == []
    assert findings["store_integrity"] == []


def test_clean_cli_json_uses_the_new_contract_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human/JSON command path consumes the same clean offline result."""
    from rebar._engine_support import bridge_fsck

    tracker = _init_committed_tracker(tmp_path)
    _write_event(tracker, "loc-cli")
    _write_consistent_binding(tracker, "loc-cli", "REB-2")
    _commit(tracker)

    rc = bridge_fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert set(out) == {"unknown_event_types", "binding_drift", "store_integrity"}
    assert out["store_integrity"] == []
