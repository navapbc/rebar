"""Bug 8384: alert-store dedup must actually fire — ``append`` stamps ``timestamp_ns``.

``alert_store.is_deduped`` suppresses a same-key unresolved alert written within a 24h
window by comparing ``now - rec["timestamp_ns"]``. The record's ``timestamp_ns`` was owned
by nobody: ``append`` wrote the caller's dict verbatim and several callers (notably
``BindingRepository.alert`` and a couple of fetcher paths) never stamped it, so
``rec.get("timestamp_ns", 0)`` defaulted to 0, ``now - 0`` always exceeded the window, and
dedup never fired.

Fix: ``append`` stamps ``timestamp_ns`` centrally when the caller did not, so EVERY dedup
path works. A caller-supplied ``timestamp_ns`` is preserved (so the dozen callers that
already stamp are unchanged, and a test can inject an out-of-window timestamp).

These are seam-level oracles over ``alert_store`` directly, complementing the
through-the-real-path oracle in
``tests/unit/rebar_reconciler/state/test_binding_repository.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "alert_store.py"
)


def _load_alert_store():
    spec = importlib.util.spec_from_file_location("rebar_reconciler.alert_store", _MOD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_24H_NS = 24 * 3600 * 1_000_000_000


def test_append_without_timestamp_then_is_deduped_fires(tmp_path):
    """AC#2 seam: a record the caller did NOT stamp is still deduped on the next check —
    ``append`` stamped ``timestamp_ns``, so the same-key alert lands inside the window."""
    alert_store = _load_alert_store()

    alert_store.append({"key": "k-8384", "resolved": False, "kind": "demo"}, tmp_path)

    assert alert_store.is_deduped("k-8384", tmp_path) is True


def test_appended_record_carries_a_timestamp(tmp_path):
    """AC#1/AC#5 seam: the persisted record gains an integer ``timestamp_ns`` stamp."""
    alert_store = _load_alert_store()

    alert_store.append({"key": "k-stamp", "resolved": False}, tmp_path)

    lines = (tmp_path / "bridge_state" / "bridge_alerts").glob("*.jsonl")
    records = []
    import json

    for path in lines:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    assert len(records) == 1
    assert isinstance(records[0]["timestamp_ns"], int)


def test_caller_supplied_timestamp_is_preserved_and_out_of_window_still_appends(tmp_path):
    """AC#3: a record whose ``timestamp_ns`` predates the window does NOT suppress — a
    genuinely recurring condition is never silenced forever. Proves the central stamp is
    preserve-if-present (the injected out-of-window value survives)."""
    import time

    alert_store = _load_alert_store()
    old = time.time_ns() - (_24H_NS + 10_000_000_000)  # ~24h+10s ago

    alert_store.append({"key": "k-old", "resolved": False, "timestamp_ns": old}, tmp_path)

    assert alert_store.is_deduped("k-old", tmp_path) is False


def test_legacy_record_without_timestamp_remains_readable(tmp_path):
    """AC#4: a pre-existing on-disk record with no ``timestamp_ns`` must not start
    throwing during the dedup scan; it reads as out-of-window (ts defaults to 0)."""
    import json

    alert_store = _load_alert_store()
    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
    alerts_dir.mkdir(parents=True)
    (alerts_dir / "2020-01-01.jsonl").write_text(
        json.dumps({"key": "k-legacy", "resolved": False}) + "\n", encoding="utf-8"
    )

    assert alert_store.is_deduped("k-legacy", tmp_path) is False
