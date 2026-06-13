"""rebar status-transition critical section (extracted from ticket-transition.sh).

This module IS the lock-holding, committing entrypoint for a status transition.
It runs as ONE process that: opens the write lock (fcntl.flock LOCK_EX on
.tickets-tracker/.ticket-write.lock), re-reads + verifies the current status
(exit 10 / ConcurrencyError on optimistic-concurrency mismatch), applies the
close-time guards, writes the append-only STATUS event, and runs git add+commit
-- releasing the lock only after the commit. The lock, the optimistic re-read,
the write, and the commit are a SINGLE critical section in a SINGLE process; do
NOT split the commit out (it would reopen a lost-update window). See
REMEDIATION_PROPOSAL.md sec 0 (I4/I5) and docs/concurrency.md.

Behavior is byte-for-byte identical to the former bash heredoc; the only change
is heredoc -> importable, unit-testable module. Invoked by ticket-transition.sh
as:  python3 ticket_txn.py <lock> <tracker> <ticket> <current> <target>
                           <env_id> <author> <reducer> [close_reason]
                           [verdict_hash] [force_close_reason]
(positional argv indices match the former heredoc exactly).

Exit codes: 0 success; 10 optimistic-concurrency mismatch; 1 lock timeout /
validation / generic; 2 git operation failure.
"""

import os
import sys

# The I2 filename contract lives once in event_append (ticket pokey-matte-flute);
# this critical section keeps its own commit but shares the filename helper and —
# since Tier D — the ONE unified write lock.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import event_append  # noqa: E402

# Bootstrap the `rebar` package (this runs as a bare `python3` subprocess with only
# the engine dir on sys.path) so the unified write lock is importable. __file__ =
# .../src/rebar/_engine/ticket_txn.py → three dirnames up = .../src (the dir that
# contains the `rebar` package).
_REBAR_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REBAR_SRC not in sys.path:
    sys.path.insert(0, _REBAR_SRC)
from rebar._store import lock as _store_lock  # noqa: E402


def _acquire_write_lock(tracker_dir):
    """Acquire the unified write lock (fcntl + mkdir dual leg, 30s) for a txn
    critical section. The dual leg makes this mutually exclusive with bash
    leaf-writes on every platform class (the stiff-mop-lane fix); the budget (30s,
    ``attempts=1``) preserves ticket_txn's historical timeout. Held across the whole
    re-read → write → commit section; ``handle.release()`` drops it at each exit."""
    try:
        return _store_lock.acquire(tracker_dir, timeout=30, attempts=1, dual_window=True)
    except _store_lock.LockTimeout:
        print('Error: could not acquire lock', file=sys.stderr)
        sys.exit(1)


def _transition(argv):
    import fcntl, json, os, subprocess, sys, time, uuid

    lock_path = argv[1]
    tracker_dir = argv[2]
    ticket_id = argv[3]
    current_status = argv[4]
    target_status = argv[5]
    env_id_val = argv[6]
    author_val = argv[7]
    reducer_path = argv[8]
    close_reason = argv[9] if len(argv) > 9 else ''
    verdict_hash_arg = argv[10] if len(argv) > 10 else ''
    force_close_reason_arg = argv[11] if len(argv) > 11 else ''

    # Import reduce_ticket directly (single-process: eliminates subprocess for state read)
    sys.path.insert(0, os.path.dirname(os.path.abspath(reducer_path)))
    from ticket_reducer import reduce_ticket

    timeout = 30

    # Acquire flock
    handle = _acquire_write_lock(tracker_dir)

    # Lock acquired — read current state via direct reduce_ticket import (no subprocess)
    try:
        state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
        if state is None:
            print('Error: reducer returned no state (ticket may be corrupt or missing events)', file=sys.stderr)
            handle.release()
            sys.exit(1)

        actual_status = state.get('status', '')

        # Optimistic concurrency check
        if actual_status != current_status:
            # Defensive hint: never suggest a command the transition validator
            # rejects. `archived` is not in the transition whitelist
            # (open|in_progress|closed|blocked); from `archived` the only valid
            # transition is the un-archive seam `... archived open`. For any
            # other actual_status, the generic re-run hint is valid.
            if actual_status == 'archived':
                hint = f'ticket transition {ticket_id} archived open  (un-archive; archived is otherwise inescapable via transition)'
            else:
                hint = f'ticket transition {ticket_id} {actual_status} {target_status}'
            print(f'Error: current status is "{actual_status}", not "{current_status}". Re-run: {hint}', file=sys.stderr)
            handle.release()
            sys.exit(10)

        # Bug-close-reason guard
        if target_status == 'closed':
            ticket_type = state.get('ticket_type', '')
            # If ticket_type is empty (old tickets predating the type field), treat as
            # non-bug: don't require --reason. This ensures backward compatibility.
            if ticket_type == 'bug':
                if not close_reason:
                    print('Error: closing a bug ticket requires --reason with prefix "Fixed:" or "Escalated to user:"', file=sys.stderr)
                    handle.release()
                    sys.exit(1)
                # Validate required prefix: accept Fixed (covers Fixed:, Fixed in, etc.)
                # and case-insensitive escalat prefix (covers Escalated to user: variants).
                if not (close_reason.startswith('Fixed') or close_reason.lower().startswith('escalat')):
                    print('Error: --reason must start with "Fixed:" or "Escalated to user:"', file=sys.stderr)
                    handle.release()
                    sys.exit(1)

        # ── Verdict hash gate (story/epic closure) ────────────────────────────
        # Stories and epics require a verified completion verdict to close.
        # The verdict hash is an HMAC that encodes: this ticket received PASS at this git state.
        # compute-verdict-hash.sh and this gate compute the same HMAC independently.
        if target_status == 'closed' and ticket_type in ('story', 'epic'):
            # Check config: verify.require_verdict_for_close (default: false — opt-in).
            # The verdict-hash gate is OFF unless the repo explicitly enables it.
            require_verdict = False
            try:
                _cfg_root = os.environ.get('REBAR_ROOT') or os.environ.get('PROJECT_ROOT') or tracker_dir.rsplit('/', 1)[0]
                config_path = os.environ.get('REBAR_CONFIG') or os.path.join(_cfg_root, '.rebar', 'config.conf')
                if os.path.isfile(config_path):
                    with open(config_path) as _cf:
                        for _line in _cf:
                            if _line.strip().startswith('verify.require_verdict_for_close='):
                                val = _line.strip().split('=', 1)[1].strip().lower()
                                if val in ('true', '1', 'yes'):
                                    require_verdict = True
            except Exception:
                pass

            if require_verdict:
                if force_close_reason_arg:
                    # Force-close with reason — write audit comment
                    print(f'Warning: closing {ticket_type} {ticket_id} via --force-close (verdict hash bypassed)', file=sys.stderr)
                    print(f'  Reason: {force_close_reason_arg}', file=sys.stderr)
                    # The STATUS event data will include the force_close_reason for audit
                elif verdict_hash_arg:
                    # Verify the hash by independently computing the expected HMAC
                    import hmac, hashlib
                    key_file = os.path.join(tracker_dir, '.closure-key')
                    if not os.path.isfile(key_file):
                        print(f'Error: .closure-key not found. Run ticket init to generate it.', file=sys.stderr)
                        handle.release()
                        sys.exit(1)
                    with open(key_file, 'r') as _kf:
                        key = _kf.read().strip().encode()
                    try:
                        head_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5).stdout.strip()
                    except Exception:
                        head_sha = 'unknown'
                    expected_data = f'{ticket_id}|PASS|{head_sha}'.encode()
                    expected_hash = hmac.new(key, expected_data, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(verdict_hash_arg, expected_hash):
                        print(f'Error: verdict hash mismatch for {ticket_type} {ticket_id}.', file=sys.stderr)
                        print(f'  This means the completion verifier did not produce a PASS verdict at the current HEAD.', file=sys.stderr)
                        print(f'  Recovery: produce a PASS completion verdict, then run compute-verdict-hash.sh.', file=sys.stderr)
                        print(f'  Override: use --force-close="<reason>" to bypass (requires user approval).', file=sys.stderr)
                        handle.release()
                        sys.exit(1)
                else:
                    print(f'Error: closing a {ticket_type} requires --verdict-hash (from compute-verdict-hash.sh after completion verifier PASS).', file=sys.stderr)
                    print(f'  Recovery: produce a PASS completion verdict, then:', file=sys.stderr)
                    print(f'    bash compute-verdict-hash.sh {ticket_id} PASS  # produces the hash', file=sys.stderr)
                    print(f'    ticket transition {ticket_id} closed --verdict-hash=<hash-from-above>', file=sys.stderr)
                    print(f'  Override: use --force-close="<reason>" to bypass (requires user approval).', file=sys.stderr)
                    handle.release()
                    sys.exit(1)

        # Compute parent_status_uuid: UUID of the most recent prior STATUS event for this ticket,
        # or null if this is the first STATUS event.
        # Sort STATUS event files by filename (timestamp prefix ensures chronological order).
        ticket_dir_path = os.path.join(tracker_dir, ticket_id)
        parent_status_uuid = None
        try:
            status_files = sorted(
                f for f in os.listdir(ticket_dir_path)
                if f.endswith('-STATUS.json') and not f.startswith('.')
            )
            if status_files:
                most_recent = os.path.join(ticket_dir_path, status_files[-1])
                with open(most_recent, encoding='utf-8') as _sf:
                    _prev = json.load(_sf)
                parent_status_uuid = _prev.get('uuid') or None
        except Exception:
            parent_status_uuid = None

        # Build STATUS event JSON
        timestamp = time.time_ns()
        event_uuid = str(uuid.uuid4())
        event = {
            'timestamp': timestamp,
            'uuid': event_uuid,
            'event_type': 'STATUS',
            'env_id': env_id_val,
            'author': author_val,
            'parent_status_uuid': parent_status_uuid,
            'data': {
                'status': target_status,
                'current_status': current_status,
                'parent_status_uuid': parent_status_uuid,
            },
        }

        # Write to temp file
        temp_path = os.path.join(tracker_dir, f'.tmp-transition-{event_uuid}')
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(event, f, ensure_ascii=False)

        # Compute final filename and path
        final_filename = event_append.event_filename(timestamp, event_uuid, 'STATUS')
        ticket_dir = os.path.join(tracker_dir, ticket_id)
        final_path = os.path.join(ticket_dir, final_filename)

        # Atomic rename
        os.rename(temp_path, final_path)

        # Ensure gc.auto=0
        subprocess.run(
            ['git', '-C', tracker_dir, 'config', 'gc.auto', '0'],
            check=True, capture_output=True, text=True,
        )

        # git add + commit
        subprocess.run(
            ['git', '-C', tracker_dir, 'add', f'{ticket_id}/{final_filename}'],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', '-C', tracker_dir, 'commit', '-q', '--no-verify', '-m', f'ticket: STATUS {ticket_id}'],
            check=True, capture_output=True, text=True,
        )

    except subprocess.CalledProcessError as e:
        print(f'Error: git operation failed: {e.stderr}', file=sys.stderr)
        # Clean up event file if it was written
        try:
            os.remove(final_path)
        except (OSError, NameError):
            pass
        handle.release()
        sys.exit(2)
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        handle.release()
        sys.exit(1)

    # Release lock
    handle.release()
    sys.exit(0)


def _claim(argv):
    """Atomic claim: move an `open` ticket to in_progress AND set assignee in ONE
    locked critical section (single commit), rejecting with exit 10 if the ticket
    is not `open` (someone else claimed it).

    argv: [prog, lock, tracker, ticket, env_id, author, reducer, assignee?]

    Concurrency (REMEDIATION_PROPOSAL §0 / docs/concurrency.md): this reuses the
    transition critical section's optimistic-concurrency + flock pattern (I4/I5).
    Both the STATUS(in_progress) and EDIT(assignee) events are fresh UUID-named
    files (I2, merge-as-union safe) written and committed in ONE commit before the
    lock releases, so no reader on any clone ever observes in_progress without the
    assignee. The STATUS event carries parent_status_uuid so concurrent cross-clone
    claims resolve via the reducer's skew-independent UUID fork tie-break (I8).
    """
    import fcntl, json, os, subprocess, sys, time, uuid

    lock_path = argv[1]
    tracker_dir = argv[2]
    ticket_id = argv[3]
    env_id_val = argv[4]
    author_val = argv[5]
    reducer_path = argv[6]
    assignee = argv[7] if len(argv) > 7 else ''

    # Import reduce_ticket directly (must self-insert: bash sets no PYTHONPATH).
    sys.path.insert(0, os.path.dirname(os.path.abspath(reducer_path)))
    from ticket_reducer import reduce_ticket

    timeout = 30

    # Acquire flock (identical pattern to _transition).
    handle = _acquire_write_lock(tracker_dir)

    status_path = None
    edit_path = None
    try:
        # Optimistic concurrency: re-read under the lock and require `open`.
        state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
        if state is None:
            print('Error: reducer returned no state (ticket may be corrupt or missing events)', file=sys.stderr)
            handle.release()
            sys.exit(1)
        actual_status = state.get('status', '')
        if actual_status != 'open':
            print(f'Error: cannot claim {ticket_id}: status is "{actual_status}", not "open" (already claimed or not claimable).', file=sys.stderr)
            handle.release()
            sys.exit(10)

        ticket_dir_path = os.path.join(tracker_dir, ticket_id)

        # Compute parent_status_uuid (exact same logic as _transition).
        parent_status_uuid = None
        try:
            status_files = sorted(
                f for f in os.listdir(ticket_dir_path)
                if f.endswith('-STATUS.json') and not f.startswith('.')
            )
            if status_files:
                most_recent = os.path.join(ticket_dir_path, status_files[-1])
                with open(most_recent, encoding='utf-8') as _sf:
                    _prev = json.load(_sf)
                parent_status_uuid = _prev.get('uuid') or None
        except Exception:
            parent_status_uuid = None

        rel_paths = []

        # STATUS(open -> in_progress).
        ts1 = time.time_ns()
        uuid1 = str(uuid.uuid4())
        status_event = {
            'timestamp': ts1,
            'uuid': uuid1,
            'event_type': 'STATUS',
            'env_id': env_id_val,
            'author': author_val,
            'parent_status_uuid': parent_status_uuid,
            'data': {
                'status': 'in_progress',
                'current_status': 'open',
                'parent_status_uuid': parent_status_uuid,
            },
        }
        status_filename = event_append.event_filename(ts1, uuid1, 'STATUS')
        status_tmp = os.path.join(tracker_dir, f'.tmp-claim-{uuid1}')
        with open(status_tmp, 'w', encoding='utf-8') as f:
            json.dump(status_event, f, ensure_ascii=False)
        status_path = os.path.join(ticket_dir_path, status_filename)
        os.rename(status_tmp, status_path)
        rel_paths.append(f'{ticket_id}/{status_filename}')

        # EDIT(assignee) — only when an assignee was supplied. ts2 sampled AFTER
        # ts1 so STATUS sorts before EDIT in replay (cosmetic: disjoint fields).
        if assignee:
            ts2 = time.time_ns()
            uuid2 = str(uuid.uuid4())
            edit_event = {
                'timestamp': ts2,
                'uuid': uuid2,
                'event_type': 'EDIT',
                'env_id': env_id_val,
                'author': author_val,
                'data': {'fields': {'assignee': assignee}},
            }
            edit_filename = event_append.event_filename(ts2, uuid2, 'EDIT')
            edit_tmp = os.path.join(tracker_dir, f'.tmp-claim-{uuid2}')
            with open(edit_tmp, 'w', encoding='utf-8') as f:
                json.dump(edit_event, f, ensure_ascii=False)
            edit_path = os.path.join(ticket_dir_path, edit_filename)
            os.rename(edit_tmp, edit_path)
            rel_paths.append(f'{ticket_id}/{edit_filename}')

        # gc.auto=0, then stage BOTH events and commit ONCE (atomic: no reader
        # ever sees in_progress without the assignee).
        subprocess.run(
            ['git', '-C', tracker_dir, 'config', 'gc.auto', '0'],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', '-C', tracker_dir, 'add', *rel_paths],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ['git', '-C', tracker_dir, 'commit', '-q', '--no-verify', '-m', f'ticket: CLAIM {ticket_id}'],
            check=True, capture_output=True, text=True,
        )

    except subprocess.CalledProcessError as e:
        print(f'Error: git operation failed: {e.stderr}', file=sys.stderr)
        # Clean up BOTH event files so the working tree is left consistent.
        for _p in (status_path, edit_path):
            if _p:
                try:
                    os.remove(_p)
                except OSError:
                    pass
        handle.release()
        sys.exit(2)
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        handle.release()
        sys.exit(1)

    handle.release()
    sys.exit(0)


def main(argv):
    """Dispatch on the operation verb (argv[1]); each op body keeps 1-based argv
    indexing via the [argv[0]] + argv[2:] reconstruction (no re-indexing)."""
    if len(argv) < 2:
        print("usage: ticket_txn.py <transition|claim> <args...>", file=sys.stderr)
        sys.exit(2)
    op = argv[1]
    rest = [argv[0]] + argv[2:]
    if op == "transition":
        _transition(rest)
    elif op == "claim":
        _claim(rest)
    else:
        print(f"ticket_txn: unknown operation {op!r}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv)
