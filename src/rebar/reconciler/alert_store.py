from __future__ import annotations

import json
import time
from pathlib import Path

from rebar._store.fsutil import open_published, sibling_exclusive_lock
from rebar.reconciler.scratch_retention import alerts_dir

#: The stable sibling every appender serializes on. A dotfile, so retention
#: (``scratch_retention``) and the dedupe scan, which read ``*.jsonl``, ignore it.
#: It replaces an in-file fcntl flock that Windows lacks (appends there overwrote each other).
# mechanism-ok: lock .append.lock — 3bc8-49a4-6c84-4b19 keep-open admission
_APPEND_LOCK_NAME = ".append.lock"

_24H_NS = 24 * 3600 * 1_000_000_000


def _store_dir(repo_root: Path) -> Path:
    # The layout (``<repo_root>/.rebar/bridge/bridge_alerts``) is owned by
    # scratch_retention; consuming projects move it only by passing a different repo_root.
    return alerts_dir(repo_root)


def _today_file(repo_root: Path) -> Path:
    from datetime import datetime, timezone

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _store_dir(repo_root) / f"{date_str}.jsonl"


def append(record: dict, repo_root: Path) -> None:
    """Append a record to today's JSONL alert log.

    Concurrent writers from multiple threads or reconciler instances serialize on
    an exclusive kernel lock on a sibling lock file (``flock`` on POSIX,
    ``msvcrt.locking`` on Windows), so records can neither interleave nor, on
    Windows, where append mode seeks then writes, overwrite each other.
    """
    store_dir = _store_dir(repo_root)
    store_dir.mkdir(parents=True, exist_ok=True)
    today = _today_file(repo_root)
    # Bug 8384: stamp timestamp_ns centrally so is_deduped's window comparison has a
    # basis for EVERY caller. A caller-supplied value wins (spread after the default),
    # so the callers that already stamp are unchanged; the ones that never did (e.g.
    # BindingRepository.alert, some fetcher paths) now dedupe correctly.
    record = {"timestamp_ns": time.time_ns(), **record}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with sibling_exclusive_lock(today, lock_name=_APPEND_LOCK_NAME):
        with today.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()


def is_deduped(key: str, repo_root: Path, window_ns: int = _24H_NS) -> bool:
    """Return True if an unresolved alert with this key was written within the window."""
    store_dir = _store_dir(repo_root)
    if not store_dir.is_dir():
        return False
    now = time.time_ns()
    for jf in sorted(store_dir.glob("*.jsonl")):
        try:
            with open_published(jf) as fh:
                lines = fh.read().splitlines()
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001 — skip an unparseable alert-store line (per-line fail-open during the dedupe scan)
                    continue
                if rec.get("key") == key and not rec.get("resolved"):
                    ts = rec.get("timestamp_ns", 0)
                    if now - ts <= window_ns:
                        return True
        except Exception:  # noqa: BLE001 — skip an unreadable alert file (per-file fail-open during the dedupe scan)
            continue
    return False


def _atomic_write(path: Path, content: str) -> None:
    """Replace `path` atomically with `content` via the shared ``fsutil.atomic_write`` seam.

    ``fsutil.atomic_write`` writes a temp file in the same directory + ``os.replace`` (with
    ``fsync=True``: file + dir fsync), so a crash mid-write cannot leave the destination
    truncated or partially written — the prior ``write_text`` approach truncated first and
    could lose the whole file on SIGKILL/OOM.

    The prior hand-rolled ``mkstemp``+``os.replace`` produced a 0o600 file (mkstemp's default,
    never chmod'd) and created the parent dir first; ``fsutil.atomic_write`` requires the
    parent to already exist and defaults to the umask mode, so BOTH are preserved explicitly
    here: the ``mkdir`` below and ``permissions=0o600`` keep the on-disk bytes AND mode
    byte-identical to before. These alert files live in the git-ignored, local-scratch
    ``.rebar/bridge/bridge_alerts`` tree (NOT the committed tickets branch), so no commit /
    publish path is involved — the atomic write alone is the whole contract.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    from rebar._store import fsutil

    fsutil.atomic_write(path, content, permissions=0o600, fsync=True)


def patch_bug_filed(key: str, bug_ticket_id: str, repo_root: Path) -> None:
    """Patch the latest unresolved record for key with bug_ticket_id.

    The rewrite is atomic (tempfile + os.replace + fsync) so a crash mid-write
    cannot lose the day's alert history. Non-dict JSONL payloads (e.g. a bare
    number from a corrupt writer) are preserved verbatim rather than crashing
    the patch attempt — the inner guard checks isinstance(rec, dict) before
    accessing rec.get().

    Lines that are NOT the patch target are preserved BYTE-IDENTICAL to their
    original text — we never re-serialize via json.dumps for non-target lines.
    This guarantees that key order, whitespace, ensure_ascii encoding, and any
    other writer-specific formatting stay stable across the patch operation,
    so a crash mid-write cannot leave the file in a "some lines re-serialized,
    some original" mixed-formatting state.
    """
    store_dir = _store_dir(repo_root)
    if not store_dir.is_dir():
        return
    for jf in sorted(store_dir.glob("*.jsonl"), reverse=True):
        try:
            # Hold the append lock across the read and the rewrite: an alert appended in
            # between would otherwise be dropped by the replace (bug equable-overseas-hoiho).
            with sibling_exclusive_lock(jf, lock_name=_APPEND_LOCK_NAME):
                if _patch_file(jf, key, bug_ticket_id):
                    return
        except Exception:  # noqa: BLE001 — skip an unwritable/unparseable alert file (per-file fail-open during the patch scan)
            continue


def _patch_file(jf: Path, key: str, bug_ticket_id: str) -> bool:
    """Patch the first unresolved record for *key* in *jf*; return whether one was patched."""
    out_lines: list[str] = []
    patched = False
    for line in jf.read_text(encoding="utf-8").splitlines():
        rec = None
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            pass  # malformed JSONL line — skip and preserve original text
        if (
            not patched
            and isinstance(rec, dict)
            and rec.get("key") == key
            and not rec.get("resolved")
        ):
            rec["bug_ticket_id"] = bug_ticket_id
            rec["op"] = "bug_filed"
            patched = True
            # Only the patched line is re-serialized; all other lines
            # are written back byte-identical to their original input.
            out_lines.append(json.dumps(rec))
        else:
            out_lines.append(line)
    if patched:
        _atomic_write(jf, "\n".join(out_lines) + "\n")
    return patched
