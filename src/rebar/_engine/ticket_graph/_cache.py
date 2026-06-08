"""Graph cache for ticket-graph."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

_GRAPH_CACHE_FILE = ".graph-cache.json"


def _compute_cache_key(tracker_dir: str) -> str:
    """Compute a cache key from the sha256 of all ticket dirs' content hashes.

    Uses the same dir_hash method as the reducer: filename + file size.
    """
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError:
        return ""

    all_hashes: list[str] = []
    for entry in entries:
        entry_path = os.path.join(tracker_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        try:
            dir_entries = sorted(os.listdir(entry_path))
        except OSError:
            dir_entries = []

        hash_parts: list[str] = []
        for name in dir_entries:
            if not name.endswith(".json") or name == ".cache.json":
                continue
            filepath = os.path.join(entry_path, name)
            try:
                size = os.path.getsize(filepath)
            except OSError:
                size = -1
            hash_parts.append(f"{name}:{size}")
        dir_hash = hashlib.sha256("|".join(hash_parts).encode()).hexdigest()
        all_hashes.append(f"{entry}:{dir_hash}")

    return hashlib.sha256("|".join(all_hashes).encode()).hexdigest()


def _read_graph_cache(tracker_dir: str, cache_key: str) -> dict[str, Any] | None:
    """Return cached graph data if the cache key matches, else None."""
    cache_path = os.path.join(tracker_dir, _GRAPH_CACHE_FILE)
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, dict) and cached.get("cache_key") == cache_key:
            return cached.get("graphs", {})
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def _write_graph_cache(
    tracker_dir: str, cache_key: str, graphs: dict[str, Any]
) -> None:
    """Atomically write the graph cache."""
    cache_path = os.path.join(tracker_dir, _GRAPH_CACHE_FILE)
    cache_tmp = cache_path + ".tmp"
    try:
        with open(cache_tmp, "w", encoding="utf-8") as f:
            json.dump({"cache_key": cache_key, "graphs": graphs}, f, ensure_ascii=False)
        os.rename(cache_tmp, cache_path)
    except OSError:
        pass  # Cache write failure is non-fatal
