"""Persistent COMMENT-HLC-key -> Jira-comment-ID map (emersed-specific-mutt).

Comment sync is APPEND-ONLY and must never duplicate a comment on re-sync. The
identity of an already-mirrored comment is NOT its body (same-text comments
collide, an edit looks new) but the pairing of the COMMENT event's HLC
``timestamp`` (the stable ``local_comment_key``) to the Jira comment ID returned
by ``add_comment``. ``BindingStore`` owns that map:

    - ``record_comment_id(local_comment_key, jira_comment_id)`` persists the
      pairing and ``save()``s IMMEDIATELY (write-ahead) so a crash after the Jira
      post cannot re-post (closes the DIG-5301 duplicate-comment class).
    - ``is_comment_mapped`` / ``comment_id_for`` are the read side the outbound
      differ's PRIMARY skip keys on.

These are hermetic unit tests over a tmp tracker dir — no Jira, no network.

Follows the reconciler test-tree loader convention (spec_from_file_location).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BindingStore = _load("_binding_store_for_comment_ids", "binding_store.py").BindingStore

_HLC = "1785800303802040001-48a90ae3-3fbc-4d5b-b71c-a3133f3baa9d"


def _store(tmp_path: Path) -> BindingStore:
    return BindingStore(tmp_path / ".tickets-tracker")


def test_record_comment_id_persists_and_is_mapped(tmp_path):
    """A recorded pairing is queryable and written to bindings.json immediately."""
    store = _store(tmp_path)
    assert store.is_comment_mapped(_HLC) is False
    assert store.comment_id_for(_HLC) is None

    store.record_comment_id(_HLC, "10001")

    assert store.is_comment_mapped(_HLC) is True
    assert store.comment_id_for(_HLC) == "10001"
    # Write-ahead: the mapping is on disk the instant record returns (no deferred
    # save) so a crash before the next explicit save cannot lose it.
    on_disk = json.loads(store._path.read_text(encoding="utf-8"))
    assert on_disk["comment_ids"][_HLC] == "10001"


def test_comment_id_map_survives_across_reconcile_runs(tmp_path):
    """A fresh BindingStore over the same tracker dir re-reads the map."""
    _store(tmp_path).record_comment_id(_HLC, "20002")

    reopened = _store(tmp_path)
    assert reopened.is_comment_mapped(_HLC) is True
    assert reopened.comment_id_for(_HLC) == "20002"


def test_comment_map_no_churn_on_no_new_comment_pass(tmp_path):
    """AC: the comment-ID map is change-gated — a reconcile pass with no NEW comment
    (re-recording an identical pairing) is a no-op, so there is no per-pass save
    churn on the tickets branch."""
    store = _store(tmp_path)
    store.record_comment_id(_HLC, "30003")

    saves = 0
    real_save = store.save

    def _counting_save() -> None:
        nonlocal saves
        saves += 1
        real_save()

    store.save = _counting_save  # type: ignore[method-assign]
    store.record_comment_id(_HLC, "30003")

    assert saves == 0, "an identical re-record must not save() again (append-only, no churn)"
    assert store.comment_id_for(_HLC) == "30003"


def test_record_comment_id_coerces_to_str(tmp_path):
    """Non-string keys/ids (e.g. an int Jira id) are coerced so lookups match."""
    store = _store(tmp_path)
    store.record_comment_id(_HLC, 40004)
    assert store.comment_id_for(_HLC) == "40004"


def test_comment_ids_map_tolerates_legacy_store_without_key(tmp_path):
    """A store written before comment_ids existed still records without KeyError."""
    store = _store(tmp_path)
    store._data.pop("comment_ids", None)  # model a legacy on-disk shape
    store.record_comment_id(_HLC, "50005")
    assert store.is_comment_mapped(_HLC) is True
