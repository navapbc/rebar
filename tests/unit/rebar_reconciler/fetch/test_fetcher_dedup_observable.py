"""Pin observable cross-page deduplication for ``DIG-100``.

When a newer ``DIG-100`` reappears on page two, the snapshot retains one record
and ``alert_store.append`` receives a ``fetcher-dedup-suppressed`` alert naming
that key.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def fetcher():
    if not FETCHER_PATH.exists():
        pytest.fail(f"fetcher.py not found at {FETCHER_PATH}")
    return _load_fetcher()


@pytest.fixture(autouse=True)
def _isolate_alert_store_module():
    """Keep the shared alert-store module key order-independent.

    Sibling tests may install another object at ``rebar_reconciler.alert_store``;
    snapshot and restore it while this module patches the loader seam directly.
    """
    key = "rebar_reconciler.alert_store"
    saved = sys.modules.pop(key, None)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules[key] = saved
        else:
            sys.modules.pop(key, None)


class _DuplicatingPaginatingClient:
    """Return 100 issues, then newer ``DIG-100`` plus ``DIG-101``, then stop."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._page1 = [
            {
                "key": f"DIG-{i}",
                "fields": {"summary": f"issue {i}", "updated": "2026-05-24T10:00:00Z"},
            }
            for i in range(1, 101)
        ]
        self._page2 = [
            {
                "key": "DIG-100",
                "fields": {"summary": "issue 100", "updated": "2026-05-24T10:05:00Z"},
            },
            {
                "key": "DIG-101",
                "fields": {"summary": "issue 101", "updated": "2026-05-24T10:01:00Z"},
            },
        ]

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50):
        self.calls.append({"jql": jql, "start_at": start_at, "max_results": max_results})
        if start_at == 0:
            return list(self._page1)
        if start_at == 100:
            return list(self._page2)
        return []


def _make_acli_mock():
    client_holder: dict[str, _DuplicatingPaginatingClient] = {}

    class _Client(_DuplicatingPaginatingClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__()
            client_holder["client"] = self

    # S4: _load_acli returns the transport instance directly.
    return _Client(), client_holder


def test_dedup_suppression_emits_alert(tmp_path, fetcher):
    """Keep one DIG-100 and append a dedup-suppressed alert naming it."""
    mock_acli, _holder = _make_acli_mock()

    captured: list[dict] = []

    def _capture_append(record, repo_root):
        captured.append(record)

    # Patch the loader seam so whichever object occupies the shared module key
    # is irrelevant; the stub supplies the fetcher's sole ``append`` API.
    stub_alert_store = types.SimpleNamespace(append=_capture_append)

    with (
        patch.object(fetcher, "_load_acli", return_value=mock_acli),
        patch.object(fetcher, "_load_alert_store", return_value=stub_alert_store),
    ):
        snapshot_path = fetcher.fetch_snapshot("2026-05-24-dedup-pass", repo_root=tmp_path)

    # 1. The snapshot file must exist and contain exactly one DIG-100 record.
    assert snapshot_path.exists()
    import json

    snapshot = json.loads(snapshot_path.read_text())
    assert "DIG-100" in snapshot
    dig100_count = sum(1 for k in snapshot if k == "DIG-100")
    assert dig100_count == 1, (
        f"Cross-page duplicate of DIG-100 was not deduped: count={dig100_count}"
    )

    # 2. An observable alert MUST have been emitted via alert_store.append.
    dedup_alerts = [rec for rec in captured if rec.get("kind") == "fetcher-dedup-suppressed"]
    assert dedup_alerts, (
        "Expected at least one BRIDGE_ALERT with kind='fetcher-dedup-suppressed' "
        f"to be appended to alert_store. Captured records: {captured!r}"
    )

    # 3. The alert payload must reference the duplicated key DIG-100.
    alert = dedup_alerts[0]
    payload_str = json.dumps(alert)
    assert "DIG-100" in payload_str, (
        f"fetcher-dedup-suppressed alert must reference duplicated key DIG-100; got: {alert!r}"
    )
