from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
from typing import Any


def _parse_block(block: Any) -> Any:
    import json

    text = getattr(block, "text", block)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return text


def _unwrap_call_tool(result: Any) -> Any:
    if isinstance(result, tuple):
        structured = result[1] if len(result) > 1 else None
        if isinstance(structured, dict):
            return structured.get("result", structured)
        result = result[0]
    if not result:
        return None
    if len(result) == 1:
        return _parse_block(result[0])
    return [_parse_block(block) for block in result]


def test_mcp_log_session_reclaims_dead_compact_sweep_stamp_after_fcntl(
    rebar_repo: Path, monkeypatch
) -> None:
    """MCP writes must trust the free kernel lock over a stale mkdir owner stamp."""
    from rebar._store import lock as _lock
    from rebar._store import lock_owner as _owner
    from rebar.mcp_server import build_server

    tracker = rebar_repo / ".tickets-tracker"
    lock_file = tracker / _lock.WRITE_LOCK_NAME
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    owner = lock_dir / _owner._MKDIR_OWNER_FILE
    lock_dir.mkdir()
    owner.write_text(
        (
            f"{_owner._STAMP_V2_PREFIX} host={_owner._host_identity()} "
            f"ns={_owner._read_pid_namespace_id() or _owner._STAMP_UNKNOWN} "
            "pid=49003 start=- op=compact-sweep"
        ),
        encoding="utf-8",
    )

    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    kwdefaults = _lock.write_lock.__wrapped__.__kwdefaults__
    assert kwdefaults is not None
    monkeypatch.setitem(kwdefaults, "timeout", 1)
    monkeypatch.setitem(kwdefaults, "attempts", 1)
    monkeypatch.setenv("REBAR_LOCK_RETRIES", "0")
    monkeypatch.setattr(_owner, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(_owner, "_process_start_time", lambda _pid: None)

    acquired_fcntl = False
    original_acquire_fcntl = _lock._acquire_fcntl

    def traced_acquire_fcntl(path: str, deadline: float) -> int:
        nonlocal acquired_fcntl
        fd = original_acquire_fcntl(path, deadline)
        acquired_fcntl = fd != -1
        return fd

    monkeypatch.setattr(_lock, "_acquire_fcntl", traced_acquire_fcntl)

    result = _unwrap_call_tool(
        asyncio.run(
            build_server().call_tool(
                "log_session",
                {"entry": "dead compact-sweep stamp repro", "summary": "lock repro"},
            )
        )
    )

    assert acquired_fcntl, "the test must exercise mkdir reclaim after taking fcntl"
    assert isinstance(result, dict)
    assert result["id"]
    assert not lock_dir.exists(), "stale mkdir holder was not reclaimed"
