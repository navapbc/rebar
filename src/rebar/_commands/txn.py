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
from rebar._store import event_append, fsutil, hlc, lock
from rebar._store.canonical import canonical_str
from rebar._store.gitutil import run_git_write
from rebar.reducer import reduce_ticket
from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES
from rebar.reducer._sort import prefix_ts as _prefix_ts


class ConcurrencyMismatch(CommandError):
    """Optimistic-concurrency rejection (exit 10): the ticket's actual status no
    longer matches the caller's expectation, or a claim target is not ``open``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, returncode=10)


def _stamp_session(status_data: dict) -> None:
    """Add the claiming session provenance to an ``open -> in_progress`` STATUS event's
    ``data`` when the shared resolvers find any (epic crust-fetch-stump, stories 68ef +
    c557): the primary session id (``session``), the harness tag (``harness``), and the
    secondary remote session (``remote_session``). Each absent value OMITS its key, so a
    no-provenance claim's event bytes are identical to the pre-feature path (older clones
    preserve-and-ignore the extra keys). Values are opaque strings, read verbatim — never
    interpolated or executed."""
    from rebar._commands.session_id import (
        resolve_harness,
        resolve_remote_session,
        resolve_session_id,
    )

    for key, value in (
        ("session", resolve_session_id()),
        ("harness", resolve_harness()),
        ("remote_session", resolve_remote_session()),
    ):
        if value:
            status_data[key] = value


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
    except Exception:  # noqa: BLE001 — best-effort prev-STATUS read; fall open to None (no expected-status guard)
        return None
    return None


def _git(tracker_dir: str, *args: str) -> None:
    """Run a git command in the tracker, raising :class:`CommandError` (exit 2) on
    failure with the exact bash stderr prefix.

    Routed through :func:`run_git_write` so any index-mutating op (the claim/transition
    ``add``+``commit``) self-heals git's ``.git/index.lock`` contention — a stale lock is
    reclaimed and a contended one ridden out with a bounded backoff before this reports a
    genuine (post-retry) failure. index.lock only appears on index-mutating commands, so a
    read op run through here simply never trips the retry (bug fix-indexlock-retry)."""
    cp = run_git_write(tracker_dir, *args, check=False)
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


def bug_close_reason_ok(reason: str) -> bool:
    """True if a bug-close ``--reason`` is acceptable (starts with ``Fixed`` or
    ``Escalated``…). Shared by :func:`transition_core`'s close guard and the completion
    gate's pre-check (in ``transition_compute``) so the two cannot drift. Empty → False."""
    if not reason:
        return False
    return reason.startswith("Fixed") or reason.lower().startswith("escalat")


def transition_core(
    tracker_dir: str,
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    env_id: str,
    author: str,
    close_reason: str = "",
    force_close_reason: str = "",
) -> None:
    """Write the append-only STATUS(``target_status``) event under the write lock.

    Re-reads the ticket under the lock and rejects with :class:`ConcurrencyMismatch`
    (exit 10) if its status is not ``current_status``. Applies the bug-close-reason
    and (opt-in) story/epic signature-close guards. Raises :class:`CommandError` for
    validation / git failures. Returns ``None`` on success (the wrapper computes
    newly_unblocked + output separately)."""
    # Resolve the signature-close gate FLAG *outside* the write lock — matching the completion
    # gate, whose flag is resolved before the locked core — so the config read never holds the
    # lock. Only a close can trigger the gate, so resolve only then (the ticket_type-specific
    # applicability + the fail-closed WARNING are deferred to `_signature_gate`, which fires the
    # warning under the lock once the re-read confirms a story/epic — so the message names the
    # right type and only when the gate applies). The actual signature CHECK needs the fresh
    # under-lock state, so it stays inside the lock (see `_signature_gate`).
    sig_require: bool | None = None
    sig_config_error: str | None = None
    if target_status == "closed":
        from rebar._commands.gates import resolve_signature_gate

        cfg_root = os.environ.get("REBAR_ROOT") or tracker_dir.rsplit("/", 1)[0]
        sig_require, sig_config_error = resolve_signature_gate(cfg_root)

    handle = _acquire_write_lock(tracker_dir)
    final_path = None
    try:
        state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
        if state is None:
            raise CommandError(
                "Error: reducer returned no state (ticket may be corrupt or missing events)",
                returncode=1,
            )

        # session_log / code_review artifacts are lifecycle-exempt: they have no
        # workflow status to advance. Refuse transition authoritatively (before the
        # concurrency check) so the message is clear regardless of current_status.
        if state.get("ticket_type", "") in _NON_GRAPH_ARTIFACT_TYPES:
            _t = state.get("ticket_type", "")
            raise CommandError(
                f"Error: {_t} tickets are lifecycle-exempt and cannot be "
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

        # `idea → closed` is a reject/drop, not a completion: an undesigned idea has
        # nothing built to verify or attest, so it bypasses BOTH the bug-close-reason
        # guard and the story/epic signature gate below (mirrors the completion-precheck
        # bypass in transition_close.close_ticket). The open-children structural guard is
        # enforced elsewhere and is NOT relaxed for idea.
        from_idea = current_status == "idea"

        # Bug-close-reason guard (predicate shared with the completion gate's pre-check).
        if target_status == "closed" and ticket_type == "bug" and not from_idea:
            if not close_reason:
                raise CommandError(
                    "Error: closing a bug ticket requires --reason with prefix "
                    '"Fixed:" or "Escalated to user:"',
                    returncode=1,
                )
            if not bug_close_reason_ok(close_reason):
                raise CommandError(
                    'Error: --reason must start with "Fixed:" or "Escalated to user:"',
                    returncode=1,
                )

        # Signature gate (story/epic closure; opt-in via config). The flag was resolved OUTSIDE
        # the lock (above); only the signature CHECK runs here, under the lock, on the fresh
        # re-read `state`. (`sig_require` is set whenever target_status == "closed", so it is a
        # real bool here, never None.)
        if target_status == "closed" and ticket_type in ("story", "epic") and not from_idea:
            _signature_gate(
                tracker_dir,
                ticket_id,
                ticket_type,
                state,
                force_close_reason,
                require_sig=bool(sig_require),
                config_error=sig_config_error,
            )

        ticket_dir_path = os.path.join(tracker_dir, ticket_id)
        parent_status_uuid = _parent_status_uuid(ticket_dir_path)

        timestamp = hlc.next_tick(tracker_dir, ticket_id)
        event_uuid = str(uuid.uuid4())
        status_data = {
            "status": target_status,
            "current_status": current_status,
            "parent_status_uuid": parent_status_uuid,
        }
        # Record the claiming session id on ANY open -> in_progress STATUS (bare
        # transition too, incl. the parent-first cascade), mirroring claim (epic
        # crust-fetch-stump, story 68ef). Absent -> key omitted -> byte-identical.
        if current_status == "open" and target_status == "in_progress":
            _stamp_session(status_data)
        event = {
            "timestamp": timestamp,
            "uuid": event_uuid,
            "event_type": "STATUS",
            "env_id": env_id,
            "author": author,
            "parent_status_uuid": parent_status_uuid,
            "data": status_data,
        }

        final_filename = event_append.event_filename(timestamp, event_uuid, "STATUS")
        final_path = os.path.join(ticket_dir_path, final_filename)
        fsutil.atomic_write(final_path, canonical_str(event), encoding="utf-8")

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
    except Exception as exc:  # noqa: BLE001 — fail-closed: any write failure re-raises as CommandError (exit 1)
        raise CommandError(f"Error: {exc}", returncode=1) from None
    finally:
        handle.release()


def _signature_gate(
    tracker_dir: str,
    ticket_id: str,
    ticket_type: str,
    state: dict,
    force_close_reason: str,
    *,
    require_sig: bool,
    config_error: str | None,
) -> None:
    """Story/epic close gate: require a CERTIFIED signature made at the current HEAD
    (OFF unless ``verify.require_signature_for_close=true`` in ``rebar.toml``).

    The FLAG (is the gate on?) is resolved by ``gates.resolve_signature_gate`` OUTSIDE the write
    lock and passed in as ``require_sig`` / ``config_error`` — so this function performs only the
    actual signature CHECK, which needs the fresh under-lock ``state``. A present-but-unreadable
    config fails CLOSED (``require_sig=True`` with ``config_error`` set); its warning is emitted
    HERE (deferred from the flag resolution) now that ``ticket_type`` is known, so it names the
    right ticket and only fires when the gate applies.

    A manifest of verified steps is HMAC-signed with the environment key
    (`rebar sign <id> <manifest>`) and recomputed/certified here; the signature
    must also have been made at the current HEAD so a stale attestation cannot
    close work whose code has since changed. Raises :class:`CommandError` when the
    ticket is not certified; a force-close reason bypasses with a stderr warning
    (the wrapper writes the audit comment). Replaces the legacy verdict-hash gate; the
    deprecated ``--verdict-hash`` flag is now ignored at the CLI parse boundary
    (``transition._warn_verdict_hash_deprecated``) and no longer reaches this gate."""
    import sys

    # Fail-closed warning, deferred from the (out-of-lock) flag resolution so it names the
    # ticket_type and fires only when the gate applies (a present-but-unreadable config →
    # require_sig=True, config_error set).
    if config_error is not None:
        print(
            f"Warning: could not read rebar config ({config_error}); requiring a "
            f"signature to close {ticket_type} {ticket_id} (fail-closed).",
            file=sys.stderr,
        )

    if not require_sig:
        return

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
    result = _signing.verify_record(_signing.most_recent_attestation(state), ticket_id, key)
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
        # session_log / code_review artifacts are lifecycle-exempt: they cannot be claimed
        # (no status to advance, and they never participate in the work workflow).
        if state.get("ticket_type", "") in _NON_GRAPH_ARTIFACT_TYPES:
            _t = state.get("ticket_type", "")
            raise CommandError(
                f"Error: {_t} tickets are lifecycle-exempt and cannot be claimed",
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
        status_data = {
            "status": "in_progress",
            "current_status": "open",
            "parent_status_uuid": parent_status_uuid,
        }
        # Record the claiming coding-agent session id when present (epic
        # crust-fetch-stump, story 68ef). Absent -> key omitted -> byte-identical to the
        # pre-feature event; the reducer folds it to state["claimed_session"].
        _stamp_session(status_data)
        status_event = {
            "timestamp": ts1,
            "uuid": uuid1,
            "event_type": "STATUS",
            "env_id": env_id,
            "author": author,
            "parent_status_uuid": parent_status_uuid,
            "data": status_data,
        }
        status_filename = event_append.event_filename(ts1, uuid1, "STATUS")
        status_path = os.path.join(ticket_dir_path, status_filename)
        fsutil.atomic_write(status_path, canonical_str(status_event), encoding="utf-8")
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
            edit_path = os.path.join(ticket_dir_path, edit_filename)
            fsutil.atomic_write(edit_path, canonical_str(edit_event), encoding="utf-8")
            rel_paths.append(f"{ticket_id}/{edit_filename}")

        # Stage BOTH events and commit ONCE (atomic).
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
    except Exception as exc:  # noqa: BLE001 — fail-closed: any claim-write failure re-raises as CommandError (exit 1)
        raise CommandError(f"Error: {exc}", returncode=1) from None
    finally:
        handle.release()
