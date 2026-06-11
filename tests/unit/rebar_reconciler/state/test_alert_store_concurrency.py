"""Regression tests for alert_store.append() concurrency.

Verifies that fcntl.LOCK_EX serialization in append() prevents interleaved
line writes when multiple reconciler instances write to the same JSONL file
concurrently. Without the flock guarantee, two simultaneous writes can
interleave bytes mid-line and corrupt JSONL boundaries.

See alert_store.append() — the fcntl.flock(LOCK_EX) call is the unit under
test.

Loader pattern: uses ``importlib.util.spec_from_file_location`` per the
established convention for tests in this directory (see conftest.py module
docstring for rationale).
"""

from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
ALERT_STORE_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "alert_store.py"
)


def _load_alert_store() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "alert_store_concurrency_module", ALERT_STORE_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def alert_store() -> ModuleType:
    return _load_alert_store()


# ---------------------------------------------------------------------------
# Concurrency regression test
# ---------------------------------------------------------------------------


def test_concurrent_appends_produce_no_malformed_lines(
    alert_store: ModuleType, tmp_path: Path
) -> None:
    """10 threads x 5 records = 50 valid JSONL lines, no interleaved bytes.

    Without the fcntl.LOCK_EX flock in append(), concurrent write() calls on
    POSIX file descriptors could interleave mid-line, producing malformed
    JSONL. The flock serializes writers so each record lands as one atomic
    line.
    """
    threads = 10
    records_per_thread = 5
    expected_total = threads * records_per_thread

    # Each record carries a unique (thread_id, seq) tuple so we can verify
    # nothing is lost or duplicated.
    def _append_batch(thread_id: int) -> None:
        for seq in range(records_per_thread):
            record = {
                "key": f"concurrent-{thread_id}-{seq}",
                "thread_id": thread_id,
                "seq": seq,
                "payload": "x" * 256,  # nontrivial size to widen the race window
            }
            alert_store.append(record, tmp_path)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(_append_batch, range(threads)))

    # Locate today's JSONL file (append() writes to today's UTC-dated file).
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    jsonl_files = list(store_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1, (
        f"Expected exactly one JSONL file in {store_dir}, got {jsonl_files}"
    )
    jsonl_file = jsonl_files[0]

    lines = jsonl_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected_total, (
        f"Expected {expected_total} lines, got {len(lines)}"
    )

    # Every line must be a valid JSON object with the expected fields.
    seen_keys: set[str] = set()
    for idx, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"Line {idx} is malformed JSONL "
                f"(concurrency corruption): {line!r} ({exc})"
            )
        assert isinstance(rec, dict), f"Line {idx} parsed but is not a dict: {rec!r}"
        assert "key" in rec, f"Line {idx} missing 'key': {rec!r}"
        assert "thread_id" in rec, f"Line {idx} missing 'thread_id': {rec!r}"
        assert "seq" in rec, f"Line {idx} missing 'seq': {rec!r}"
        seen_keys.add(rec["key"])

    # No record lost, no record duplicated.
    expected_keys = {
        f"concurrent-{t}-{s}"
        for t in range(threads)
        for s in range(records_per_thread)
    }
    assert seen_keys == expected_keys, (
        f"Missing keys: {expected_keys - seen_keys}; "
        f"unexpected keys: {seen_keys - expected_keys}"
    )
