"""Conflict-safe JSON sidecar read-modify-write transactions.

The reconciler keeps several peer-derived sidecars outside the append-only
ticket event stream. They are still shared store state: two reconcile passes can
start from the same sidecar snapshot, each change a different key, and then
publish in either order. A complete-document rewrite loses one of those changes.

This module is the narrow merge seam for those sidecars. It serializes each RMW
with the shared sibling lock from :mod:`rebar._store.fsutil`, reloads the current
valid JSON while holding that lock, applies key-level deltas, and publishes with
``atomic_write(..., fsync=True)``. Failures are fail-open: the pass continues and
the previous bytes remain the observable sidecar state.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from rebar._store.fsutil import atomic_write, sibling_exclusive_lock

JsonObject = dict[str, Any]

logger = logging.getLogger(__name__)


def _clone_mapping(mapping: Mapping[str, Any]) -> JsonObject:
    return {str(key): copy.deepcopy(value) for key, value in mapping.items()}


def _read_json_object(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON document is {type(data).__name__}, not object")
    return data


def mutate_json_object(
    path: str | os.PathLike[str],
    mutator: Callable[[MutableMapping[str, Any]], None],
    *,
    log_label: str,
    sort_keys: bool = True,
) -> bool:
    """Mutate a JSON object under the sidecar lock and durable atomic publish.

    Missing files are treated as empty objects. Malformed existing files,
    lock/acquire failures, mutator failures, and atomic-write failures are
    logged with the sidecar path and return ``False`` without raising.
    """
    sidecar_path = Path(path)
    try:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        with sibling_exclusive_lock(sidecar_path):
            current = _read_json_object(sidecar_path)
            before = copy.deepcopy(current)
            mutator(current)
            if current == before:
                return True
            payload = json.dumps(current, indent=2, sort_keys=sort_keys)
            atomic_write(sidecar_path, payload, encoding="utf-8", fsync=True)
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open sidecar persistence
        logger.warning("%s: could not persist %s (%r)", log_label, sidecar_path, exc)
        return False


def record_deltas(
    baseline_records: Mapping[str, Any],
    desired_records: Mapping[str, Any],
) -> tuple[JsonObject, set[str]]:
    """Return ``(upserts, deletes)`` needed to move baseline to desired state."""
    baseline = _clone_mapping(baseline_records)
    desired = _clone_mapping(desired_records)
    upserts = {
        key: value
        for key, value in desired.items()
        if key not in baseline or baseline[key] != value
    }
    deletes = set(baseline) - set(desired)
    return upserts, deletes


def persist_record_deltas(
    path: str | os.PathLike[str],
    *,
    version: int,
    baseline_records: Mapping[str, Any],
    desired_records: Mapping[str, Any],
    log_label: str,
) -> bool:
    """Merge a store's record-key deltas into the latest on-disk sidecar.

    ``baseline_records`` must be the immutable load-time snapshot for the store
    instance. Distinct-key changes from later writers are retained because the
    current file is reloaded inside the lock before applying only this instance's
    upserts/deletes. If two writers touch the same key, the one that acquires the
    lock last applies its delta last and therefore wins.
    """
    upserts, deletes = record_deltas(baseline_records, desired_records)
    if not upserts and not deletes:
        return True

    def apply_deltas(payload: MutableMapping[str, Any]) -> None:
        existing_records = payload.get("records")
        if existing_records is None:
            records: JsonObject = {}
        elif isinstance(existing_records, dict):
            records = _clone_mapping(existing_records)
        else:
            raise ValueError("records field is not an object")
        for key in deletes:
            records.pop(key, None)
        for key, value in upserts.items():
            records[key] = copy.deepcopy(value)
        payload["version"] = version
        payload["records"] = records

    return mutate_json_object(path, apply_deltas, log_label=log_label)
