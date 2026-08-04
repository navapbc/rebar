from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).parents[2] / "infra" / "scripts" / "reclaim_bridge_history.py"


def _load_script() -> ModuleType:
    assert SCRIPT.is_file(), "reclaim_bridge_history.py is not implemented"
    spec = importlib.util.spec_from_file_location("reclaim_bridge_history", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _data(payload: bytes) -> bytes:
    return f"data {len(payload)}\n".encode() + payload + b"\n"


def test_filter_export_stream_removes_only_bridge_file_commands() -> None:
    module = _load_script()
    message = b"ordinary commit message"
    keep = b"M 100644 " + (b"a" * 40) + b" tickets/event.json\n"
    drop_state = b"M 100644 " + (b"b" * 40) + b" .bridge_state/bindings.json\n"
    drop_backup = b"D .bridge_state.bak-retarget/prev_snapshot.json\n"
    exported = (
        b"commit refs/heads/tickets\n"
        b"mark :1\n"
        b"committer Test <test@example.com> 1 +0000\n"
        + _data(message)
        + keep
        + drop_state
        + drop_backup
        + b"\n"
    )
    destination = io.BytesIO()

    module.filter_export_stream(io.BytesIO(exported), destination)

    rewritten = destination.getvalue()
    assert _data(message) in rewritten
    assert keep in rewritten
    assert drop_state not in rewritten
    assert drop_backup not in rewritten


def test_filter_export_stream_treats_data_payload_by_declared_byte_length() -> None:
    module = _load_script()
    message = (
        b"M 100644 "
        + (b"c" * 40)
        + b" .bridge_state/looks-like-a-command.json\n"
        + b"D .bridge_state.bak-retarget/also-message-data.json\n"
        + b"final message line"
    )
    kept = b"M 100644 " + (b"d" * 40) + b" tickets/kept.json\n"
    exported = (
        b"commit refs/heads/tickets\n"
        b"mark :7\n"
        b"committer Test <test@example.com> 7 +0000\n" + _data(message) + kept + b"\n"
    )
    destination = io.BytesIO()

    module.filter_export_stream(io.BytesIO(exported), destination)

    rewritten = destination.getvalue()
    assert _data(message) in rewritten
    assert kept in rewritten
