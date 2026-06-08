"""Ticket reducer file-level cache: read and write .cache.json."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys


def read_cache(cache_path: str, dir_hash: str) -> dict | None:
    """Return the cached state if dir_hash matches, else None (cache miss)."""
    try:
        with open(cache_path, encoding="utf-8") as cf:
            cached = json.load(cf)
        if isinstance(cached, dict) and cached.get("dir_hash") == dir_hash:
            return cached["state"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def write_cache(cache_path: str, dir_hash: str, state: dict, ticket_dir: str) -> None:
    """Atomically write the state to .cache.json using a tmp-then-rename pattern."""
    try:
        cache_tmp = cache_path + ".tmp"
        with open(cache_tmp, "w", encoding="utf-8") as tf:
            json.dump({"dir_hash": dir_hash, "state": state}, tf, ensure_ascii=False)
        os.rename(cache_tmp, cache_path)
    except OSError:
        print(
            f"WARNING: failed to write cache for {ticket_dir}",
            file=sys.stderr,
        )


def compute_dir_hash(ticket_dir: str, event_filenames: list[str]) -> str:
    """Compute a content hash based on event filenames and their file sizes.

    Uses filename + file size (fast stat) to detect both additions/deletions and
    in-place content overwrites (same filename, different size).
    """
    hash_parts: list[str] = []
    for name in event_filenames:
        path = os.path.join(ticket_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        hash_parts.append(f"{name}:{size}")
    hash_parts.append(
        "marker:present"
        if os.path.exists(os.path.join(ticket_dir, ".archived"))
        else "marker:absent"
    )
    return hashlib.sha256("|".join(hash_parts).encode()).hexdigest()


def prepare_event_files(
    ticket_dir: str,
) -> tuple[str, str, list[str], str | None]:
    """Build sorted event file list and compute dir_hash; check cache.

    Returns (cache_path, dir_hash, event_files, cached_state_json_or_none).
    cached_state_json_or_none is the raw cached state dict if a cache hit
    occurred, or None if a cache miss (caller must recompute).

    ``event_files`` is the sorted list of *.json event file paths (glob-expanded,
    excluding .cache.json via glob's dotfile exclusion).  Empty list means no
    events in the directory.
    """
    from ticket_reducer._sort import event_sort_key

    cache_path = os.path.join(ticket_dir, ".cache.json")

    try:
        all_files = os.listdir(ticket_dir)
    except OSError:
        all_files = []
    event_filenames = sorted(
        f for f in all_files if f.endswith(".json") and f != ".cache.json"
    )
    dir_hash = compute_dir_hash(ticket_dir, event_filenames)

    cached = read_cache(cache_path, dir_hash)

    event_files = sorted(
        glob.glob(os.path.join(ticket_dir, "*.json")), key=event_sort_key
    )

    return cache_path, dir_hash, event_files, cached
