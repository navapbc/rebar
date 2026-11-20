"""patch_bug_filed and append serialize on the same lock (bug equable-overseas-hoiho).

patch_bug_filed rewrites a day's alert file from what it read. An alert appended between
that read and the replace would be dropped by the replace unless both hold the append lock.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from rebar.reconciler import alert_store

pytestmark = pytest.mark.unit


def _records(repo_root: Path) -> list[dict]:
    [day] = sorted(alert_store._store_dir(repo_root).glob("*.jsonl"))
    return [json.loads(line) for line in day.read_text(encoding="utf-8").splitlines()]


def test_an_append_during_a_patch_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_store.append({"key": "first", "msg": "needs a bug"}, tmp_path)

    in_write = threading.Event()
    release = threading.Event()
    real_write = alert_store._atomic_write

    def paused_write(path: Path, content: str) -> None:
        in_write.set()
        assert release.wait(10), "the test never released the paused patch"
        real_write(path, content)

    monkeypatch.setattr(alert_store, "_atomic_write", paused_write)
    errors: list[BaseException] = []

    def run(fn) -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    patcher = threading.Thread(
        target=run, args=(lambda: alert_store.patch_bug_filed("first", "bug-1", tmp_path),)
    )
    patcher.start()
    assert in_write.wait(10), "patch_bug_filed never reached its rewrite"

    appender = threading.Thread(
        target=run, args=(lambda: alert_store.append({"key": "second"}, tmp_path),)
    )
    appender.start()
    # Unserialized, the append finishes here and the paused rewrite then drops it.
    # Serialized, the append waits for the lock and this join times out.
    appender.join(timeout=2)
    release.set()
    patcher.join(10)
    appender.join(10)

    assert not errors, errors
    keys = [record["key"] for record in _records(tmp_path)]
    assert keys == ["first", "second"]
    assert _records(tmp_path)[0]["bug_ticket_id"] == "bug-1"
