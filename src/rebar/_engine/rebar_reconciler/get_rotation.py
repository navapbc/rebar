"""Fail-open persistence helpers for bounded direct-GET rotation state.

The sidecar is deliberately separate from ``bindings.json`` so advancing a
small GET-rotation cursor does not rewrite binding entries.  During rollout,
callers continue to dual-write the legacy inline stamp for old readers.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_VERSION = 1


def load(path: Path) -> dict[str, str]:
    """Load sidecar stamps, treating absent or malformed state as empty.

    Rotation is an optimization: losing its history costs a bounded extra GET,
    whereas failing a reconciliation pass because of its sidecar would prevent
    ordinary binding progress.  A subsequent ``BindingStore.save()`` repairs a
    malformed file from the in-memory state.
    """
    try:
        with path.open(encoding="utf-8") as file:
            payload: Any = json.load(file)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    stamps = payload.get("last_get_pass")
    if not isinstance(stamps, dict):
        return {}
    valid_stamps: dict[str, str] = {}
    for key, stamp in stamps.items():
        if isinstance(key, str) and isinstance(stamp, str):
            valid_stamps[key] = stamp
    return valid_stamps


def merge_legacy(stamps: dict[str, str], bindings: dict[str, Any]) -> None:
    """Merge inline legacy stamps into sidecar state using chronological max."""
    for entry in bindings.values():
        if not isinstance(entry, dict):
            continue
        jira_key = entry.get("jira_key")
        legacy = entry.get("last_get_pass")
        if isinstance(jira_key, str) and isinstance(legacy, str):
            stamps[jira_key] = max(stamps.get(jira_key, ""), legacy)


def latest(stamps: dict[str, str], jira_key: str, legacy: Any) -> str:
    """Return the later of a sidecar stamp and a legacy inline value.

    Pass identifiers are canonical UTC strings, so lexical ordering is their
    chronological ordering.  Invalid old values are treated as the empty
    never-GET sentinel rather than allowed to poison the rotation sort.
    """
    legacy_value = legacy if isinstance(legacy, str) else ""
    return max(stamps.get(jira_key, ""), legacy_value)


def last_get_pass(binding_store: Any, jira_key: str) -> str:
    """Read a store's rotation stamp with the legacy-stub fallback."""
    fn = getattr(binding_store, "last_get_pass", None)
    if fn is None:
        return ""
    try:
        return fn(jira_key) or ""
    except Exception:  # noqa: BLE001 — rotation must not fail the sync pass
        return ""


def set_last_get(stamps: dict[str, str], jira_key: str, pass_id: str) -> None:
    """Record a newly observed GET stamp in sidecar state."""
    stamps[jira_key] = pass_id


def save(path: Path, stamps: dict[str, str]) -> bool:
    """Atomically write the sidecar, returning false when persistence fails open.

    The caller owns when this is invoked: it must be called only from the
    binding store's established save boundary, never while merely inspecting
    rotation state.  This preserves ``reconcile-check``'s read-only contract.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix="get_rotation_", suffix=".tmp"
        )
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                {"version": _VERSION, "last_get_pass": stamps},
                file,
                indent=2,
                sort_keys=True,
            )
            file.write("\n")
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
