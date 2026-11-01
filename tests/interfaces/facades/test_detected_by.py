"""detected-by capture at bug creation — happy path (ticket d3ed-88e3-86a3-4918).

Automated filers export ``REBAR_DETECTED_BY``; ``rebar create`` stamps a
``detected_by`` field into the genesis CREATE event and the compiled state.
An optional ``--detected-by`` CLI flag overrides the env var. The read lives at
the ``create_core`` seam so every ingress (CLI, library, MCP) gets the stamp.

Observable oracle only: persisted ``CREATE.data`` bytes and the show projection.

Held-out oracles merged from test_detected_by_heldout: detected-by capture — held-out edge/E2E
oracle (ticket d3ed-88e3-86a3-4918).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
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
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        env=merged,
        check=False,
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


# ── held-out oracles (merged from test_detected_by_heldout) ──

_REPO_ROOT = Path(__file__).resolve().parents[3]


_SCHEMA_DIR = Path(rebar.__file__).resolve().parent / "schemas"


_U1 = "11111111-1111-4111-8111-111111111111"


def _create_data_heldout(rebar_repo: Path, ticket_id: str) -> dict:
    tracker = rebar_repo / ".tickets-tracker"
    matches = sorted(tracker.glob(f"{ticket_id}/*-CREATE.json"))
    assert len(matches) == 1
    return json.loads(matches[0].read_bytes())["data"]


def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:

    merged = subprocess_env({**(env or {})})
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def _state(rebar_repo: Path, tid: str) -> dict:
    return rebar.show_ticket(tid, repo_root=str(rebar_repo))


def test_absent_when_neither_env_nor_flag(rebar_repo: Path, monkeypatch):
    monkeypatch.delenv("REBAR_DETECTED_BY", raising=False)
    tid = rebar.create_ticket("bug", "plain bug", repo_root=str(rebar_repo))
    assert "detected_by" not in _create_data_heldout(rebar_repo, tid)
    assert "detected_by" not in _state(rebar_repo, tid)


def test_env_empty_string_treated_as_unset(rebar_repo: Path, monkeypatch):
    monkeypatch.setenv("REBAR_DETECTED_BY", "")
    tid = rebar.create_ticket("bug", "empty env bug", repo_root=str(rebar_repo))
    assert "detected_by" not in _create_data_heldout(rebar_repo, tid)
    assert "detected_by" not in _state(rebar_repo, tid)


def test_env_whitespace_only_treated_as_unset(rebar_repo: Path, monkeypatch):
    monkeypatch.setenv("REBAR_DETECTED_BY", "   \t")
    tid = rebar.create_ticket("bug", "ws env bug", repo_root=str(rebar_repo))
    assert "detected_by" not in _create_data_heldout(rebar_repo, tid)
    assert "detected_by" not in _state(rebar_repo, tid)


def test_mixed_case_with_padding_normalized(rebar_repo: Path, monkeypatch):
    monkeypatch.setenv("REBAR_DETECTED_BY", "  Canary ")
    tid = rebar.create_ticket("bug", "padded bug", repo_root=str(rebar_repo))
    assert _create_data_heldout(rebar_repo, tid).get("detected_by") == "canary"
    assert _state(rebar_repo, tid).get("detected_by") == "canary"


def test_unknown_token_stamped_verbatim(rebar_repo: Path, monkeypatch):
    """No enum enforcement: unknown channels never require a code change."""
    monkeypatch.setenv("REBAR_DETECTED_BY", "fuzzer")
    tid = rebar.create_ticket("bug", "fuzzer bug", repo_root=str(rebar_repo))
    assert _create_data_heldout(rebar_repo, tid).get("detected_by") == "fuzzer"


def test_create_never_fails_on_value(rebar_repo: Path, monkeypatch):
    """Even a bizarre value must not block filing (never-blocks contract)."""
    monkeypatch.setenv("REBAR_DETECTED_BY", "!!TOTALLY//weird VALUE!!")
    tid = rebar.create_ticket("bug", "weird value bug", repo_root=str(rebar_repo))
    assert tid, "create must succeed regardless of detected_by value"
    assert _create_data_heldout(rebar_repo, tid).get("detected_by") == "!!totally//weird value!!"


def test_explicit_empty_flag_suppresses_env(rebar_repo: Path):
    cp = _cli(
        "create",
        "bug",
        "explicit empty bug",
        "--detected-by=",
        env={"REBAR_DETECTED_BY": "canary"},
    )
    assert cp.returncode == 0, cp.stderr
    tid = _extract_id(cp.stdout)
    assert "detected_by" not in _create_data_heldout(rebar_repo, tid)


def test_mcp_create_reads_env(rebar_repo: Path, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from adapters import _unwrap

    from rebar.mcp_server import build_server

    monkeypatch.setenv("REBAR_DETECTED_BY", "ci")
    res = _unwrap(
        asyncio.run(
            build_server().call_tool("create_ticket", {"ticket_type": "bug", "title": "mcp bug"})
        )
    )
    tid = res["id"] if isinstance(res, dict) else res
    assert _create_data_heldout(rebar_repo, tid).get("detected_by") == "ci"


def test_legacy_create_without_field_reduces_without_key(tmp_path: Path):
    from rebar.reducer import reduce_ticket

    tdir = tmp_path / "legacy"
    tdir.mkdir()
    event = {
        "event_type": "CREATE",
        "uuid": _U1,
        "timestamp": 1700000000,
        "author": "someone",
        "env_id": "someenv",
        "data": {"id": "legacyid00000001", "ticket_type": "bug", "title": "legacy", "priority": 2},
    }
    (tdir / f"1700000000-{_U1}-CREATE.json").write_text(json.dumps(event))
    state = reduce_ticket(str(tdir))
    assert "detected_by" not in state, "present-only projection: absent field -> absent key"


def test_reducer_projects_field_present_only(tmp_path: Path):
    from rebar.reducer import reduce_ticket

    tdir = tmp_path / "withfield"
    tdir.mkdir()
    event = {
        "event_type": "CREATE",
        "uuid": _U1,
        "timestamp": 1700000000,
        "author": "someone",
        "env_id": "someenv",
        "data": {
            "id": "withfield0000001",
            "ticket_type": "bug",
            "title": "with field",
            "priority": 2,
            "detected_by": "canary",
        },
    }
    (tdir / f"1700000000-{_U1}-CREATE.json").write_text(json.dumps(event))
    state = reduce_ticket(str(tdir))
    assert state.get("detected_by") == "canary"


def test_ticket_state_schema_declares_optional_field():
    schema = json.loads((_SCHEMA_DIR / "ticket_state.schema.json").read_bytes())
    props = schema["properties"]
    assert "detected_by" in props, "compiled-state schema must declare detected_by"
    assert props["detected_by"].get("type") == "string"
    assert "detected_by" not in schema.get("required", []), "field must stay optional"


def test_env_var_registry_documents_detected_by():
    """docs/env-vars.md is generated + CI-drift-gated; the row must exist so the
    gate covers REBAR_DETECTED_BY from now on."""
    text = (_REPO_ROOT / "docs" / "env-vars.md").read_text()
    assert "REBAR_DETECTED_BY" in text


def test_e2e_flag_only_cli_roundtrip(rebar_repo: Path, monkeypatch):
    monkeypatch.delenv("REBAR_DETECTED_BY", raising=False)
    cp = _cli("create", "bug", "e2e flag bug", "--detected-by", "human-audit")
    assert cp.returncode == 0, cp.stderr
    tid = _extract_id(cp.stdout)
    assert _create_data_heldout(rebar_repo, tid).get("detected_by") == "human-audit"
    assert _state(rebar_repo, tid).get("detected_by") == "human-audit"
