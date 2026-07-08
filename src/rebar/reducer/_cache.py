"""Ticket reducer file-level cache: read and write .cache.json."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

# Invariant I1 (docs/concurrency.md): compaction RENAMES the event files it folds
# to ``<name>.retired`` instead of hard-deleting them. A hard delete can be
# resurrected by a delete/add reconciliation (the RC1 rebase class) and then trips
# SNAPSHOT_INCONSISTENT; an append-only rename never loses the source bytes and is
# invisible to replay/fsck. This is the SINGLE source of truth for that suffix —
# compaction (the producer), the reducer (both listing paths), and fsck all import
# it so there is exactly one string literal to reason about.
RETIRED_SUFFIX = ".retired"


def is_active_event(name: str) -> bool:
    """True for a live event file, False for a folded ``*.retired`` source.

    A retired file is an append-only tombstone of an event that compaction has
    already folded into a SNAPSHOT; it must be excluded from every replay, dir
    hash, and fsck scan so it neither re-enters state nor reads as an
    inconsistency."""
    return not name.endswith(RETIRED_SUFFIX)


# Reducer-logic cache version. The dir-hash captures EVENT-FILE changes, but not
# changes to how the reducer PROJECTS those events into state. When the reducer's
# projection semantics change, every previously-cached .cache.json would
# otherwise serve a state compiled by the old logic. Folding this version into
# the dir hash invalidates all caches on a bump. BUMP THIS whenever a processor's
# projection changes.
#   v2: process_revert now un-archives on REVERT-of-ARCHIVED (bug vocal-jig-apron)
#   v3: replay now projects a derived `updated_at` (P1.1); pre-v3 caches lack it
#   v4: replay now projects the kind-keyed `attestations` map (epic
#       dark-acme-lumen); pre-v4 caches lack it, hiding a signed plan-review
#       attestation and wrongly blocking `claim` (bug wait-warp-inlay)
_REDUCER_CACHE_VERSION = 4


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
        logger.warning("failed to write cache for %s", ticket_dir, exc_info=True)


def compute_dir_hash(ticket_dir: str, event_filenames: list[str]) -> str:
    """Compute a content hash based on event filenames, sizes, and mtimes.

    Uses filename + file size + modification time (a single fast stat per file)
    to detect additions/deletions and in-place content overwrites — including a
    same-byte-length rewrite (e.g. from a git checkout/rebase of the tickets
    branch or an fsck-recover cherry-pick), which a filename+size key alone
    cannot see. Folding in st_mtime_ns closes that stale-read gap.
    """
    hash_parts: list[str] = [f"rv:{_REDUCER_CACHE_VERSION}"]
    for name in event_filenames:
        path = os.path.join(ticket_dir, name)
        try:
            st = os.stat(path)
            size, mtime_ns = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime_ns = -1, -1
        hash_parts.append(f"{name}:{size}:{mtime_ns}")
    hash_parts.append(
        "marker:present"
        if os.path.exists(os.path.join(ticket_dir, ".archived"))
        else "marker:absent"
    )
    return hashlib.sha256("|".join(hash_parts).encode()).hexdigest()


def prepare_event_files(
    ticket_dir: str,
) -> tuple[str, str, list[str], dict | None]:
    """Build sorted event file list and compute dir_hash; check cache.

    Returns (cache_path, dir_hash, event_files, cached_state_json_or_none).
    cached_state_json_or_none is the raw cached state dict if a cache hit
    occurred, or None if a cache miss (caller must recompute).

    ``event_files`` is the sorted list of *.json event file paths (glob-expanded,
    excluding .cache.json via glob's dotfile exclusion).  Empty list means no
    events in the directory.
    """
    from ._sort import event_sort_key

    cache_path = os.path.join(ticket_dir, ".cache.json")

    try:
        all_files = os.listdir(ticket_dir)
    except OSError:
        all_files = []
    # Both listing paths exclude folded ``*.retired`` sources via is_active_event
    # (I1): they are append-only tombstones, not live events, so they must never
    # enter the replay set nor the dir hash that keys the reducer cache.
    event_filenames = sorted(
        f for f in all_files if f.endswith(".json") and f != ".cache.json" and is_active_event(f)
    )
    dir_hash = compute_dir_hash(ticket_dir, event_filenames)

    cached = read_cache(cache_path, dir_hash)

    event_files = sorted(
        (p for p in glob.glob(os.path.join(ticket_dir, "*.json")) if is_active_event(p)),
        key=event_sort_key,
    )

    return cache_path, dir_hash, event_files, cached
