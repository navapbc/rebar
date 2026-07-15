"""In-process ``compact`` / ``compact-all``.

Compaction squashes a ticket's event log into ONE SNAPSHOT event under the unified
write lock: re-list events inside the lock, partition out forward-compat
unknown-type events (never absorbed/deleted), re-check the threshold, reduce the
current state, write the SNAPSHOT, delete the originals, invalidate the reducer
cache, and ``git add -A`` + commit atomically.

Reuses ``rebar._store.lock`` (the fcntl+mkdir dual-leg lock),
``rebar.reducer.reduce_ticket`` (in-process), and ``event_append.event_filename``.
SNAPSHOT bytes go through the single canonical serializer
``rebar._store.canonical.canonical_str`` (sorted keys, P1.0).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rebar import config
from rebar._commands import _seam
from rebar._commands._compact_policy import is_foldable
from rebar._engine_support.resolver import resolve_ticket_id
from rebar._store import compat, event_append, fsutil, hlc, lock
from rebar._store.canonical import canonical_str
from rebar._store.gitutil import run_git_write
from rebar.reducer import KNOWN_EVENT_TYPES, reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

logger = logging.getLogger(__name__)

# Process-level count of SNAPSHOT rebuilds (RC2b Option 1) — observability for the
# fsck remediation path (A3). Read via get_rebuild_count().
_REBUILD_COUNT = 0


def get_rebuild_count() -> int:
    """Number of snapshot rebuilds performed by this process (RC2b Option 1)."""
    return _REBUILD_COUNT


def _usage() -> int:
    sys.stderr.write(
        "Usage: ticket-compact.sh <ticket_id> [--threshold=N] [--horizon=NS]\n"
        "  Default threshold: REBAR_COMPACT_THRESHOLD env / compact.threshold config or 10\n"
        "  Default horizon:   REBAR_COMPACTION_HORIZON_NS env / compact.COMPACTION_HORIZON_NS\n"
        "                     config or 1800s in ns (events younger than this stay live)\n"
    )
    return 1


def _git(tracker: str, *args: str):
    return run_git_write(tracker, *args, check=False)


def _sync_before_compact(tracker: str) -> None:
    """Pull the latest tickets before compacting (best-effort, in-process) so a
    remote SNAPSHOT written by another agent is visible and local compaction can
    defer to it. Honors the ``sync.pull`` policy and is fully best-effort (every
    fetch failure is swallowed). Replaces the former dead ``ticket sync`` shell-out
    (no such subcommand existed; ``shell=True`` injection smell)."""
    from rebar._engine_support import reads

    reads.ensure_fresh(tracker)


def _build_authorship_ledger(event_paths: list[str], repo_root) -> list[dict]:
    """Independently scan the folded event files and build the SNAPSHOT authorship ledger
    (epic gnu-whale-ichor / 117b): one ``{event_uuid, content_hash, signature, signer_pubkey,
    position}`` record per folded event that carries an ``author_sig``. This preserves the
    signed events so the merge-gate ``rebar verify-authorship`` can re-verify them AFTER the
    raw event files are retired (folded into the SNAPSHOT).

    Everything the gate needs is captured at compaction time (when the raw file + git history
    are still present): ``content_hash`` (the canonical content binding), the ``signature``
    envelope, the ``signer_pubkey`` that actually verifies it (``identify_signer``; ``null`` for
    a forged/foreign sig — the entry is STILL recorded so the gate can flag it), and the
    ``position`` (the ``{timestamp}-{uuid}`` string plus its resolved introducing
    ``commit_sha``) for the commit-ancestry era check. Unsigned events are omitted (the
    presence-only count lives in ``compiled_state`` already). Best-effort throughout — a
    lookup/decode failure records ``null`` rather than raising."""
    from rebar.attest import authorship, dsse

    ledger: list[dict] = []
    for path in event_paths:
        try:
            with open(path, encoding="utf-8") as f:
                ev = json.load(f)
        except (OSError, ValueError):
            continue
        sig = ev.get("author_sig")
        if not sig:
            continue
        author_id = ev.get("author_id")
        position_str = f"{ev.get('timestamp')}-{ev.get('uuid')}"
        ticket_dir = os.path.dirname(path)

        signer_pubkey = None
        try:
            envelope = dsse.decode(sig if isinstance(sig, str) else "")
            signer_pubkey = authorship.identify_signer(
                envelope, str(author_id), repo_root=repo_root
            )
        except Exception:  # noqa: BLE001 — a decode/lookup failure records a null signer
            signer_pubkey = None

        commit_sha = authorship.resolve_event_commit(position_str, ticket_dir, repo_root=repo_root)

        ledger.append(
            {
                "event_uuid": ev.get("uuid"),
                "content_hash": authorship.authorship_content_hash(ev),
                "signature": sig,
                "signer_pubkey": signer_pubkey,
                "position": {"commit_sha": commit_sha, "position": position_str},
            }
        )
    return ledger


def _snapshot_strip_keys() -> set[str]:
    """Keys to drop from a SNAPSHOT's compiled_state before persisting. ``updated_at`` is
    a derived presentation field re-computed every replay. The legacy ``signature`` mirror
    is ALWAYS dropped (task 7ed9 hardcoded never-emit) — new snapshots carry only the
    kind-keyed ``attestations`` map. The mirror is still re-derived in memory on replay
    (reducer ``process_signature``), so verification keeps working on a compacted ticket."""
    return {"updated_at", "signature"}


def _maybe_pause_at_rename_barrier(n_renamed: int) -> None:
    """Test-only failpoint (inert in production). When the environment variable
    ``REBAR_TEST_COMPACT_RENAME_BARRIER`` names a directory, pause after the FIRST
    source rename (``n_renamed == 1``) so a test can reliably SIGKILL a real
    compactor process in the mid-retirement window — SNAPSHOT already written, one
    source ``*.retired``, the rest still active, nothing committed and the write lock
    still held. The hook writes a ``reached`` marker (its PID) and then blocks until a
    ``release`` file appears, so the test controls the kill point deterministically
    with no timing race. Guarded entirely by the env var: unset ⇒ immediate return."""
    barrier = os.environ.get("REBAR_TEST_COMPACT_RENAME_BARRIER")
    if not barrier or n_renamed != 1:
        return
    bdir = Path(barrier)
    (bdir / "reached").write_text(str(os.getpid()), encoding="utf-8")
    release = bdir / "release"
    while not release.exists():
        time.sleep(0.02)


def _compact_locked(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    threshold: int,
    no_commit: bool,
    horizon: int = 0,
) -> int:
    """The locked compaction critical section. Returns 0 on success (prints
    EVENT_COUNT + the compacted line), 0 on below-threshold-inside-lock (prints the
    skip line), 1 on lock timeout / reducer / state / git failure."""
    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except lock.LockTimeout as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except compat.StoreIncompatibleError as exc:
        # Story 21dd: fail closed (non-zero) on an incompatible store before compaction.
        sys.stderr.write(str(exc) + "\n")
        return exc.returncode
    try:
        # Re-list event files inside the lock (authoritative). Exclude -SYNC.json
        # (bridge metadata that must survive compaction).
        candidates = sorted(
            os.path.join(ticket_dir, f)
            for f in os.listdir(ticket_dir)
            if f.endswith(".json") and not f.startswith(".") and not f.endswith("-SYNC.json")
        )
        # Forward-compat: unknown-type events (written by a newer clone) are
        # preserved untouched — never snapshotted or deleted. Parse each candidate
        # once, capturing its uuid + timestamp for the horizon partition below.
        parsed: list[tuple[str, str, int | None]] = []  # (path, uuid, ts)
        for fp in candidates:
            try:
                with open(fp, encoding="utf-8") as f:
                    ev = json.load(f)
                etype = ev.get("event_type", "")
                euuid = ev.get("uuid", os.path.basename(fp))
                raw_ts = ev.get("timestamp")
                ets = raw_ts if isinstance(raw_ts, int) else None
            except (json.JSONDecodeError, OSError):
                etype, euuid, ets = "", os.path.basename(fp), None
            if etype and etype not in KNOWN_EVENT_TYPES:
                continue
            parsed.append((fp, euuid, ets))
        event_count = len(parsed)

        if event_count <= threshold:
            sys.stdout.write("below threshold (re-checked inside flock) — skipping compaction\n")
            return 0

        # RC2b Option 3 (conservative horizon): only FOLD events older than the
        # horizon. Younger "hot-edge" events stay live ``.json`` and — because the
        # SNAPSHOT is timestamped just after the newest folded event and before the
        # youngest live one — sort AFTER the snapshot and replay on top. So a
        # concurrent sub-horizon append that merges in later is NOT silently dropped by
        # the snapshot's positional skip. horizon<=0 folds everything (the pre-RC2b
        # behavior; the offline test suite defaults to 0).
        now = hlc.physical_now()

        old = [(fp, u, ts) for (fp, u, ts) in parsed if is_foldable(ts, now, horizon)]
        young = [(fp, u, ts) for (fp, u, ts) in parsed if not is_foldable(ts, now, horizon)]

        if not old:
            sys.stdout.write("all events within the compaction horizon — nothing to fold\n")
            return 0

        fold_files = [fp for (fp, _u, _ts) in old]

        # Pick a SNAPSHOT timestamp strictly between the newest folded event and the
        # youngest live one, so folded events sort before it (positionally skipped,
        # their state in compiled_state) and live events sort after it (replayed).
        if young:
            old_ts = [ts for (_fp, _u, ts) in old if ts is not None]
            young_ts = [ts for (_fp, _u, ts) in young if ts is not None]
            max_old = max(old_ts) if old_ts else now
            snapshot_ts = max_old + 1
            if young_ts and snapshot_ts >= min(young_ts):
                # No safe placement gap (adjacent straddling timestamps) — defer folding
                # this pass rather than risk a mis-sorted snapshot.
                sys.stdout.write("no safe horizon gap for a SNAPSHOT timestamp — deferring\n")
                return 0
            compiled_state = reduce_ticket(ticket_dir, event_files_override=fold_files)
        else:
            snapshot_ts = hlc.next_tick(tracker, ticket_id)
            compiled_state = reduce_ticket(ticket_dir)

        if compiled_state is None:
            sys.stderr.write(
                f"Error: reducer failed for ticket {ticket_id} (corrupt or ghost ticket)\n"
            )
            return 1
        # ``updated_at`` is a derived presentation field (P1.1), re-computed on
        # every replay. It must NOT enter the SNAPSHOT's compiled_state, or it
        # would (a) ride into event-log bytes and (b) be restored stale by
        # process_snapshot. Copy-and-drop it so the cache object is untouched.
        # The legacy ``signature`` mirror is dropped here too (task 7ed9 never-emit) —
        # new snapshots carry only ``attestations``.
        _strip = _snapshot_strip_keys()
        compiled_state = {k: v for k, v in compiled_state.items() if k not in _strip}
        # Authorship ledger (epic gnu-whale-ichor / 3183): independently scan the folded
        # events and preserve each SIGNED one so verify-authorship can re-verify it after
        # the raw files are retired. Derive repo_root from the tracker (no repo_root here).
        compiled_state["authorship_ledger"] = _build_authorship_ledger(
            fold_files, os.path.dirname(os.path.realpath(tracker))
        )
        status = compiled_state.get("status", "")
        if status in ("error", "fsck_needed"):
            sys.stderr.write(f"Error: ticket {ticket_id} has status '{status}' — cannot compact\n")
            return 1

        source_uuids = [u for (_fp, u, _ts) in old]

        env_id = _seam.env_id(Path(tracker))
        author = _git_author()

        snapshot_uuid = str(uuid.uuid4())
        snapshot_event = {
            "event_type": "SNAPSHOT",
            "timestamp": snapshot_ts,
            "uuid": snapshot_uuid,
            "env_id": env_id,
            "author": author,
            "data": {
                "compiled_state": compiled_state,
                "source_event_uuids": source_uuids,
                "compacted_at": snapshot_ts,
            },
        }
        # Denormalized author attribution (epic gnu-whale-ichor): stamp author_email /
        # author_id on the SNAPSHOT envelope. No repo_root param here, so derive it from
        # the tracker (mirrors the derivation in rebuild_snapshot_from_full_log).
        snapshot_event.update(_seam.attribution_fields(os.path.dirname(os.path.realpath(tracker))))
        final_path = os.path.join(
            ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
        )
        fsutil.atomic_write(final_path, canonical_str(snapshot_event), encoding="utf-8")

        # I1: RENAME folded sources to ``*.retired`` rather than deleting them. The
        # SNAPSHOT above is written atomically FIRST, so a crash mid-rename leaves a
        # valid SNAPSHOT plus some already-retired sources; the SNAPSHOT-present
        # short-circuit makes a re-compact a no-op, and an existing ``.retired``
        # target is skipped (idempotent). A rename failure is logged (never
        # swallowed) and every completed rename is reversed before we abort, so the
        # fold is atomic: either all sources are retired or none are.
        renamed: list[tuple[str, str]] = []
        try:
            for fp in fold_files:
                retired = fp + RETIRED_SUFFIX
                if os.path.exists(retired):
                    continue  # idempotent re-run: source already retired
                os.rename(fp, retired)
                renamed.append((fp, retired))
                logger.info("compact: retired folded event %s", os.path.basename(fp))
                _maybe_pause_at_rename_barrier(len(renamed))
        except OSError:
            logger.warning(
                "compact: failed to retire a folded event for %s — reversing %d rename(s)",
                ticket_id,
                len(renamed),
                exc_info=True,
            )
            rollback_clean = True
            for orig, retired in reversed(renamed):
                try:
                    os.rename(retired, orig)
                except OSError:
                    rollback_clean = False
                    logger.warning(
                        "compact: could not reverse rename %s -> %s", retired, orig, exc_info=True
                    )
            if rollback_clean:
                # CLEAN rollback: every completed rename was reversed, so the store is
                # back to its exact pre-fold state and the uncommitted SNAPSHOT is a
                # stray artifact — remove it. (Preserves the original behavior.)
                try:
                    os.remove(final_path)
                except OSError:
                    logger.warning("compact: could not remove uncommitted SNAPSHOT %s", final_path)
                sys.stderr.write("Error: failed to retire folded events while holding lock\n")
                return 1
            # INCOMPLETE rollback: at least one reverse-rename failed, so a source is
            # stuck as ``*.retired`` while its folded effect lives ONLY in the SNAPSHOT
            # we wrote. We MUST intentionally RETAIN the SNAPSHOT here — removing it
            # would drop that source's effect from BOTH an active event and the
            # snapshot (silent data loss, the hazard this branch exists to avoid).
            # Retaining it leaves a SNAPSHOT_INCONSISTENT state (a SNAPSHOT plus a
            # reversed-to-active source) that ``fsck --repair-snapshots`` already
            # rebuilds. Reads are already correct in this mixed window: the
            # reversed-to-active source keeps its original (pre-snapshot) filename, so
            # it is positionally skipped during replay and never double-counted. Do NOT
            # "simplify" this back into an unconditional ``os.remove(final_path)``.
            logger.warning(
                "compact: rollback incomplete for %s — SNAPSHOT %s retained; run fsck",
                ticket_id,
                final_path,
                exc_info=True,
            )
            sys.stderr.write(
                "Error: failed to retire folded events while holding lock; rollback "
                "incomplete (a folded source is stranded) — the SNAPSHOT is retained "
                "to avoid data loss. Run `rebar fsck --repair-snapshots` to reconcile.\n"
            )
            return 1
        try:
            os.remove(os.path.join(ticket_dir, ".cache.json"))
        except OSError:
            pass

        if not no_commit:
            add = _git(tracker, "add", "-A", f"{ticket_id}/")
            if add.returncode != 0:
                sys.stderr.write("Error: git operation failed while holding lock\n")
                return 1
            staged = _git(tracker, "diff", "--cached", "--quiet")
            if staged.returncode != 0:
                commit = _git(
                    tracker, "commit", "-q", "--no-verify", "-m", f"ticket: COMPACT {ticket_id}"
                )
                if commit.returncode != 0:
                    sys.stderr.write("Error: git operation failed while holding lock\n")
                    return 1

        sys.stdout.write(f"EVENT_COUNT={event_count}\n")
        sys.stdout.write(f"compacted events into SNAPSHOT for {ticket_id}\n")
        return 0
    finally:
        handle.release()


def _git_author() -> str:
    cp = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    if cp.returncode != 0:
        return "system"
    return cp.stdout.strip()


def _read_event_uuid(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("uuid", os.path.basename(path))
    except (json.JSONDecodeError, OSError):
        return os.path.basename(path)


def rebuild_snapshot_from_full_log(
    tracker: str,
    ticket_id: str,
    ticket_dir: str,
    *,
    no_commit: bool = False,
) -> bool:
    """RC2b Option 1 (rebuild-on-stray): recompute a ticket's SNAPSHOT from the FULL
    ordered event log INCLUDING ``*.retired`` sources, folding a merged-in pre-snapshot
    orphan that a stale snapshot's positional skip had silently dropped.

    Crash-safe via a ``.snapshot-rebuild.bak`` sentinel: it is written before any
    mutation and removed only after a clean round-trip (a fresh reduce reproduces the
    rebuilt state). A ``.bak`` present at entry means a prior rebuild was interrupted —
    we rebuild again (the operation is idempotent). Runs under the write lock
    (single-writer). Returns True if a rebuild was performed.
    """
    global _REBUILD_COUNT
    try:
        handle = lock.acquire(tracker, timeout=30, attempts=2, dual_window=True)
    except lock.LockTimeout as exc:
        logger.warning("fsck: cannot rebuild snapshot for %s: %s", ticket_id, exc)
        return False
    except compat.StoreIncompatibleError as exc:
        # Story 21dd: fail closed on an incompatible store — the snapshot rebuild is a
        # mutation, so skip it (the read-only diagnostic still surfaces the record).
        logger.warning("fsck: cannot rebuild snapshot for %s: %s", ticket_id, exc)
        return False
    try:
        bak_path = os.path.join(ticket_dir, ".snapshot-rebuild.bak")
        if os.path.exists(bak_path):
            logger.warning(
                "fsck: interrupted snapshot rebuild for %s (.bak present) — restarting", ticket_id
            )

        # Full raw-history state (active + retired, snapshots stripped) — INCLUDES the
        # merged-in orphan the stale snapshot's positional skip had dropped.
        compiled_state = reduce_ticket(ticket_dir, include_retired=True)
        if compiled_state is None or compiled_state.get("status") in ("error", "fsck_needed"):
            logger.warning("fsck: snapshot rebuild for %s aborted (reduce failed)", ticket_id)
            return False
        # The legacy ``signature`` mirror is never persisted into a rebuilt snapshot
        # either (task 7ed9 never-emit) — it is re-derived in memory on replay.
        _strip = _snapshot_strip_keys()
        compiled_state = {k: v for k, v in compiled_state.items() if k not in _strip}

        # Every raw (non-snapshot) event becomes a source of the new SNAPSHOT; the live
        # ones are retired, superseded snapshot(s) are retired too.
        live_raw: list[str] = []
        source_uuids: list[str] = []
        old_snaps: list[str] = []
        raw_paths: list[str] = []
        for name in sorted(os.listdir(ticket_dir)):
            if name.startswith(".") or name.endswith("-SYNC.json"):
                continue
            path = os.path.join(ticket_dir, name)
            base = name[: -len(RETIRED_SUFFIX)] if name.endswith(RETIRED_SUFFIX) else name
            if base.endswith("-SNAPSHOT.json"):
                if is_active_event(name):
                    old_snaps.append(path)
                continue
            source_uuids.append(_read_event_uuid(path))
            raw_paths.append(path)
            if is_active_event(name):
                live_raw.append(path)
        # Authorship ledger (epic gnu-whale-ichor / 3183): rebuild it from the FULL raw log
        # (active + retired) so a rebuilt SNAPSHOT preserves the signed-event ledger too.
        compiled_state["authorship_ledger"] = _build_authorship_ledger(
            raw_paths, os.path.dirname(os.path.realpath(tracker))
        )

        env_id = _seam.env_id(Path(tracker))
        author = _git_author()
        snapshot_uuid = str(uuid.uuid4())
        snapshot_ts = hlc.next_tick(tracker, ticket_id)
        snapshot_event = {
            "event_type": "SNAPSHOT",
            "timestamp": snapshot_ts,
            "uuid": snapshot_uuid,
            "env_id": env_id,
            "author": author,
            "data": {
                "compiled_state": compiled_state,
                "source_event_uuids": source_uuids,
                "compacted_at": snapshot_ts,
            },
        }
        # Denormalized author attribution (epic gnu-whale-ichor) — derive repo_root from
        # the tracker (no repo_root param on this fsck-repair path).
        snapshot_event.update(_seam.attribution_fields(os.path.dirname(os.path.realpath(tracker))))

        # Sentinel/back-up the pre-rebuild snapshot BEFORE mutating.
        try:
            backup = ""
            if old_snaps:
                with open(old_snaps[-1], encoding="utf-8") as f:
                    backup = f.read()
            fsutil.atomic_write(bak_path, backup, encoding="utf-8")
        except OSError:
            logger.warning("fsck: could not write rebuild sentinel for %s", ticket_id)
            return False

        final_path = os.path.join(
            ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
        )
        fsutil.atomic_write(final_path, canonical_str(snapshot_event), encoding="utf-8")

        for fp in live_raw + old_snaps:
            retired = fp + RETIRED_SUFFIX
            if os.path.exists(retired):
                continue
            try:
                os.rename(fp, retired)
            except OSError:
                logger.warning("fsck: could not retire %s during rebuild", fp, exc_info=True)

        try:
            os.remove(os.path.join(ticket_dir, ".cache.json"))
        except OSError:
            pass

        # Clean round-trip: a fresh reduce must reproduce the rebuilt status before we
        # drop the sentinel (else leave it so the next fsck retries).
        check = reduce_ticket(ticket_dir)
        if check is None or check.get("status") != compiled_state.get("status"):
            logger.warning(
                "fsck: snapshot rebuild round-trip mismatch for %s — leaving .bak for retry",
                ticket_id,
            )
            return False
        try:
            os.remove(bak_path)
        except OSError:
            pass

        _REBUILD_COUNT += 1
        logger.warning(
            "fsck: rebuilt SNAPSHOT for %s from full log (%d sources) — folded a merged-in "
            "pre-snapshot orphan",
            ticket_id,
            len(source_uuids),
        )

        if not no_commit:
            add = _git(tracker, "add", "-A", f"{ticket_id}/")
            if add.returncode == 0:
                staged = _git(tracker, "diff", "--cached", "--quiet")
                if staged.returncode != 0:
                    _git(
                        tracker,
                        "commit",
                        "-q",
                        "--no-verify",
                        "-m",
                        f"ticket: REBUILD SNAPSHOT {ticket_id}",
                    )
        return True
    finally:
        handle.release()


def compact_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact <id>`` entry."""
    if len(argv) < 1:
        return _usage()
    tracker = str(config.tracker_dir(repo_root))
    raw = argv[0]
    ticket_id = resolve_ticket_id(raw, tracker)
    if ticket_id is None:
        sys.stderr.write(f"Error: ticket '{raw}' not found\n")
        return 1

    # Default threshold from the typed config (compact.threshold; env
    # REBAR_COMPACT_THRESHOLD, deprecated alias COMPACT_THRESHOLD, or a config file).
    # A --threshold= flag below still overrides. A malformed config is reported as a
    # clean error (exit 1), not an uncaught traceback.
    try:
        _cfg = config.load_config(repo_root).compact
        threshold = _cfg.threshold
        horizon = _cfg.COMPACTION_HORIZON_NS
    except config.ConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    skip_sync = False
    no_commit = False
    for a in argv[1:]:
        if a.startswith("--threshold="):
            threshold = int(a[len("--threshold=") :])
        elif a.startswith("--horizon="):
            horizon = int(a[len("--horizon=") :])
        elif a == "--skip-sync":
            skip_sync = True
        elif a == "--no-commit":
            no_commit = True
        else:
            sys.stderr.write(f"Error: unknown argument '{a}'\n")
            return _usage()

    if not (
        os.path.isdir(tracker)
        and (
            os.path.isfile(os.path.join(tracker, ".git"))
            or os.path.isdir(os.path.join(tracker, ".git"))
        )
    ):
        sys.stderr.write("Error: ticket system not initialized. Run 'ticket init' first.\n")
        return 1
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        sys.stderr.write(f"Error: ticket directory not found: {ticket_dir}\n")
        return 1

    if not skip_sync:
        _sync_before_compact(tracker)
        if any(
            f.endswith("-SNAPSHOT.json") and not f.startswith(".") for f in os.listdir(ticket_dir)
        ):
            sys.stdout.write(f"skipping compaction for {ticket_id} — remote SNAPSHOT exists\n")
            return 0

    preflock = sum(
        1 for f in os.listdir(ticket_dir) if f.endswith(".json") and not f.startswith(".")
    )
    if preflock <= threshold:
        sys.stdout.write(f"below threshold ({preflock} <= {threshold}) — skipping compaction\n")
        return 0

    rc = _compact_locked(tracker, ticket_id, ticket_dir, threshold, no_commit, horizon)
    # A successful compaction commits a SNAPSHOT inline (not via write_and_push), so
    # push it best-effort — unless --no-commit (nothing committed) or --skip-sync
    # (the caller owns sync: compact-on-close passes it and the transition pushes;
    # compact-all batches one commit + push itself). Bug prone-octet-cheek.
    if rc == 0 and not no_commit and not skip_sync:
        from rebar._store import push

        push.push_after_commit(tracker)
    return rc


# ── compact-all ──────────────────────────────────────────────────────────────
def _scan_snapshot_state(tracker: str) -> tuple[list[str], int]:
    """Return (ticket ids lacking a SNAPSHOT, count already having one), scanning
    ticket dirs (those with at least one event JSON), sorted by name."""
    needs: list[str] = []
    already = 0
    try:
        entries = sorted(os.scandir(tracker), key=lambda e: e.name)
    except OSError:
        return [], 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        names = os.listdir(entry.path)
        if not any(n.endswith(".json") for n in names):
            continue
        if any(n.endswith("-SNAPSHOT.json") for n in names):
            already += 1
        else:
            needs.append(entry.name)
    return needs, already


def compact_all_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar compact-all`` entry — backfill SNAPSHOTs for tickets lacking one."""
    import contextlib
    import io

    dry_run = False
    limit = 0
    no_commit = False
    for a in argv:
        if a == "--dry-run":
            dry_run = True
        elif a.startswith("--limit="):
            limit = int(a[len("--limit=") :])
        elif a == "--no-commit":
            no_commit = True
        elif a in ("--help", "-h"):
            sys.stdout.write("Usage: ticket compact-all [--dry-run] [--limit=N] [--no-commit]\n")
            return 0
        else:
            sys.stderr.write(f"Error: unknown option '{a}'\n")
            return 1

    tracker = str(config.tracker_dir(repo_root))
    if not os.path.isdir(tracker):
        sys.stderr.write(f"Error: tracker dir not found: {tracker}\n")
        return 1

    needs, already = _scan_snapshot_state(tracker)
    total_needs = len(needs)
    sys.stdout.write(f"Tickets already with SNAPSHOT : {already}\n")
    sys.stdout.write(f"Tickets needing compaction     : {total_needs}\n")
    if total_needs == 0:
        sys.stdout.write("Nothing to do.\n")
        return 0

    if dry_run:
        sys.stdout.write("\nDry-run — would compact:\n")
        for tid in needs:
            sys.stdout.write(f"  {tid}\n")
        return 0

    if limit > 0 and total_needs > limit:
        sys.stdout.write(f"Applying --limit={limit} (will stop after {limit} tickets).\n")
        needs = needs[:limit]
        total_needs = limit

    compacted = 0
    error_ids: list[str] = []
    sys.stdout.write(f"\nCompacting {total_needs} tickets...\n")
    sys.stdout.write("(each dot = 1 ticket; E = error)\n")
    for tid in needs:
        with contextlib.redirect_stderr(io.StringIO()):  # bash 2>/dev/null
            rc = compact_cli(
                [tid, "--threshold=0", "--skip-sync", "--no-commit"], repo_root=repo_root
            )
        if rc == 0:
            compacted += 1
            sys.stdout.write(".")
        else:
            error_ids.append(tid)
            sys.stdout.write("E")
        sys.stdout.flush()

    sys.stdout.write("\n\n")
    sys.stdout.write(
        f"Done: {compacted} compacted, {len(error_ids)} errors (of {total_needs} attempted)\n"
    )
    if error_ids:
        sys.stderr.write("Errored tickets:\n")
        for tid in error_ids:
            sys.stderr.write(f"  {tid}\n")

    if compacted > 0 and not no_commit:
        sys.stdout.write(f"Staging and committing {compacted} new SNAPSHOT files...\n")
        _git(tracker, "add", "-A")
        if _git(tracker, "diff", "--cached", "--quiet").returncode == 0:
            sys.stdout.write("No staged changes (SNAPSHOTs may already have been committed).\n")
        else:
            _git(
                tracker,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"chore: backfill SNAPSHOT files for {compacted} tickets (ticket-compact-all)",
            )
            sys.stdout.write("Committed.\n")
            # One best-effort push for the whole batch (per-ticket calls used
            # --skip-sync to defer it here) — bug prone-octet-cheek.
            from rebar._store import push

            push.push_after_commit(tracker)

    return 2 if error_ids else 0
