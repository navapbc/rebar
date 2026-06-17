"""The status-transition + claim locked critical section, in-process (Tier E E5c).

This module IS the lock-holding, committing core for a status transition and for
an atomic claim. It was relocated from ``_engine/ticket_txn.py`` (the bash-era
heredoc extraction) into the ``rebar`` package so the CLI/library can call it
in-process — without putting the engine dir on ``sys.path`` (the ``test_engine_dir``
guard, a Tier D invariant). The old ``_engine/ticket_txn.py`` is now a thin shim
that re-exits 10/1/2 for the bash dispatcher leg until E7.

Each core runs as ONE critical section in ONE process: acquire the unified write
lock (``rebar._store.lock`` — fcntl + mkdir dual leg, the ``stiff-mop-lane`` fix),
re-read + verify the current status (exit 10 / :class:`ConcurrencyMismatch` on
optimistic-concurrency mismatch), apply the close-time guards, write the
append-only event file(s), and ``git add``+``commit`` — releasing the lock only
after the commit. Do NOT split the commit out: it would reopen a lost-update
window (REMEDIATION_PROPOSAL §0 I4/I5, docs/concurrency.md).

**Byte-parity contract.** Event files are serialised through the single canonical
helper ``rebar._store.canonical.canonical_str`` (sorted keys, compact separators,
``ensure_ascii=False``) — byte-identical to every other live writer (epic P1.0).
This still does NOT use ``rebar._store.event_append.stage_and_commit``/
``write_and_push`` (which re-acquire the lock per event); it shares only the
serializer and ``event_filename``, keeping the inline rename+commit window here.

Failure signalling: these cores **raise** rather than ``sys.exit``. exit-10
optimistic-concurrency mismatch → :class:`ConcurrencyMismatch`; everything else →
:class:`CommandError` carrying the exact stderr text + exit code (1 generic /
2 git). The caller (CLI/library/shim) emits ``message`` to stderr and maps the
exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

from rebar._commands._seam import CommandError
from rebar._store import event_append, hlc, lock
from rebar._store.canonical import canonical_str
from rebar.reducer import reduce_ticket
from rebar.reducer._sort import prefix_ts as _prefix_ts


class ConcurrencyMismatch(CommandError):
    """Optimistic-concurrency rejection (exit 10): the ticket's actual status no
    longer matches the caller's expectation, or a claim target is not ``open``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, returncode=10)


def _acquire_write_lock(tracker_dir: str) -> lock.LockHandle:
    """Acquire the unified write lock (fcntl + mkdir dual leg, 30s) for a txn
    critical section — mutually exclusive with bash leaf-writes on every platform
    class (the ``stiff-mop-lane`` fix). ``attempts=1`` preserves ticket_txn's 30s
    budget. Held across the whole re-read → write → commit section."""
    try:
        return lock.acquire(tracker_dir, timeout=30, attempts=1, dual_window=True)
    except lock.LockTimeout:
        raise CommandError("Error: could not acquire lock", returncode=1) from None


def _parent_status_uuid(ticket_dir_path: str) -> str | None:
    """UUID of the most recent prior STATUS event for this ticket, or None if this
    is the first. STATUS event files sort by filename (timestamp prefix ⇒
    chronological)."""
    try:
        status_files = sorted(
            (
                f
                for f in os.listdir(ticket_dir_path)
                if f.endswith("-STATUS.json") and not f.startswith(".")
            ),
            key=lambda f: (_prefix_ts(f), f),
        )
        if status_files:
            most_recent = os.path.join(ticket_dir_path, status_files[-1])
            with open(most_recent, encoding="utf-8") as sf:
                prev = json.load(sf)
            return prev.get("uuid") or None
    except Exception:
        return None
    return None


def _git(tracker_dir: str, *args: str) -> None:
    """Run a git command in the tracker, raising :class:`CommandError` (exit 2) on
    failure with the exact bash stderr prefix."""
    cp = subprocess.run(["git", "-C", tracker_dir, *args], capture_output=True, text=True)
    if cp.returncode != 0:
        raise CommandError(f"Error: git operation failed: {cp.stderr}", returncode=2)


def _unstage(tracker_dir: str, *abs_paths: str | None) -> None:
    """Best-effort: drop ``abs_paths`` from the git index (and working tree). On a
    commit failure the event file was already ``git add``-ed; removing it from disk
    alone leaves it STAGED, so the next write's commit would sweep the orphaned
    event in. Reset the index entry too. Held under the write lock, so this is the
    sole writer. Never raises (cleanup path)."""
    rels = [os.path.relpath(p, tracker_dir) for p in abs_paths if p]
    if not rels:
        return
    try:
        subprocess.run(
            ["git", "-C", tracker_dir, "reset", "-q", "--", *rels],
            capture_output=True,
            text=True,
        )
    except OSError:
        pass


def transition_core(
    tracker_dir: str,
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    env_id: str,
    author: str,
    close_reason: str = "",
    verdict_hash: str = "",
    force_close_reason: str = "",
) -> None:
    """Write the append-only STATUS(``target_status``) event under the write lock.

    Re-reads the ticket under the lock and rejects with :class:`ConcurrencyMismatch`
    (exit 10) if its status is not ``current_status``. Applies the bug-close-reason
    and (opt-in) story/epic signature-close guards. Raises :class:`CommandError` for
    validation / git failures. Returns ``None`` on success (the wrapper computes
    newly_unblocked + output separately)."""
    handle = _acquire_write_lock(tracker_dir)
    final_path = None
    try:
        state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
        if state is None:
            raise CommandError(
                "Error: reducer returned no state (ticket may be corrupt or missing events)",
                returncode=1,
            )

        # session_log tickets are lifecycle-exempt: they have no workflow status
        # to advance. Refuse transition authoritatively (before the concurrency
        # check) so the message is clear regardless of the supplied current_status.
        if state.get("ticket_type", "") == "session_log":
            raise CommandError(
                "Error: session_log tickets are lifecycle-exempt and cannot be "
                "transitioned (they are not claimed, transitioned, or closed)",
                returncode=1,
            )

        actual_status = state.get("status", "")
        if actual_status != current_status:
            if actual_status == "archived":
                hint = (
                    f"ticket transition {ticket_id} archived open  "
                    "(un-archive; archived is otherwise inescapable via transition)"
                )
            else:
                hint = f"ticket transition {ticket_id} {actual_status} {target_status}"
            raise ConcurrencyMismatch(
                f'Error: current status is "{actual_status}", not "{current_status}". '
                f"Re-run: {hint}"
            )

        ticket_type = state.get("ticket_type", "")

        # Bug-close-reason guard.
        if target_status == "closed" and ticket_type == "bug":
            if not close_reason:
                raise CommandError(
                    "Error: closing a bug ticket requires --reason with prefix "
                    '"Fixed:" or "Escalated to user:"',
                    returncode=1,
                )
            if not (close_reason.startswith("Fixed") or close_reason.lower().startswith("escalat")):
                raise CommandError(
                    'Error: --reason must start with "Fixed:" or "Escalated to user:"',
                    returncode=1,
                )

        # Signature gate (story/epic closure; opt-in via config).
        if target_status == "closed" and ticket_type in ("story", "epic"):
            _signature_gate(
                tracker_dir, ticket_id, ticket_type, state, verdict_hash, force_close_reason
            )

        ticket_dir_path = os.path.join(tracker_dir, ticket_id)
        parent_status_uuid = _parent_status_uuid(ticket_dir_path)

        timestamp = hlc.next_tick(tracker_dir, ticket_id)
        event_uuid = str(uuid.uuid4())
        event = {
            "timestamp": timestamp,
            "uuid": event_uuid,
            "event_type": "STATUS",
            "env_id": env_id,
            "author": author,
            "parent_status_uuid": parent_status_uuid,
            "data": {
                "status": target_status,
                "current_status": current_status,
                "parent_status_uuid": parent_status_uuid,
            },
        }

        temp_path = os.path.join(tracker_dir, f".tmp-transition-{event_uuid}")
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(canonical_str(event))

        final_filename = event_append.event_filename(timestamp, event_uuid, "STATUS")
        final_path = os.path.join(ticket_dir_path, final_filename)
        os.rename(temp_path, final_path)

        _git(tracker_dir, "config", "gc.auto", "0")
        _git(tracker_dir, "add", f"{ticket_id}/{final_filename}")
        _git(tracker_dir, "commit", "-q", "--no-verify", "-m", f"ticket: STATUS {ticket_id}")
    except CommandError:
        if final_path is not None:
            _unstage(tracker_dir, final_path)  # drop from index (not just disk)
            try:
                os.remove(final_path)
            except OSError:
                pass
        raise
    except Exception as exc:
        raise CommandError(f"Error: {exc}", returncode=1) from None
    finally:
        handle.release()


def _signature_gate(
    tracker_dir: str,
    ticket_id: str,
    ticket_type: str,
    state: dict,
    verdict_hash: str,
    force_close_reason: str,
) -> None:
    """Story/epic close gate: require a CERTIFIED signature made at the current HEAD
    (OFF unless ``verify.require_signature_for_close=true`` — legacy alias
    ``verify.require_verdict_for_close=true`` — in ``.rebar/config.conf``).

    A manifest of verified steps is HMAC-signed with the environment key
    (`rebar sign <id> <manifest>`) and recomputed/certified here; the signature
    must also have been made at the current HEAD so a stale attestation cannot
    close work whose code has since changed. Raises :class:`CommandError` when the
    ticket is not certified; a force-close reason bypasses with a stderr warning
    (the wrapper writes the audit comment). Replaces the legacy verdict-hash gate;
    ``--verdict-hash`` is deprecated and ignored."""
    cfg_root = os.environ.get("REBAR_ROOT") or tracker_dir.rsplit("/", 1)[0]
    # Resolve the verify gate through the unified typed config (all layers + the
    # legacy require_verdict_for_close alias). Fail-CLOSED: a present-but-unreadable
    # config must never silently disable the gate — require a signature. An absent
    # config returns the default (gate off), the intended opt-out.
    from rebar.config import ConfigError, load_config

    try:
        require_sig = load_config(cfg_root).verify.require_signature_for_close
    except ConfigError as cfg_exc:
        import sys

        print(
            f"Warning: could not read rebar config ({cfg_exc}); requiring a "
            f"signature to close {ticket_type} {ticket_id} (fail-closed).",
            file=sys.stderr,
        )
        require_sig = True

    if not require_sig:
        return

    import sys

    if verdict_hash:
        print(
            "Warning: --verdict-hash is deprecated and ignored; the close gate now "
            "uses signatures (rebar sign <id> <manifest>).",
            file=sys.stderr,
        )

    if force_close_reason:
        print(
            f"Warning: closing {ticket_type} {ticket_id} via --force-close "
            "(signature gate bypassed)",
            file=sys.stderr,
        )
        print(f"  Reason: {force_close_reason}", file=sys.stderr)
        return

    from rebar import config as _config
    from rebar import signing as _signing

    key = _signing.signing_key(tracker_dir, create_if_missing=False)
    result = _signing.verify_record(state.get("signature"), ticket_id, key)
    if not result["verified"]:
        raise CommandError(
            f"Error: closing a {ticket_type} requires a certified signature "
            f"(verdict: {result['verdict']}).\n"
            "  Recovery: sign a manifest of verified steps, then close:\n"
            f'    rebar sign {ticket_id} \'["step one: PASS", "step two: PASS"]\'\n'
            f"    rebar transition {ticket_id} closed\n"
            '  Override: use --force-close="<reason>" to bypass (requires user approval).',
            returncode=1,
        )
    # Git-state binding: the attestation must be for the current HEAD. An
    # unresolvable HEAD ('unknown') must NEVER satisfy the binding — otherwise
    # 'unknown' == 'unknown' would silently void the freshness guard.
    head_sha = _signing.head_sha(_config.repo_root())
    if head_sha == "unknown" or result.get("head_sha") != head_sha:
        raise CommandError(
            f"Error: the signature for {ticket_type} {ticket_id} was made at a "
            f"different commit (signed at {result.get('head_sha')}, HEAD is {head_sha}).\n"
            f"  Recovery: re-sign at the current HEAD:\n"
            f"    rebar sign {ticket_id} '[...verified steps...]'\n"
            '  Override: use --force-close="<reason>" to bypass (requires user approval).',
            returncode=1,
        )


def claim_core(
    tracker_dir: str,
    ticket_id: str,
    *,
    env_id: str,
    author: str,
    assignee: str = "",
) -> None:
    """Atomic claim: move an ``open`` ticket to ``in_progress`` AND set its assignee
    in ONE locked critical section (single commit). Rejects with
    :class:`ConcurrencyMismatch` (exit 10) if the ticket is not ``open``.

    Both the STATUS(in_progress) and EDIT(assignee) events are fresh UUID-named
    files written and committed in ONE commit before the lock releases, so no
    reader on any clone ever observes in_progress without the assignee (I2/I8;
    docs/concurrency.md)."""
    handle = _acquire_write_lock(tracker_dir)
    status_path = None
    edit_path = None
    try:
        state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
        if state is None:
            raise CommandError(
                "Error: reducer returned no state (ticket may be corrupt or missing events)",
                returncode=1,
            )
        # session_log tickets are lifecycle-exempt: they cannot be claimed (no
        # status to advance, and they never participate in the work workflow).
        if state.get("ticket_type", "") == "session_log":
            raise CommandError(
                "Error: session_log tickets are lifecycle-exempt and cannot be claimed",
                returncode=1,
            )
        actual_status = state.get("status", "")
        if actual_status != "open":
            raise ConcurrencyMismatch(
                f'Error: cannot claim {ticket_id}: status is "{actual_status}", not '
                '"open" (already claimed or not claimable).'
            )

        ticket_dir_path = os.path.join(tracker_dir, ticket_id)
        parent_status_uuid = _parent_status_uuid(ticket_dir_path)
        rel_paths = []

        # STATUS(open -> in_progress).
        ts1 = hlc.next_tick(tracker_dir, ticket_id)
        uuid1 = str(uuid.uuid4())
        status_event = {
            "timestamp": ts1,
            "uuid": uuid1,
            "event_type": "STATUS",
            "env_id": env_id,
            "author": author,
            "parent_status_uuid": parent_status_uuid,
            "data": {
                "status": "in_progress",
                "current_status": "open",
                "parent_status_uuid": parent_status_uuid,
            },
        }
        status_filename = event_append.event_filename(ts1, uuid1, "STATUS")
        status_tmp = os.path.join(tracker_dir, f".tmp-claim-{uuid1}")
        with open(status_tmp, "w", encoding="utf-8") as f:
            f.write(canonical_str(status_event))
        status_path = os.path.join(ticket_dir_path, status_filename)
        os.rename(status_tmp, status_path)
        rel_paths.append(f"{ticket_id}/{status_filename}")

        # EDIT(assignee) — only when supplied. ts2 ticked AFTER ts1 so STATUS sorts
        # before EDIT in replay (the HLC +1 floor makes ts2 > ts1 strictly).
        if assignee:
            ts2 = hlc.next_tick(tracker_dir, ticket_id)
            uuid2 = str(uuid.uuid4())
            edit_event = {
                "timestamp": ts2,
                "uuid": uuid2,
                "event_type": "EDIT",
                "env_id": env_id,
                "author": author,
                "data": {"fields": {"assignee": assignee}},
            }
            edit_filename = event_append.event_filename(ts2, uuid2, "EDIT")
            edit_tmp = os.path.join(tracker_dir, f".tmp-claim-{uuid2}")
            with open(edit_tmp, "w", encoding="utf-8") as f:
                f.write(canonical_str(edit_event))
            edit_path = os.path.join(ticket_dir_path, edit_filename)
            os.rename(edit_tmp, edit_path)
            rel_paths.append(f"{ticket_id}/{edit_filename}")

        # gc.auto=0, then stage BOTH events and commit ONCE (atomic).
        _git(tracker_dir, "config", "gc.auto", "0")
        _git(tracker_dir, "add", *rel_paths)
        _git(tracker_dir, "commit", "-q", "--no-verify", "-m", f"ticket: CLAIM {ticket_id}")
    except CommandError:
        _unstage(tracker_dir, status_path, edit_path)  # drop from index (not just disk)
        for p in (status_path, edit_path):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
        raise
    except Exception as exc:
        raise CommandError(f"Error: {exc}", returncode=1) from None
    finally:
        handle.release()
