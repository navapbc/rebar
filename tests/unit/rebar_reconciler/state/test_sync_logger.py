"""Unit tests for rebar_reconciler/sync_logger.py — structured JSON-line logger.

Tests follow the importlib-based loading convention used by the reconciler
test tree (see conftest.py docstring).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
SL_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "sync_logger.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def sl_mod() -> ModuleType:
    return _load_module("sync_logger", SL_PATH)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_log_writes_jsonl(sl_mod: ModuleType, tmp_path: Path) -> None:
    """Each log() call writes one valid JSON line."""
    log_file = tmp_path / "test.jsonl"
    logger = sl_mod.SyncLogger(log_file)
    logger.log("sync_pass_start", pass_id="p1", mode="live")
    logger.log("outbound_create", local_id="abc-1", jira_key="DIG-100", fields={"title": "T"})
    logger.close()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2

    for line in lines:
        parsed = json.loads(line)
        assert "ts" in parsed
        assert "event" in parsed
        assert parsed["ts"].endswith("Z")


def test_event_types(sl_mod: ModuleType, tmp_path: Path) -> None:
    """Each event type produces expected fields in the log entry."""
    log_file = tmp_path / "events.jsonl"
    logger = sl_mod.SyncLogger(log_file)

    logger.log("sync_pass_start", pass_id="p1", mode="dry-run")
    logger.log(
        "sync_pass_end",
        pass_id="p1",
        mutations_computed=10,
        mutations_applied=5,
        duration_s=1.5,
    )
    logger.log("outbound_create", local_id="a", jira_key="D-1", fields={"title": "X"})
    logger.log("outbound_update", local_id="a", jira_key="D-1", changed_fields=["status"])
    logger.log("inbound_update", local_id="b", jira_key="D-2", changed_fields=["priority"])
    logger.log("comment_sync", local_id="a", jira_key="D-1", action="add", comment_id="c1")
    logger.log("label_sync", local_id="a", jira_key="D-1", action="add", label="team:x")
    logger.log("link_sync", from_id="a", to_id="b", action="add", link_type="blocks")
    logger.log("binding_create", local_id="a", jira_key="D-1")
    logger.log(
        "error",
        local_id="a",
        jira_key="D-1",
        operation="create",
        error="timeout",
        traceback="...",
    )
    logger.log("rate_limit", endpoint="/rest/api/2/issue", retry_after_s=30)
    logger.close()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 11

    events = [json.loads(line) for line in lines]
    event_types = [e["event"] for e in events]
    assert "sync_pass_start" in event_types
    assert "sync_pass_end" in event_types
    assert "outbound_create" in event_types
    assert "error" in event_types
    assert "rate_limit" in event_types

    # Check specific fields on pass_start
    start = next(e for e in events if e["event"] == "sync_pass_start")
    assert start["pass_id"] == "p1"
    assert start["mode"] == "dry-run"

    # Check specific fields on error
    err = next(e for e in events if e["event"] == "error")
    assert err["error"] == "timeout"
    assert err["operation"] == "create"

    # Check specific fields on rate_limit
    rl = next(e for e in events if e["event"] == "rate_limit")
    assert rl["retry_after_s"] == 30


def test_close_flushes(sl_mod: ModuleType, tmp_path: Path) -> None:
    """After close(), all written data is persisted to disk."""
    log_file = tmp_path / "flush.jsonl"
    logger = sl_mod.SyncLogger(log_file)
    logger.log("binding_create", local_id="x", jira_key="D-99")
    logger.close()

    # File should be readable and complete after close
    content = log_file.read_text().strip()
    assert content  # non-empty
    parsed = json.loads(content)
    assert parsed["event"] == "binding_create"

    # Verify file handle is actually closed by trying to write
    # (this should raise because the fd is closed)
    with pytest.raises((ValueError, OSError)):
        logger.log("should_fail")


def test_append_mode(sl_mod: ModuleType, tmp_path: Path) -> None:
    """Logger opens in append mode — second logger adds to existing content."""
    log_file = tmp_path / "append.jsonl"

    logger1 = sl_mod.SyncLogger(log_file)
    logger1.log("sync_pass_start", pass_id="p1", mode="live")
    logger1.close()

    logger2 = sl_mod.SyncLogger(log_file)
    logger2.log("sync_pass_start", pass_id="p2", mode="live")
    logger2.close()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["pass_id"] == "p1"
    assert json.loads(lines[1])["pass_id"] == "p2"


def test_context_manager_closes_on_exit(sl_mod: ModuleType, tmp_path: Path) -> None:
    """SyncLogger supports the context manager protocol and closes the file on exit."""
    log_file = tmp_path / "ctx.jsonl"
    with sl_mod.SyncLogger(log_file) as logger:
        logger.log("sync_pass_start", pass_id="p1", mode="live")

    # After exiting the with-block, the file handle should be closed
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "sync_pass_start"

    # Writing after context exit should raise
    with pytest.raises((ValueError, OSError)):
        logger.log("should_fail")


def test_non_serializable_values(sl_mod: ModuleType, tmp_path: Path) -> None:
    """Non-serializable values are converted via default=str."""
    log_file = tmp_path / "nonser.jsonl"
    logger = sl_mod.SyncLogger(log_file)
    logger.log("error", some_path=Path("/tmp/foo"))
    logger.close()

    parsed = json.loads(log_file.read_text().strip())
    assert "/tmp/foo" in parsed["some_path"]
