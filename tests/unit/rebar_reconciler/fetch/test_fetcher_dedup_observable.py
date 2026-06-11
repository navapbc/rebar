"""RED test for fetcher cross-page dedup observability (task aa11-440a).

Asserts that when the fetcher encounters the same issue key on two different
pages of the paginated ACLI response, it MUST:

  1. Deduplicate — the resulting snapshot contains exactly one record for the
     duplicated key (the snapshot is a key -> fields mapping, so dedup is
     implicit, but the test pins the invariant explicitly).
  2. Emit an observable BRIDGE_ALERT of kind ``fetcher-dedup-suppressed`` via
     ``alert_store.append`` so operators can detect when remote pagination has
     gone unstable. The alert record must reference the duplicated issue key
     (``DIG-100``) in a structured field.

RED expectation against current fetcher.py: the existing implementation
silently overwrites the duplicate when building the ``snapshot`` dict and
never calls ``alert_store.append`` — so this test fails RED. The GREEN fix is
to detect the cross-page key collision and emit the observability alert
(without dropping data semantics).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FETCHER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"
)


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
    """Isolate the shared ``alert_store`` dotted key across tests (bug 4cc1).

    ``test_bridge_alerts_surface.py`` registers its OWN module object under
    ``rebar_reconciler.alert_store`` (via namespace stubs +
    importlib). ``fetcher._load_alert_store()`` resolves that same dotted key at
    call time. When the bridge-alerts test runs first, the leftover object in
    ``sys.modules`` diverges from the one this test patches — silently defeating
    the patch and producing an ORDER-DEPENDENT failure (passes alone, fails in
    the full suite). Snapshot and clear the key around each test so loads are
    fresh and no foreign object leaks in. The dedup test below additionally
    patches the ``_load_alert_store`` seam directly, which is order-independent
    on its own; this fixture protects every other test in the module too.
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
    """Stub ACLI client that returns DIG-100 on BOTH page 1 and page 2.

    Page 1: [DIG-1, ..., DIG-100]  (100 issues; DIG-100 at end)
    Page 2: [DIG-100, DIG-101]     (DIG-100 reappears with a NEWER timestamp)
    Page 3: empty -> terminates

    The mismatched ``updated`` timestamp on the two DIG-100 records mirrors the
    real-world failure mode (remote re-paged because an issue was updated
    mid-fetch).
    """

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
        self.calls.append(
            {"jql": jql, "start_at": start_at, "max_results": max_results}
        )
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

    mock_acli = types.ModuleType("acli_integration")
    mock_acli.AcliClient = _Client
    return mock_acli, client_holder


def test_dedup_suppression_emits_alert(tmp_path, fetcher):
    """Cross-page duplicate of DIG-100 is deduped AND emits a fetcher-dedup-suppressed alert.

    The alert MUST be written via ``alert_store.append`` (the canonical
    observability channel) with ``kind="fetcher-dedup-suppressed"`` and a
    structured reference to the duplicated key ``DIG-100``.
    """
    mock_acli, _holder = _make_acli_mock()

    captured: list[dict] = []

    def _capture_append(record, repo_root):
        captured.append(record)

    # Patch the _load_alert_store SEAM rather than
    # `rebar_reconciler.alert_store.append` directly: the
    # fetcher resolves alert_store via this helper at call time, and patching
    # the seam is independent of which module object happens to occupy the
    # shared sys.modules dotted key (the source of the order-dependent failure,
    # bug 4cc1). The stub exposes the single attribute fetcher uses (`append`).
    stub_alert_store = types.SimpleNamespace(append=_capture_append)

    with (
        patch.object(fetcher, "_load_acli", return_value=mock_acli),
        patch.object(fetcher, "_load_alert_store", return_value=stub_alert_store),
    ):
        snapshot_path = fetcher.fetch_snapshot(
            "2026-05-24-dedup-pass", repo_root=tmp_path
        )

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
    dedup_alerts = [
        rec for rec in captured if rec.get("kind") == "fetcher-dedup-suppressed"
    ]
    assert dedup_alerts, (
        "Expected at least one BRIDGE_ALERT with kind='fetcher-dedup-suppressed' "
        f"to be appended to alert_store. Captured records: {captured!r}"
    )

    # 3. The alert payload must reference the duplicated key DIG-100.
    alert = dedup_alerts[0]
    payload_str = json.dumps(alert)
    assert "DIG-100" in payload_str, (
        f"fetcher-dedup-suppressed alert must reference duplicated key DIG-100; "
        f"got: {alert!r}"
    )
