"""In-process ``transition`` / ``reopen`` / ``claim`` wrappers.

These wrappers drive the locked write cores in :mod:`rebar._commands.txn`
(``transition_core`` / ``claim_core``) and own everything around the locked write:
``--output`` parsing, the 2-arg current-status autodetect, the ``archived → open``
un-archive seam, status validation, the idempotent no-op, the ghost / init checks,
the open-children close guard, ``newly_unblocked`` detection (via
:func:`rebar.graph._unblock.batch_close_operations`), the force-close audit
comment, compact-on-close, per-ticket scratch cleanup, the
``{ticket_id,from,to,newly_unblocked}`` json / ``UNBLOCKED:`` text output, and
claim's error-envelope (ticket_not_found / concurrency_conflict / claim_failed) +
``CLAIMED:`` output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from rebar import config
from rebar._commands import scratch, txn
from rebar._commands._seam import CommandError
from rebar._commands.txn import ConcurrencyMismatch
from rebar._engine_support.output import OutputFormatError, error_envelope, parse_output
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.graph._unblock import batch_close_operations
from rebar.reducer import reduce_ticket

_VALID_STATUSES = ("open", "in_progress", "closed", "blocked")

_USAGE = (
    "Usage: ticket transition <ticket_id> <current_status> <target_status> "
    "[--reason=<text>] [--force] [--force-close=<reason>]\n"
    "       ticket transition <ticket_id> <target_status> [--reason=<text>] [--force] "
    "[--force-close=<reason>]  (auto-detects current status)\n"
    "  current_status / target_status: open | in_progress | closed | blocked\n"
    "  --reason=<text>          Required when closing bug tickets. Must start with "
    "'Fixed:' or 'Escalated to user:'.\n"
    "  --force                  Skip the unresolved-children guard when closing. "
    "Non-closed children remain unresolved.\n"
    "  --verdict-hash=<hash>    DEPRECATED (ignored): the story/epic close gate now "
    "requires a certified signature ('rebar sign'), not a verdict hash.\n"
    "  --force-close=<reason>   Bypass the signature requirement for story/epic "
    "(requires user approval via hook).\n"
    "  Examples:\n"
    '    ticket transition abc1 open closed --reason="Fixed: patched null check in foo.sh"\n'
    "    rebar sign abc1 '[\"tests: PASS\"]' && ticket transition abc1 closed  "
    "# close story with a certified signature\n"
    '    ticket transition abc1 closed --force-close="verifier timed out"  # bypass with reason\n'
)


def _usage() -> int:
    sys.stderr.write(_USAGE)
    return 1


def _read_status(tracker: str, ticket_id: str) -> str | None:
    state = reduce_ticket(os.path.join(tracker, ticket_id))
    if state is None:
        return None
    status = state.get("status")
    if status in (None, "error", "fsck_needed"):
        return None
    return status


def _parse_flags(args: list[str]) -> tuple[str, bool, str, str]:
    """Parse [--reason[=]] [--force] [--verdict-hash[=]] [--force-close[=]] from the
    args AFTER <current> <target>. Returns (reason, force, verdict_hash,
    force_close_reason). Mirrors ticket-transition.sh's flag loop (unknown tokens
    are silently skipped)."""
    reason = ""
    force = False
    verdict_hash = ""
    force_close = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--reason="):
            reason = a[len("--reason=") :]
            i += 1
        elif a == "--reason":
            if i + 1 >= len(args):
                raise CommandError("Error: --reason requires a value", returncode=1)
            reason = args[i + 1]
            i += 2
        elif a == "--force":
            force = True
            i += 1
        elif a.startswith("--verdict-hash="):
            verdict_hash = a[len("--verdict-hash=") :]
            i += 1
        elif a == "--verdict-hash":
            if i + 1 >= len(args):
                raise CommandError("Error: --verdict-hash requires a value", returncode=1)
            verdict_hash = args[i + 1]
            i += 2
        elif a.startswith("--force-close="):
            force_close = a[len("--force-close=") :]
            i += 1
        elif a == "--force-close":
            if i + 1 >= len(args):
                raise CommandError("Error: --force-close requires a reason", returncode=1)
            force_close = args[i + 1]
            i += 2
        else:
            i += 1
    return reason, force, verdict_hash, force_close


def _validate_status(label: str, value: str) -> None:
    if value in _VALID_STATUSES:
        return
    if value.startswith("--"):
        raise CommandError(
            f"Error: invalid {label} '{value}'. Options like --reason must come AFTER "
            "<target_status>.\n"
            "  Correct: ticket transition <id> [<current_status>] <target_status> "
            '--reason="<text>"',
            returncode=1,
        )
    raise CommandError(
        f"Error: invalid {label} '{value}'. Must be one of: open, in_progress, closed, blocked",
        returncode=1,
    )


def _compact_on_close(repo_root: str, ticket_id: str) -> None:
    """Compact-on-close: squash the event log into a SNAPSHOT (non-blocking, output
    silenced). In-process via rebar._commands.compact; --threshold=0 --skip-sync,
    commit kept."""
    import contextlib
    import io

    from rebar._commands import compact as _compact

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            _compact.compact_cli([ticket_id, "--threshold=0", "--skip-sync"], repo_root=repo_root)
    except Exception:
        pass


def _completion_precheck(
    ticket_id: str,
    ticket_type: str,
    cfg_root: str,
    repo_root,
    *,
    reason: str,
    force_close: str,
):
    """The completion-verification close gate's PRE-close half (runs outside the write lock).

    Returns the manifest to **sign** on a PASS verdict, or ``None`` when the gate is off or the
    close is a ``--force-close`` (which closes WITHOUT verifying or signing — withholding the
    signed confirmation, so a closed-without-signature ticket is the durable signal that
    validation did not pass). Raises :class:`CommandError` (block) on a FAIL verdict, or when
    the LLM is unavailable / any verifier error (fail-closed). The ``rebar.llm`` import is LAZY
    so the optionality contract holds: core stays stdlib-only unless the gate is on AND a
    non-force close is attempted."""
    # session_log is lifecycle-exempt — it cannot be transitioned, so transition_core will refuse
    # this close authoritatively. Skip the gate BEFORE the (billable) verifier runs, so a doomed
    # close attempt never fires an LLM call.
    if ticket_type == "session_log":
        return None
    from rebar.config import ConfigError, load_config

    try:
        on = load_config(cfg_root).verify.require_completion_verification_for_close
    except ConfigError as exc:
        # Unreadable config: fail this gate OFF with a WARNING (not silently), and do NOT block.
        # Rationale: (1) this is an opt-in/default-off gate — an unreadable config must not
        # auto-enable it across EVERY repo (and every ticket type) and run a billable LLM on a
        # broken config; (2) the signature gate (txn._signature_gate) independently fail-CLOSES
        # for story/epic on an unreadable config, so its stronger guarantee is preserved (and
        # not preempted by this gate, which runs first/outside the lock); (3) within the trust
        # model, a missing attestation is itself the "not validated" signal CI checks. The
        # confirmed fail-CLOSED behavior still applies when the gate is readable-ON but the LLM
        # is unavailable (below).
        import sys

        print(
            f"Warning: could not read rebar config ({exc}); the completion-verification close "
            f"gate is skipped for {ticket_id} (other close gates still apply).",
            file=sys.stderr,
        )
        return None
    if not on:
        return None
    if force_close:
        return None  # close, but withhold the signed confirmation (no verify, no sign)

    # Cheap precondition BEFORE the billable LLM call: a bug close needs a valid --reason
    # (transition_core would reject it anyway). Shared predicate, so it can't drift.
    if ticket_type == "bug" and not txn.bug_close_reason_ok(reason):
        raise CommandError(
            'Error: closing a bug requires --reason starting with "Fixed:" or '
            '"Escalated to user:" (checked before running completion verification).',
            returncode=1,
        )

    try:
        from rebar import llm  # LAZY — preserves the optionality contract

        # graph=False: the close gate verifies THIS ticket's OWN completion criteria, NOT its
        # whole descendant subtree. Children are separate tickets gated on their own close; the
        # agent reads the actual code regardless of whether child ticket TEXT is inlined, so
        # graph=True would only bloat the context and make an epic close re-verify the entire
        # feature in one run (impractical — it blows the step budget). The standalone
        # `rebar verify-completion <id> --graph` remains available for a deep human review.
        result = llm.verify_completion(ticket_id, graph=False, repo_root=repo_root)
    except Exception as exc:  # missing extra/key OR any verifier failure -> fail-closed
        raise CommandError(
            f"Error: cannot close {ticket_id}: completion verification could not run ({exc}). "
            "The completion-verification gate is enabled "
            "(verify.require_completion_verification_for_close); install the 'agents' extra and "
            'set a model API key, or override with --force-close="<reason>".',
            returncode=1,
        ) from None

    if str(result.get("verdict", "")).upper() != "PASS":
        items = result.get("findings", []) or []
        lines = [
            f"  - {(f.get('criterion') or f.get('dimension') or '?')}: {f.get('detail', '')}"
            for f in items[:20]
        ]
        raise CommandError(
            f"Error: completion verification FAILED for {ticket_id} — {len(items)} unmet "
            "criteria; not closing.\n"
            + "\n".join(lines)
            + '\n  Address the criteria above, or override with --force-close="<reason>" '
            "(closes without a completion signature).",
            returncode=1,
        )
    return _verdict_manifest(result, ticket_id)


def _verdict_manifest(result: dict, ticket_id: str) -> list[str]:
    """Deterministic manifest (non-empty strings) of the verified PASS verdict, for signing.

    The signature binds ``(ticket_id, manifest)``; the key fingerprint + head_sha on the record
    provide attribution + freshness. Findings are failures-only, so a PASS has no per-criterion
    list to itemize — the minimal core IS the attestation. Deterministic (no timestamps) so
    re-signing the same verified state is reproducible."""
    return [
        "completion-verifier: PASS",
        f"ticket: {ticket_id}",
        f"model: {result.get('model') or 'n/a'}",
        f"runner: {result.get('runner') or 'n/a'}",
    ]


def transition_compute(
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    reason: str = "",
    force: bool = False,
    verdict_hash: str = "",
    force_close: str = "",
    repo_root=None,
) -> dict:
    """Validate, guard, write, and post-process a transition for an ALREADY-RESOLVED
    ticket id. Returns ``{ticket_id, from, to, newly_unblocked, noop}``. Raises
    :class:`ConcurrencyMismatch` (exit 10) / :class:`CommandError`. Does NOT parse
    ``--output`` or autodetect current — that is the CLI wrapper's job."""
    tracker = str(config.tracker_dir(repo_root))
    repo_root_str = os.path.dirname(tracker)

    _validate_status("current_status", current_status)
    if target_status == "deleted":
        raise CommandError(
            f"Error: deleted is not a valid transition target -- use ticket delete "
            f"{ticket_id} to delete a ticket",
            returncode=1,
        )
    _validate_status("target_status", target_status)

    if current_status == target_status:
        # Same-status no-op short-circuits BEFORE the authoritative guard in
        # txn.transition_core, so refuse a session_log here too — it is
        # lifecycle-exempt and must never report a (no-op) transition success.
        from rebar.reducer import reduce_ticket

        _state = reduce_ticket(os.path.join(tracker, ticket_id))
        if _state is not None and _state.get("ticket_type") == "session_log":
            raise CommandError(
                "Error: session_log tickets are lifecycle-exempt and cannot be "
                "transitioned (they are not claimed, transitioned, or closed)",
                returncode=1,
            )
        return {
            "ticket_id": ticket_id,
            "from": current_status,
            "to": target_status,
            "newly_unblocked": [],
            "noop": True,
        }

    # Ghost check (ticket dir exists + has a CREATE/SNAPSHOT event).
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        raise CommandError(f"Error: ticket '{ticket_id}' does not exist", returncode=1)
    if not any(
        (n.endswith("-CREATE.json") or n.endswith("-SNAPSHOT.json")) and not n.startswith(".")
        for n in os.listdir(ticket_dir)
    ):
        raise CommandError(
            f"Error: ticket {ticket_id} has no CREATE or SNAPSHOT event", returncode=1
        )

    if not os.path.isfile(os.path.join(tracker, ".env-id")):
        raise CommandError(
            "Error: ticket system not initialized. Run 'ticket init' first.", returncode=1
        )

    # Open-children guard + newly_unblocked (one batch pass), only on close.
    newly_unblocked: list[str] = []
    if target_status == "closed":
        batch = batch_close_operations(ticket_ids=[ticket_id], tracker_dir=tracker)
        open_children = batch["open_children"]
        newly_unblocked = batch["newly_unblocked"]
        if open_children:
            count = len(open_children)
            if force:
                sys.stderr.write(
                    f"Warning: closing ticket '{ticket_id}' with {count} unresolved "
                    "(non-closed) child ticket(s) (--force).\n"
                    "The following children are not yet closed:\n" + "\n".join(open_children) + "\n"
                )
            else:
                raise CommandError(
                    f"Error: cannot close ticket '{ticket_id}' while it has {count} "
                    "unresolved (non-closed) child ticket(s).\n"
                    "Close the following children first, or use --force to close the "
                    "parent with children unresolved:\n" + "\n".join(open_children),
                    returncode=1,
                )

    # Completion-verification close gate (opt-in; runs OUTSIDE the write lock since an LLM
    # call must not serialize all writes). Ordering is verify -> close -> sign: the precheck
    # runs the verifier and blocks (fail-closed) on FAIL / unavailable-LLM; on PASS it returns
    # the manifest to sign AFTER a confirmed close (so a failed/raced close never leaves an
    # orphan "certified" signature on an unclosed ticket). force_close skips both.
    verified_manifest = None
    if target_status == "closed":
        from rebar.reducer import reduce_ticket as _reduce

        ticket_type = (_reduce(os.path.join(tracker, ticket_id)) or {}).get("ticket_type", "")
        verified_manifest = _completion_precheck(
            ticket_id, ticket_type, repo_root_str, repo_root, reason=reason, force_close=force_close
        )

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")

    # Locked write (exit 10 on optimistic-concurrency mismatch).
    txn.transition_core(
        tracker,
        ticket_id,
        current_status,
        target_status,
        env_id=env_id,
        author=author,
        close_reason=reason,
        verdict_hash=verdict_hash,
        force_close_reason=force_close,
    )

    # PASS attestation: sign the verified verdict AFTER the close is confirmed. A crash in this
    # (two-local-commit) window leaves closed-without-signature — the conservative direction
    # (reads as "bypassed", never a false "validated"). Errors surface: we WANT a hard signal if
    # the trustworthy record can't be written.
    if target_status == "closed" and verified_manifest is not None:
        from rebar import signing as _signing

        _signing.sign_manifest(ticket_id, verified_manifest, repo_root=repo_root)

    # Force-close audit comment (best-effort, silenced — matches bash || true).
    if target_status == "closed" and force_close:
        session = os.environ.get("SESSION_ID") or _short_head(tracker) or "unknown"
        body = (
            "FORCE_CLOSE: close gate(s) bypassed by user approval — no completion/signature "
            f'attestation was signed. Reason: "{force_close}". Session: {session}.'
        )
        try:
            from rebar._commands import leaf

            leaf.comment(ticket_id, body, repo_root=repo_root)
        except Exception:
            pass

    if target_status == "closed":
        _compact_on_close(repo_root_str, ticket_id)
        scratch.cleanup_for_ticket(repo_root_str, ticket_id)

    # The STATUS (and compact-on-close SNAPSHOT) commits are now in the local
    # tickets branch but unpushed — txn.transition_core commits inline and does not
    # go through write_and_push. Trigger the same best-effort push so a trailing
    # transition (the last write of a session) isn't stranded (bug prone-octet-cheek).
    from rebar._store import push

    push.push_after_commit(tracker)

    return {
        "ticket_id": ticket_id,
        "from": current_status,
        "to": target_status,
        "newly_unblocked": newly_unblocked,
        "noop": False,
    }


def _short_head(tracker: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _unarchive(ticket_id: str, target_status: str, tracker: str, repo_root_str: str) -> int:
    """The ``archived → open`` un-archive seam: REVERT the latest live ARCHIVED
    event IN-PROCESS via :func:`rebar._commands.composer.revert_core` (Tier E
    E6.5a — replacing the ticket-revert.sh subprocess). Same REVERT event +
    ``.archived`` marker clear + ``Reverted event …`` confirmation the bash
    ``exec`` produced; ``--output`` and the UNBLOCKED block are skipped here."""
    if target_status != "open":
        sys.stderr.write(
            "Error: from 'archived' the only valid transition is to 'open' "
            f"(un-archive). Use: ticket transition {ticket_id} archived open\n"
        )
        return 1
    archived_uuid = _latest_live_archived_uuid(os.path.join(tracker, ticket_id))
    if not archived_uuid:
        sys.stderr.write("Error: no live ARCHIVED event (status may be stale)\n")
        return 1
    from rebar._commands._seam import CommandError
    from rebar._commands.composer import revert_core

    try:
        resolved = revert_core(
            ticket_id,
            archived_uuid,
            "un-archive via transition archived open",
            repo_root=repo_root_str,
        )
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    sys.stdout.write(f"Reverted event '{archived_uuid}' on ticket '{resolved}'\n")
    return 0


def _latest_live_archived_uuid(ticket_dir: str) -> str:
    """UUID of the most recent ARCHIVED event not undone by a REVERT (mirrors the
    inline heredoc in ticket-transition.sh)."""
    archived: dict[str, int] = {}
    reverted: set[str] = set()
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return ""
    for fname in names:
        if fname.startswith(".") or not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(ticket_dir, fname), encoding="utf-8") as f:
                ev = json.load(f)
        except Exception:
            continue
        et = ev.get("event_type")
        if et == "ARCHIVED":
            archived[ev.get("uuid", "")] = ev.get("timestamp", 0)
        elif et == "REVERT":
            t = ev.get("data", {}).get("target_event_uuid", "")
            if t:
                reverted.add(t)
    live = [(ts, u) for u, ts in archived.items() if u and u not in reverted]
    return max(live)[1] if live else ""


def _resolve_id_or_report(raw_id: str, tracker: str, fmt: str) -> str | None:
    """Resolve a ticket id for a ``*_cli`` handler; on failure emit the standard
    ``ticket_not_found`` envelope (when ``fmt == "json"``) plus the stderr line, and
    return ``None`` so the caller returns exit 1. Shared by ``transition_cli`` and
    ``claim_cli`` (the identical resolve-id-or-emit block was inlined in both)."""
    ticket_id = resolve_ticket_id(raw_id, tracker)
    if ticket_id is None:
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope("ticket_not_found", raw_id, f"Ticket '{raw_id}' not found", 1)
                )
                + "\n"
            )
        sys.stderr.write(f"Error: ticket '{raw_id}' not found\n")
    return ticket_id


def transition_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar transition`` entry: parse ``--output``, autodetect/validate, run the
    un-archive seam or :func:`transition_compute`, print, return the exit code."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    if len(rest) < 1:
        return _usage()

    raw_id = rest[0]
    tracker = str(config.tracker_dir(repo_root))
    ticket_id = _resolve_id_or_report(raw_id, tracker, fmt)
    if ticket_id is None:
        return 1

    tail = rest[1:]
    if len(tail) < 1:
        return _usage()

    if len(tail) == 1:
        current_status = _read_status(tracker, ticket_id)
        if current_status is None:
            sys.stderr.write(
                f"Error: could not read current status for ticket '{ticket_id}'. "
                "Provide current_status explicitly.\n"
            )
            return _usage()
        target_status = tail[0]
        flag_args: list[str] = []
    else:
        current_status = tail[0]
        target_status = tail[1]
        flag_args = tail[2:]

    # Un-archive seam — before status validation (archived is not a valid status).
    if current_status == "archived":
        return _unarchive(ticket_id, target_status, tracker, os.path.dirname(tracker))

    try:
        reason, force, verdict_hash, force_close = _parse_flags(flag_args)
        result = transition_compute(
            ticket_id,
            current_status,
            target_status,
            reason=reason,
            force=force,
            verdict_hash=verdict_hash,
            force_close=force_close,
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        return 10
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode

    if result["noop"]:
        sys.stdout.write("No transition needed\n")
        return 0

    if fmt == "json":
        sys.stdout.write(
            json.dumps(
                {
                    "ticket_id": ticket_id,
                    "from": current_status,
                    "to": target_status,
                    "newly_unblocked": result["newly_unblocked"],
                }
            )
            + "\n"
        )
    elif target_status == "closed":
        ids = result["newly_unblocked"]
        sys.stdout.write(f"UNBLOCKED: {','.join(ids) if ids else 'none'}\n")
    return 0


_CLAIM_USAGE = (
    "Usage: ticket claim <ticket_id> [--assignee=<name>]\n"
    "  Claims an OPEN ticket (-> in_progress) and sets its assignee atomically.\n"
    "  Exits 10 if the ticket is not open (someone else already claimed it).\n"
)


def _parse_assignee(args: list[str]) -> str:
    """Parse [--assignee[=]<name>] from claim's args (other tokens skipped),
    mirroring ticket-claim.sh. Raises :class:`CommandError` on a value-less flag."""
    assignee = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--assignee="):
            assignee = a[len("--assignee=") :]
            i += 1
        elif a == "--assignee":
            if i + 1 >= len(args):
                raise CommandError("Error: --assignee requires a value", returncode=1)
            assignee = args[i + 1]
            i += 2
        else:
            i += 1
    return assignee


def claim_compute(ticket_id: str, *, assignee: str = "", repo_root=None) -> dict:
    """Claim an ALREADY-RESOLVED ticket (ghost/init checks + the locked claim core).
    Returns ``{ticket_id, status, assignee}``; raises :class:`ConcurrencyMismatch`
    (exit 10) / :class:`CommandError`."""
    tracker = str(config.tracker_dir(repo_root))
    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        raise CommandError(f"Error: ticket '{ticket_id}' does not exist", returncode=1)
    if not any(
        (n.endswith("-CREATE.json") or n.endswith("-SNAPSHOT.json")) and not n.startswith(".")
        for n in os.listdir(ticket_dir)
    ):
        raise CommandError(
            f"Error: ticket {ticket_id} has no CREATE or SNAPSHOT event", returncode=1
        )
    if not os.path.isfile(os.path.join(tracker, ".env-id")):
        raise CommandError(
            "Error: ticket system not initialized. Run 'ticket init' first.", returncode=1
        )

    from rebar._commands import _seam

    env_id = _seam.env_id(config.tracker_dir(repo_root))
    author = _seam.author("Unknown")
    txn.claim_core(tracker, ticket_id, env_id=env_id, author=author, assignee=assignee)
    # claim_core commits inline (not via write_and_push); push best-effort so a
    # claim that isn't followed by an append_event write still reaches origin.
    from rebar._store import push

    push.push_after_commit(tracker)
    return {"ticket_id": ticket_id, "status": "in_progress", "assignee": assignee or None}


def claim_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar claim`` entry: parse ``--output`` / ``--assignee``, resolve, run the
    locked claim core, and emit the dispatcher-identical CLAIMED / error-envelope
    output."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        sys.stderr.write(_CLAIM_USAGE)
        return 1

    raw_id = rest[0]
    try:
        assignee = _parse_assignee(rest[1:])
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode

    tracker = str(config.tracker_dir(repo_root))
    ticket_id = _resolve_id_or_report(raw_id, tracker, fmt)
    if ticket_id is None:
        return 1

    try:
        claim_compute(ticket_id, assignee=assignee, repo_root=repo_root)
    except ConcurrencyMismatch as exc:
        sys.stderr.write(exc.message + "\n")
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope(
                        "concurrency_conflict",
                        raw_id,
                        f"Ticket '{ticket_id}' is not open (already claimed)",
                        10,
                    )
                )
                + "\n"
            )
        return 10
    except CommandError as exc:
        sys.stderr.write(exc.message + "\n")
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope(
                        "claim_failed", raw_id, f"Failed to claim ticket '{ticket_id}'", 1
                    )
                )
                + "\n"
            )
        return 1

    if fmt == "json":
        sys.stdout.write(
            json.dumps(
                {"ticket_id": ticket_id, "status": "in_progress", "assignee": assignee or None}
            )
            + "\n"
        )
    else:
        suffix = f" (assignee: {assignee})" if assignee else ""
        sys.stdout.write(f"CLAIMED: {ticket_id}{suffix}\n")
    return 0


def reopen_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar reopen <id>`` → ``transition <id> closed open`` (uses only the first
    positional, like the dispatcher arm)."""
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        from rebar._cli import _help

        text = _help.subcommand_help("reopen")
        if text:
            sys.stderr.write(text)
        return 1
    out_flag = ["--output", fmt] if fmt != "text" else []
    return transition_cli([rest[0], "closed", "open", *out_flag], repo_root=repo_root)
