"""rebar status-transition + claim critical section — BASH-LEG SHIM (Tier E E5c).

The critical-section logic now lives in the importable package module
:mod:`rebar._commands.txn` (so the CLI/library can call it in-process without
putting the engine dir on ``sys.path`` — the ``test_engine_dir`` guard). This file
remains ONLY as the thin shim the bash dispatcher still subprocesses
(``ticket-transition.sh`` / ``ticket-claim.sh``) until the dispatcher retires in E7.

It preserves the exact former positional-argv contract:

    python3 ticket_txn.py transition <lock> <tracker> <ticket> <current> <target>
                          <env_id> <author> <reducer> [close_reason]
                          [verdict_hash] [force_close_reason]
    python3 ticket_txn.py claim <lock> <tracker> <ticket> <env_id> <author>
                          <reducer> [assignee]

``<lock>`` and ``<reducer>`` are accepted for backward compatibility and ignored —
the package core derives the lock from the tracker and uses ``rebar.reducer``.

Exit codes (unchanged): 0 success; 10 optimistic-concurrency mismatch; 1 lock
timeout / validation / generic; 2 git operation failure. The core RAISES; this
shim prints the carried stderr message and exits with the carried code.
"""

import os
import sys

# Bootstrap the `rebar` package: this runs as a bare `python3` subprocess with only
# the engine dir on sys.path. __file__ = .../src/rebar/_engine/ticket_txn.py →
# three dirnames up = .../src (the dir that contains the `rebar` package).
_REBAR_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REBAR_SRC not in sys.path:
    sys.path.insert(0, _REBAR_SRC)

from rebar._commands._seam import CommandError  # noqa: E402
from rebar._commands import txn  # noqa: E402


def _fail(exc: CommandError):
    if exc.message:
        print(exc.message, file=sys.stderr)
    sys.exit(exc.returncode)


def _transition(argv):
    # argv: [prog, lock, tracker, ticket, current, target, env_id, author,
    #        reducer, close_reason?, verdict_hash?, force_close_reason?]
    tracker_dir = argv[2]
    ticket_id = argv[3]
    current_status = argv[4]
    target_status = argv[5]
    env_id_val = argv[6]
    author_val = argv[7]
    close_reason = argv[9] if len(argv) > 9 else ""
    verdict_hash_arg = argv[10] if len(argv) > 10 else ""
    force_close_reason_arg = argv[11] if len(argv) > 11 else ""
    try:
        txn.transition_core(
            tracker_dir,
            ticket_id,
            current_status,
            target_status,
            env_id=env_id_val,
            author=author_val,
            close_reason=close_reason,
            verdict_hash=verdict_hash_arg,
            force_close_reason=force_close_reason_arg,
        )
    except CommandError as exc:
        _fail(exc)
    sys.exit(0)


def _claim(argv):
    # argv: [prog, lock, tracker, ticket, env_id, author, reducer, assignee?]
    tracker_dir = argv[2]
    ticket_id = argv[3]
    env_id_val = argv[4]
    author_val = argv[5]
    assignee = argv[7] if len(argv) > 7 else ""
    try:
        txn.claim_core(
            tracker_dir,
            ticket_id,
            env_id=env_id_val,
            author=author_val,
            assignee=assignee,
        )
    except CommandError as exc:
        _fail(exc)
    sys.exit(0)


def main(argv):
    """Dispatch on the operation verb (argv[1]); each op keeps 1-based argv indexing
    via the ``[argv[0]] + argv[2:]`` reconstruction (no re-indexing)."""
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
