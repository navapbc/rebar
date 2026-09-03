"""Conflict-safe reconciler sidecar persistence.

The sidecar stores are complete JSON documents, but their writes represent
key-level facts. Two pass instances loaded from the same baseline must merge
distinct-key changes instead of snapshot-clobbering, while same-key conflicts
resolve by the actual order in which writers hold the shared sidecar lock.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A bare tracker dir with the ``.bridge_state`` sidecar directory present."""
    d = tmp_path / ".tickets-tracker"
    (d / ".bridge_state").mkdir(parents=True)
    return d


def _records(path: Path) -> dict[str, dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def _save_in_threads(stores: list[Any], final_path: Path) -> list[BaseException]:
    """Run ``save()`` concurrently while a reader proves complete-document visibility."""
    stop = threading.Event()
    errors: list[BaseException] = []
    read_errors: list[BaseException] = []

    def reader() -> None:
        while not stop.is_set():
            if final_path.exists():
                try:
                    payload = json.loads(final_path.read_text(encoding="utf-8"))
                    assert isinstance(payload, dict)
                except BaseException as exc:  # noqa: BLE001 — surfaced to the caller
                    read_errors.append(exc)
                    stop.set()
                    return

    def worker(store: Any) -> None:
        try:
            store.save()
        except BaseException as exc:  # noqa: BLE001 — fail-open save should not raise
            errors.append(exc)

    reader_thread = threading.Thread(target=reader)
    writer_threads = [threading.Thread(target=worker, args=(store,)) for store in stores]
    reader_thread.start()
    for thread in writer_threads:
        thread.start()
    for thread in writer_threads:
        thread.join()
    stop.set()
    reader_thread.join()
    return errors + read_errors


def _peer_store(tracker: Path):
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    return module, module.PeerConfirmationStore(str(tracker))


def _impossible_store(tracker: Path):
    module = importlib.import_module("rebar_reconciler.impossible_links")
    return module, module.ImpossibleLinkStore(str(tracker))


def _seed_sidecar(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "records": records}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def test_peer_confirmations_distinct_concurrent_saves_are_merged(tracker: Path) -> None:
    """Two same-baseline writers adding different records both survive."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    final = tracker / ".bridge_state" / "peer_confirmations.json"

    left = module.PeerConfirmationStore(str(tracker))
    right = module.PeerConfirmationStore(str(tracker))
    left.record("A", "B1", "blocks", link_id="1")
    right.record("A", "B2", "blocks", link_id="2")

    errors = _save_in_threads([left, right], final)

    assert errors == []
    records = _records(final)
    assert set(records) == {
        module.record_key("A", "B1", "blocks"),
        module.record_key("A", "B2", "blocks"),
    }
    assert final.with_name(final.name + ".lock").exists(), "the stable lock file is retained"
    assert not list(final.parent.glob("*.tmp")), "atomic publication must not leave temp files"
    fresh = module.PeerConfirmationStore(str(tracker))
    assert fresh.is_confirmed("A", "B1", "blocks")
    assert fresh.is_confirmed("A", "B2", "blocks")


def test_impossible_links_distinct_concurrent_saves_are_merged(tracker: Path) -> None:
    """The impossible-link sidecar uses the same conflict-safe merge seam."""
    module = importlib.import_module("rebar_reconciler.impossible_links")
    final = tracker / ".bridge_state" / "impossible_links.json"

    left = module.ImpossibleLinkStore(str(tracker))
    right = module.ImpossibleLinkStore(str(tracker))
    left._records["left"] = {"reason": module.REASON_CLOSED_SOURCE}
    right._records["right"] = {"reason": module.REASON_CYCLE}
    left._dirty = True
    right._dirty = True

    errors = _save_in_threads([left, right], final)

    assert errors == []
    assert set(_records(final)) == {"left", "right"}
    assert final.with_name(final.name + ".lock").exists()
    assert not list(final.parent.glob("*.tmp"))


def test_same_key_last_lock_holder_wins(tracker: Path) -> None:
    """Same-key conflicts resolve by publication order under the real lock seam."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    final = tracker / ".bridge_state" / "peer_confirmations.json"
    key = module.record_key("A", "B", "blocks")

    first = module.PeerConfirmationStore(str(tracker))
    second = module.PeerConfirmationStore(str(tracker))
    first.record("A", "B", "blocks", link_id="first")
    second.record("A", "B", "blocks", link_id="second")

    first.save()
    second.save()

    assert _records(final)[key]["link_id"] == "second"


def test_delete_is_not_resurrected_by_later_distinct_key_writer(tracker: Path) -> None:
    """A stale writer adding B must not reintroduce baseline key A after A is deleted."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    final = tracker / ".bridge_state" / "peer_confirmations.json"
    old_key = module.record_key("A", "B", "blocks")
    new_key = module.record_key("C", "D", "blocks")
    _seed_sidecar(
        final,
        {
            old_key: {
                "source_id": "A",
                "target_id": "B",
                "relation": "blocks",
                "link_id": "old",
            }
        },
    )

    deleter = module.PeerConfirmationStore(str(tracker))
    later_writer = module.PeerConfirmationStore(str(tracker))
    del deleter._records[old_key]
    deleter._dirty = True
    later_writer.record("C", "D", "blocks", link_id="new")

    deleter.save()
    later_writer.save()

    records = _records(final)
    assert old_key not in records
    assert records[new_key]["link_id"] == "new"


def test_malformed_sidecar_is_preserved_and_logged(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed target fails open; save() does not overwrite prior bytes."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    final = tracker / ".bridge_state" / "peer_confirmations.json"
    before = b"{ this is not json"
    final.write_bytes(before)

    with caplog.at_level(logging.WARNING):
        store = module.PeerConfirmationStore(str(tracker))
        store.record("A", "B", "blocks", link_id="new")
        store.save()

    assert final.read_bytes() == before
    assert str(final) in caplog.text
    assert "could not persist" in caplog.text


def test_lock_failure_preserves_prior_bytes_and_fails_open(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Lock/acquire failure is logged with the sidecar path and swallowed."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    tx = importlib.import_module("rebar_reconciler.sidecar_transactions")
    final = tracker / ".bridge_state" / "peer_confirmations.json"
    _seed_sidecar(final, {})
    before = final.read_bytes()

    @contextmanager
    def failing_lock(*_args, **_kwargs):
        raise OSError("lock unavailable")
        yield

    monkeypatch.setattr(tx, "sibling_exclusive_lock", failing_lock)
    store = module.PeerConfirmationStore(str(tracker))
    store.record("A", "B", "blocks", link_id="new")

    with caplog.at_level(logging.WARNING):
        store.save()

    assert final.read_bytes() == before
    assert str(final) in caplog.text
    assert "could not persist" in caplog.text


def test_atomic_write_failure_preserves_prior_bytes_and_fails_open(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Atomic publication failure leaves the previous complete JSON in place."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    tx = importlib.import_module("rebar_reconciler.sidecar_transactions")
    final = tracker / ".bridge_state" / "peer_confirmations.json"
    _seed_sidecar(final, {})
    before = final.read_bytes()

    def failing_atomic_write(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tx, "atomic_write", failing_atomic_write)
    store = module.PeerConfirmationStore(str(tracker))
    store.record("A", "B", "blocks", link_id="new")

    with caplog.at_level(logging.WARNING):
        store.save()

    assert final.read_bytes() == before
    assert str(final) in caplog.text
    assert "could not persist" in caplog.text


def test_unchanged_save_is_noop(tracker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Converged passes do not rewrite unchanged sidecars."""
    module = importlib.import_module("rebar_reconciler.peer_confirmations")
    tx = importlib.import_module("rebar_reconciler.sidecar_transactions")
    store = module.PeerConfirmationStore(str(tracker))
    store.record("A", "B", "blocks")
    store.save()

    calls = 0

    def counting_persist(*args, **kwargs) -> bool:
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(tx, "persist_record_deltas", counting_persist)

    store.save()

    assert calls == 0


def test_commit_back_pathspec_excludes_retained_lock_files(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained sidecar locks must never be staged by reconciler commit-back."""
    import rebar._store.push
    import rebar.config

    helpers = importlib.import_module("rebar_reconciler.pass_support")
    (tracker / ".bridge_state" / "impossible_links.json").write_text("{}", encoding="utf-8")
    (tracker / ".bridge_state" / "impossible_links.json.lock").write_text("", encoding="utf-8")
    (tracker / ".bridge_state" / "peer_confirmations.json").write_text("{}", encoding="utf-8")
    (tracker / ".bridge_state" / "peer_confirmations.json.lock").write_text("", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def fake_commit_tickets_branch(
        _tracker_dir: Path, *, message: str, paths: list[str], strict: bool
    ) -> None:
        captured["paths"] = paths

    monkeypatch.setattr(rebar.config, "tracker_dir", lambda _repo_root: tracker)
    monkeypatch.setattr(rebar._store.push, "commit_tickets_branch", fake_commit_tickets_branch)

    assert helpers._commit_binding_store_snapshot(None, tracker.parent, "pass-1") is True

    assert captured["paths"] == [
        ".bridge_state/impossible_links.json",
        ".bridge_state/peer_confirmations.json",
    ]
    assert all(not path.endswith(".lock") for path in captured["paths"])
