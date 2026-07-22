"""RED test: the fetcher flags a Jira workflow status with no reconciler mapping.

When the fetcher builds a snapshot and a Jira issue carries a workflow status
absent from ``config.jira_to_local_status`` (e.g. an ``Backlog`` status added on the
Jira side before the reconciler has a mapping for it), the fetcher MUST surface it
proactively — emit an observable ``fetcher-unmapped-jira-status`` BRIDGE_ALERT via
``alert_store.append`` naming the offending status — so a newly-added Jira status
is flagged for a mapping at snapshot-build time, rather than being discovered only
downstream when it reaches an outbound mutation and trips the status preflight
(``reconcile.preflight_status_mapping``).

RED expectation: the current fetcher builds the snapshot but never inspects status
names, so no such alert is emitted — the test fails RED. GREEN: the fetcher detects
the unmapped status and emits the alert (once per distinct status), while a mapped
status is never flagged.
"""

from __future__ import annotations

import importlib.util
import json
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
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fetcher():
    if not FETCHER_PATH.exists():
        pytest.fail(f"fetcher.py not found at {FETCHER_PATH}")
    return _load_fetcher()


@pytest.fixture(autouse=True)
def _protect_alert_store():
    # The fetcher resolves alert_store via a shared sys.modules dotted key; save/
    # restore it so this test is order-independent w.r.t. sibling tests that also
    # touch that key (mirrors test_fetcher_dedup_observable.py).
    key = "rebar_reconciler.alert_store"
    saved = sys.modules.pop(key, None)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules[key] = saved
        else:
            sys.modules.pop(key, None)


class _StatusPagingClient:
    """Stub ACLI client: the active query returns one MAPPED ('To Do') and one
    UNMAPPED ('Backlog') issue; the recent-Done query (ORDER BY) returns nothing."""

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50):
        if "ORDER BY" in jql or start_at != 0:
            return []
        return [
            {
                "key": "DIG-1",
                "fields": {
                    "summary": "mapped",
                    "status": {"name": "To Do"},
                    "updated": "2026-07-06T10:00:00Z",
                },
            },
            {
                "key": "DIG-2",
                "fields": {
                    "summary": "unmapped",
                    "status": {"name": "Backlog"},
                    "updated": "2026-07-06T10:00:00Z",
                },
            },
        ]


def _make_acli_mock():
    class _Client(_StatusPagingClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__()

    # S4: _load_acli returns the transport instance directly.
    return _Client()


def test_unmapped_jira_status_emits_alert(tmp_path, fetcher):
    mock_acli = _make_acli_mock()
    captured: list[dict] = []
    stub_alert_store = types.SimpleNamespace(
        append=lambda record, repo_root: captured.append(record),
        is_deduped=lambda key, repo_root: False,
    )

    with (
        patch.object(fetcher, "_load_acli", return_value=mock_acli),
        patch.object(fetcher, "_load_alert_store", return_value=stub_alert_store),
    ):
        snapshot_path = fetcher.fetch_snapshot("2026-07-06-status-pass", repo_root=tmp_path)

    snapshot = json.loads(snapshot_path.read_text())
    assert "DIG-1" in snapshot and "DIG-2" in snapshot

    unmapped = [r for r in captured if r.get("kind") == "fetcher-unmapped-jira-status"]
    assert unmapped, (
        "Expected a fetcher-unmapped-jira-status alert naming the Backlog status. "
        f"Captured alerts: {captured!r}"
    )
    assert "Backlog" in json.dumps(unmapped[0]), (
        f"alert must name the unmapped status 'Backlog'; got {unmapped[0]!r}"
    )
    # A MAPPED status ('To Do') must never be flagged as unmapped.
    assert all("To Do" not in json.dumps(r) for r in unmapped), (
        f"mapped status 'To Do' must not be flagged as unmapped; got {unmapped!r}"
    )


def test_all_mapped_statuses_emit_no_alert(tmp_path, fetcher):
    """Control: when every issue's status IS mapped, no unmapped-status alert fires."""

    class _AllMappedClient:
        def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50):
            if "ORDER BY" in jql or start_at != 0:
                return []
            return [
                {
                    "key": "DIG-1",
                    "fields": {
                        "status": {"name": "In Progress"},
                        "updated": "2026-07-06T10:00:00Z",
                    },
                },
                {
                    "key": "DIG-2",
                    "fields": {"status": {"name": "Done"}, "updated": "2026-07-06T10:00:00Z"},
                },
            ]

    class _Client(_AllMappedClient):
        def __init__(self, *_args, **_kwargs):
            super().__init__()

    # S4: _load_acli returns the transport instance directly.
    mock_acli = _Client()

    captured: list[dict] = []
    stub_alert_store = types.SimpleNamespace(
        append=lambda record, repo_root: captured.append(record),
        is_deduped=lambda key, repo_root: False,
    )

    with (
        patch.object(fetcher, "_load_acli", return_value=mock_acli),
        patch.object(fetcher, "_load_alert_store", return_value=stub_alert_store),
    ):
        fetcher.fetch_snapshot("2026-07-06-all-mapped", repo_root=tmp_path)

    unmapped = [r for r in captured if r.get("kind") == "fetcher-unmapped-jira-status"]
    assert not unmapped, (
        f"no unmapped-status alert expected when all statuses map; got {unmapped!r}"
    )


def test_unmapped_status_alert_deduped_across_passes(tmp_path, fetcher):
    """Dedup contract (plan-review advisory E4): the emitted record carries
    ``key`` == the dedup key AND a non-zero ``timestamp_ns``, so the REAL
    ``alert_store.is_deduped`` suppresses a repeat within the 24h window — the
    alert fires ONCE across passes, not every ~20-minute pass.

    Exercises the real alert_store (no ``is_deduped`` stub) so the contract the
    advisory flagged as unverifiable is actually verified end-to-end: a record
    missing ``key`` or ``timestamp_ns`` would re-fire and fail this test.
    """
    mock_acli = _make_acli_mock()  # returns the unmapped 'Backlog' issue

    # Do NOT patch _load_alert_store: use the real one so append + is_deduped hit
    # the real JSONL store under tmp_path/bridge_state/bridge_alerts/.
    with patch.object(fetcher, "_load_acli", return_value=mock_acli):
        fetcher.fetch_snapshot("2026-07-06-dedup-pass-1", repo_root=tmp_path)
        fetcher.fetch_snapshot("2026-07-06-dedup-pass-2", repo_root=tmp_path)

    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
    records: list[dict] = []
    for jf in sorted(alerts_dir.glob("*.jsonl")):
        for line in jf.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))

    unmapped = [r for r in records if r.get("kind") == "fetcher-unmapped-jira-status"]
    assert len(unmapped) == 1, (
        f"unmapped-status alert must be deduped to ONE across two passes; got {unmapped!r}"
    )
    rec = unmapped[0]
    assert rec["key"] == "unmapped-jira-status:Backlog", (
        f"record 'key' must equal the dedup lookup key for is_deduped to work; got {rec!r}"
    )
    assert rec.get("timestamp_ns", 0) > 0, (
        f"record must carry a non-zero 'timestamp_ns' for the 24h dedup window; got {rec!r}"
    )
