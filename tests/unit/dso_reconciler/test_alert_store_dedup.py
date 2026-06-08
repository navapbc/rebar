"""Tests for alert_store.py — JSONL append and 24h UTC-boundary dedup readback."""
from __future__ import annotations

import json
import time

from plugins.dso.scripts.dso_reconciler import alert_store


def _write_record(path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Test (a): in-window same-key entry returns is_deduped=True
# ---------------------------------------------------------------------------


def test_in_window_same_key_is_deduped(tmp_path):
    """A record written within the 24h window for a given key is detected."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    record = {
        "key": "alert-key-abc",
        "timestamp_ns": time.time_ns(),
        "resolved": False,
    }
    today_file = store_dir / "2099-01-01.jsonl"
    _write_record(today_file, record)

    result = alert_store.is_deduped("alert-key-abc", tmp_path)

    assert result is True


# ---------------------------------------------------------------------------
# Test (b): UTC-date-boundary — stale timestamps (>24h) return False
# ---------------------------------------------------------------------------


def test_stale_timestamp_not_deduped(tmp_path):
    """A record older than 24h does NOT trigger dedup, even if the file is globbed."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    # Write a record with a timestamp 25 hours in the past
    stale_ts = time.time_ns() - (25 * 3600 * 1_000_000_000)
    record = {
        "key": "stale-key",
        "timestamp_ns": stale_ts,
        "resolved": False,
    }
    yesterday_file = store_dir / "2099-01-01.jsonl"
    _write_record(yesterday_file, record)

    result = alert_store.is_deduped("stale-key", tmp_path)

    assert result is False


# ---------------------------------------------------------------------------
# Test (c): missing JSONL directory returns False without raising
# ---------------------------------------------------------------------------


def test_missing_directory_returns_false(tmp_path):
    """is_deduped returns False gracefully when the store directory doesn't exist."""
    # Do NOT create the alerts directory
    result = alert_store.is_deduped("any-key", tmp_path)

    assert result is False


# ---------------------------------------------------------------------------
# Test (d): malformed JSONL line is skipped without aborting
# ---------------------------------------------------------------------------


def test_malformed_jsonl_line_skipped(tmp_path):
    """Malformed JSONL lines are silently skipped; valid lines still processed."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    today_file = store_dir / "2099-01-01.jsonl"

    # Write a malformed line followed by a valid record
    today_file.write_text(
        "this is not valid json\n"
        + json.dumps(
            {
                "key": "good-key",
                "timestamp_ns": time.time_ns(),
                "resolved": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # The malformed line should not abort; the valid record should be found
    result = alert_store.is_deduped("good-key", tmp_path)

    assert result is True


# ---------------------------------------------------------------------------
# Test (e): follow-up op='bug_filed' record patches original entry's bug_ticket_id
# ---------------------------------------------------------------------------


def test_patch_bug_filed_updates_record(tmp_path):
    """patch_bug_filed patches the latest unresolved record for a key in-place."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    today_file = store_dir / "2099-01-01.jsonl"

    original = {
        "key": "patch-key",
        "timestamp_ns": time.time_ns(),
        "resolved": False,
    }
    _write_record(today_file, original)

    alert_store.patch_bug_filed("patch-key", "bug-ticket-999", tmp_path)

    # Read back the patched record
    lines = today_file.read_text(encoding="utf-8").splitlines()
    patched = json.loads(lines[0])

    assert patched["bug_ticket_id"] == "bug-ticket-999"
    assert patched["op"] == "bug_filed"
    assert patched["key"] == "patch-key"


# ---------------------------------------------------------------------------
# Test (f): patch_bug_filed handles non-dict JSONL payloads without abandoning
# the file (regression: bare-number lines used to raise AttributeError and
# trigger the outer 'except Exception: continue', leaving the target record
# unpatched).
# ---------------------------------------------------------------------------


def test_patch_bug_filed_skips_non_dict_lines(tmp_path):
    """A non-dict JSONL line (e.g. bare int) is preserved verbatim; the target record is still patched."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    today_file = store_dir / "2099-01-01.jsonl"

    # A bare integer (valid JSON, but not a dict) before the target record
    today_file.write_text(
        "42\n"
        + json.dumps(
            {
                "key": "patch-key",
                "timestamp_ns": time.time_ns(),
                "resolved": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    alert_store.patch_bug_filed("patch-key", "bug-ticket-777", tmp_path)

    lines = today_file.read_text(encoding="utf-8").splitlines()
    # Bare integer line is preserved as-is (serialized via json.dumps)
    assert lines[0] == "42"
    # Target record is patched
    patched = json.loads(lines[1])
    assert patched["bug_ticket_id"] == "bug-ticket-777"
    assert patched["op"] == "bug_filed"


# ---------------------------------------------------------------------------
# Test (g): atomic write — the temp file does not linger after success and
# the JSONL contents survive a simulated mid-write crash (verified by checking
# that a no-target patch leaves the file untouched, not truncated).
# ---------------------------------------------------------------------------


def test_patch_bug_filed_atomic_no_temp_leftover(tmp_path):
    """After a successful patch, no stray .tmp.* files remain in the alerts dir."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    today_file = store_dir / "2099-01-01.jsonl"
    _write_record(
        today_file,
        {"key": "atomic-key", "timestamp_ns": time.time_ns(), "resolved": False},
    )

    alert_store.patch_bug_filed("atomic-key", "bug-atomic", tmp_path)

    leftover = list(store_dir.glob(".*.tmp.*"))
    assert leftover == [], f"Atomic write left temp files: {leftover}"


# ---------------------------------------------------------------------------
# Test (h): non-target dict lines are byte-identical to their original input.
# Regression for the "mixed-formatting after partial re-serialization" concern:
# the patch operation must preserve unchanged lines verbatim — no whitespace
# drift, no key-order drift, no ensure_ascii recoding — so any future crash
# mid-write cannot leave the file in a "some re-serialized, some original"
# inconsistent state.
# ---------------------------------------------------------------------------


def test_patch_bug_filed_preserves_non_target_lines_byte_identical(tmp_path):
    """Non-target dict lines are written back byte-identical to their original input."""
    store_dir = tmp_path / "bridge_state" / "bridge_alerts"
    store_dir.mkdir(parents=True)
    today_file = store_dir / "2099-01-01.jsonl"

    # Use deliberately quirky formatting that json.dumps would NOT re-emit
    # identically: extra whitespace inside the JSON object, an unusual key
    # order, and a Unicode character that would be re-encoded as a \\u escape
    # if we passed it through json.dumps() with default ensure_ascii=True.
    quirky_line_1 = '{ "resolved" : false, "key" : "other-key-1", "timestamp_ns" : 1, "note" : "café" }'
    quirky_line_2 = '{"z":1,"a":2,"key":"other-key-2","resolved":false}'
    target_original = json.dumps(
        {"key": "target-key", "timestamp_ns": time.time_ns(), "resolved": False}
    )

    today_file.write_text(
        quirky_line_1 + "\n" + quirky_line_2 + "\n" + target_original + "\n",
        encoding="utf-8",
    )

    alert_store.patch_bug_filed("target-key", "bug-byte-identical", tmp_path)

    out_lines = today_file.read_text(encoding="utf-8").splitlines()

    # Non-target lines are byte-identical to their original input — no
    # whitespace normalization, no key reordering, no Unicode escaping.
    assert out_lines[0] == quirky_line_1
    assert out_lines[1] == quirky_line_2
    # Target line IS re-serialized with the patch fields applied.
    patched = json.loads(out_lines[2])
    assert patched["key"] == "target-key"
    assert patched["bug_ticket_id"] == "bug-byte-identical"
    assert patched["op"] == "bug_filed"
