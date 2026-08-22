"""The status-transition + claim locked critical section, in-process.

This module IS the lock-holding, committing core for a status transition and for
an atomic claim (history: the bash-era ``_engine/ticket_txn.py`` heredoc extraction,
relocated here; see ``docs/bash-migration.md`` §7).

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
from collections.abc import Callable, Mapping
from typing import Any

from rebar._commands._seam import CommandError, finalize_event
from rebar._store import compat, event_append, fsutil, hlc, lock
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
    """Acquire the unified write lock (fcntl + mkdir dual leg) for a txn critical
    section — mutually exclusive with every other writer on every platform class (the
    ``stiff-mop-lane`` fix). Held across the whole re-read → write → commit section.

    One pass is the historical 30s (``attempts=1``); ``retries`` extra passes follow so
    the most CONTENDED verb stops being the first to lose its write — measured, a `claim`
    behind a 45s holder died at exactly 30.30s while comments beside it survived
    (royal-weariless-zebrafish). The :class:`~rebar._store.lock.LockTimeout` message is
    PROPAGATED rather than replaced: the old generic string discarded both the cumulative
    wait and the holder, leaving a starved caller unable to say what blocked it."""
    try:
        return lock.acquire(
            tracker_dir,
            timeout=30,
            attempts=1,
            dual_window=True,
            retries=lock.write_path_retries(),
        )
    except lock.LockTimeout as exc:
        raise CommandError(f"Error: could not acquire lock — {exc}", returncode=1) from None
    except compat.StoreIncompatibleError as exc:
        # Story 21dd: the acquire() gate fails closed on an incompatible store — surface
        # it as a non-zero CommandError so the txn critical section never runs.
        raise CommandError(str(exc), returncode=getattr(exc, "returncode", 1)) from None


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


# raw-git-ok: locked store seam internal
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


# raw-git-ok: locked store seam internal
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


# The closed-ticket close classification vocabulary (ticket ed13; widened by ticket fc20): a
# bug close records a REQUIRED, bounded ``--class <value>`` (replacing the old free-text
# ``--reason``), and a non-bug close MAY record one from the ADMINISTRATIVE subset — both
# folded into reduced state as ``close_class``. Single-sourced here so the CLI parser, the
# close guard, and the completion-gate pre-check all validate against the SAME list — kept in
# the same order as common.schema.json#/$defs/close_class.
CLOSE_CLASSES: tuple[str, ...] = (
    "regression",
    "plan_defect",
    "env_integration",
    "flaky",
    "preexisting",
    "not_a_bug",
    "duplicate",
    "escalated",
    "obsolete",
    "superseded",
    "wontfix",
    "undetermined",
)


def bug_close_class_ok(close_class: str) -> bool:
    """True if a bug-close ``--class`` value is one of the bounded vocabulary
    (:data:`CLOSE_CLASSES`). Shared by :func:`transition_core`'s close guard and the
    completion gate's pre-check so the two cannot drift. Empty / unknown → False."""
    return close_class in CLOSE_CLASSES


def close_class_refusal(
    ticket_type: str,
    close_class: str,
    *,
    close_reason: str = "",
    force_close_reason: str = "",
    target_status: str = "closed",
    from_idea: bool = False,
    ticket_id: str = "",
    tracker: str = "",
) -> str | None:
    """The refusal message for an invalid close-class combination, or ``None`` when valid.

    Shared by :func:`transition_core` (the authoritative, gate-independent write-side guard —
    neither ``--force`` nor a disabled completion gate skips it) and the completion gate's
    cheap pre-LLM check in ``close_precheck`` so the two cannot drift. Three rules (tickets
    ed13 + fc20 + bug d54b): a bug close REQUIRES a class from the full vocabulary; a non-bug
    close MAY carry one only from ``close_disposition.ADMINISTRATIVE_CLASSES`` (``not_a_bug`` /
    ``escalated`` stay bug-only); and a reason-required class REQUIRES a reason — the CLI
    ``--reason`` text (persisted as ``close_reason``), the ``--force=<reason>`` bypass note when
    the close is forced, or (``not_a_bug``/``escalated`` only) a live replacement link, checked
    when the caller passes ``ticket_id``+``tracker`` (:func:`close_disposition.reason_refusal`;
    omitting them fails toward requiring the reason). ``idea -> closed`` is a reject/drop, not a
    completion, and bypasses all three."""
    if target_status != "closed" or from_idea:
        return None
    from rebar._commands import close_disposition

    if ticket_type == "bug":
        if not bug_close_class_ok(close_class):
            allowed = ", ".join(CLOSE_CLASSES)
            return f"closing a bug ticket requires --class <value> — one of: {allowed}"
    elif close_class and close_class not in close_disposition.ADMINISTRATIVE_CLASSES:
        allowed = ", ".join(
            c for c in CLOSE_CLASSES if c in close_disposition.ADMINISTRATIVE_CLASSES
        )
        return (
            f"closing a {ticket_type} accepts --class only for an administrative "
            f"disposition — one of: {allowed} ('{close_class}' is bug-only or unknown)"
        )
    if close_class in close_disposition.REASON_REQUIRED_CLASSES and not (
        close_reason or force_close_reason
    ):
        return close_disposition.reason_refusal(close_class, ticket_id, tracker)
    return None


def _stamp_close_metadata(
    status_data: dict,
    target_status: str,
    *,
    close_class: str,
    close_reason: str,
    force_reason: str,
    completion_expectation: str,
) -> None:
    """Stamp the close-metadata keys on a ``*->closed`` STATUS event's ``data``.

    All present-only (mirrors ``_stamp_session``): absent -> key omitted -> byte-identical
    to the pre-feature close event, which is what keeps every addition here
    backward-compatible (a legacy event without a key reduces with the key ABSENT — unknown,
    never guessed).

    * ``close_class`` — bug-close classification (ticket ed13): the validated ``--class``,
      folded by the reducer into ``state["close_class"]``.
    * ``close_reason`` — the operator's justification for a reason-only administrative close
      (ticket fc20): the CLI ``--reason`` on a NON-force administrative close. DISTINCT from
      ``force_close_reason`` — this key records why a truthful disposition closed, that one
      records why a gate was bypassed. Persisted ONLY for a reason-required class: any other
      close discards the value rather than smuggling a free-text rationale past the bounded
      vocabulary (ticket 3803's honesty rule, enforced write-side).
    * ``force_close_reason`` — the operator's ``--force=<reason>`` for bypassing the close
      gates (the unified ``force_reason`` in memory — ticket blusterous-earthly-kitten). The
      PERSISTED key stays ``force_close_reason``: it is durable reduced state (the reducer +
      schema fold it), so renaming it would be an out-of-scope event-schema migration. This
      parameter was previously accepted and DISCARDED (bug defiant-orthoclase-buck): the only
      durable trace of a bypass reason was a best-effort FORCE_CLOSE audit comment written
      afterwards via a SECOND lock acquisition and swallowed on failure — least reliable under
      exactly the contention that makes force-closing attractive. Recording it here costs no
      extra lock: this write already holds one.
    * ``completion_expectation`` — WHY a completion signature was or was not expected for
      this close (story mechanical-coherent-wolverine): the write-time provenance that lets a
      later reader distinguish gate-genuinely-off from force-bypassed, unreadable-config
      fail-open, local-source, certification-withheld, and signature-append failure. Records
      the EXPECTATION, never the outcome — the STATUS commits before signing is attempted, so
      ``required`` + no attestation reads as "a signature was expected and is missing".
    """
    if target_status != "closed":
        return
    if close_class:
        status_data["close_class"] = close_class
    from rebar._commands import close_disposition

    if close_reason and close_class in close_disposition.REASON_REQUIRED_CLASSES:
        status_data["close_reason"] = close_reason
    if force_reason:
        status_data["force_close_reason"] = force_reason
    if completion_expectation:
        status_data["completion_expectation"] = completion_expectation


# raw-git-ok: locked store seam internal
def transition_core(
    tracker_dir: str,
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    env_id: str,
    author: str,
    close_class: str = "",
    close_reason: str = "",
    force_reason: str = "",
    completion_expectation: str = "",
    repo_root=None,
    pre_status_check: Callable[[Mapping[str, Any]], None] | None = None,
) -> None:
    """Write the append-only STATUS(``target_status``) event under the write lock.

    Re-reads the ticket under the lock and rejects with :class:`ConcurrencyMismatch`
    (exit 10) if its status is not ``current_status``. Applies the bug-close-reason
    guard. ``pre_status_check`` is an optional local caller policy invoked with the
    freshly reduced locked state immediately before the STATUS is appended. Raises
    :class:`CommandError` for validation / git failures. Returns ``None`` on success
    (the wrapper computes newly_unblocked + output separately)."""
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
        # nothing built to verify or attest, so it bypasses the bug-close-reason guard
        # below (mirrors the completion-precheck bypass in transition_close.close_ticket).
        # The open-children structural guard is enforced elsewhere and is NOT relaxed for idea.
        from_idea = current_status == "idea"

        # Close-class guard (tickets ed13 + fc20 + bug d54b): a bug closing from a non-idea
        # status REQUIRES a bounded ``--class <value>``; a non-bug close MAY carry one only from
        # the ADMINISTRATIVE subset; a reason-required class (obsolete/wontfix, and — unless a
        # live replacement link exists — not_a_bug/escalated) REQUIRES a reason. The shared rule
        # (:func:`close_class_refusal`) is also run by the completion gate's pre-check so the
        # two cannot drift, but THIS is the authoritative, gate-independent enforcement point:
        # it runs on every close, forced or not, gate on or off. On success ``close_class``
        # (and ``close_reason``) are folded onto the ``*->closed`` STATUS edge below
        # (present-only, mirroring ``_stamp_session``).
        refusal = close_class_refusal(
            ticket_type,
            close_class,
            close_reason=close_reason,
            force_close_reason=force_reason,
            target_status=target_status,
            from_idea=from_idea,
            ticket_id=ticket_id,
            tracker=tracker_dir,
        )
        if refusal:
            raise CommandError(f"Error: {refusal}", returncode=1)

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
        _stamp_close_metadata(
            status_data,
            target_status,
            close_class=close_class,
            close_reason=close_reason,
            force_reason=force_reason,
            completion_expectation=completion_expectation,
        )
        event = {
            "timestamp": timestamp,
            "uuid": event_uuid,
            "event_type": "STATUS",
            "env_id": env_id,
            "author": author,
            "parent_status_uuid": parent_status_uuid,
            "data": status_data,
        }
        if pre_status_check is not None:
            pre_status_check(state)
        # Attribution + write-time signing / the opt-in write-gate via the SHARED finalize seam
        # (bug 0ba4) — the SAME signing path append_event uses, so a transition/close STATUS is
        # signed identically to a CREATE/COMMENT. Placed BEFORE `final_path` is assigned so a
        # require_authenticated refusal (CommandError) leaves nothing on disk to roll back.
        finalize_event(event, ticket_id, "STATUS", status_data, tracker_dir, repo_root)

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


# raw-git-ok: locked store seam internal
def claim_core(
    tracker_dir: str,
    ticket_id: str,
    *,
    env_id: str,
    author: str,
    assignee: str = "",
    repo_root=None,
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
        # Attribution + write-time signing / write-gate via the SHARED finalize seam (bug 0ba4)
        # — see transition_core. BEFORE `status_path`/`edit_path` are assigned so a
        # require_authenticated refusal (CommandError) rolls back cleanly (both paths still None).
        finalize_event(status_event, ticket_id, "STATUS", status_data, tracker_dir, repo_root)
        status_filename = event_append.event_filename(ts1, uuid1, "STATUS")
        status_path = os.path.join(ticket_dir_path, status_filename)
        fsutil.atomic_write(status_path, canonical_str(status_event), encoding="utf-8")
        rel_paths.append(f"{ticket_id}/{status_filename}")

        # EDIT(assignee) — only when supplied. ts2 ticked AFTER ts1 so STATUS sorts
        # before EDIT in replay (the HLC +1 floor makes ts2 > ts1 strictly).
        if assignee:
            ts2 = hlc.next_tick(tracker_dir, ticket_id)
            uuid2 = str(uuid.uuid4())
            edit_data = {"fields": {"assignee": assignee}}
            edit_event = {
                "timestamp": ts2,
                "uuid": uuid2,
                "event_type": "EDIT",
                "env_id": env_id,
                "author": author,
                "data": edit_data,
            }
            finalize_event(edit_event, ticket_id, "EDIT", edit_data, tracker_dir, repo_root)
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


def ensure_ac_boxes_checked(ticket_id: str, tracker: str) -> None:
    """Fail the close (CommandError, exit 1) when unchecked ``- [ ]`` AC items remain.

    Deterministic, pre-LLM. Items whose text begins with ``[non-codebase]`` (the
    shared ADR-0043 tag) are exempt. Silently returns on any read / reduce failure so an
    unreadable ticket is never blocked here (other guards own that)."""
    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
        if not isinstance(state, dict):
            return
        description = state.get("description", "") or ""
    except Exception:  # noqa: BLE001
        return

    from rebar._plan_clarity import evaluate_plan_clarity
    from rebar.llm.plan_review.det_operator_attested import _OPERATOR_ATTESTED_TAG_RE

    floor = evaluate_plan_clarity(description)
    offenders = [ln for ln in floor.unchecked_ac_lines if not _OPERATOR_ATTESTED_TAG_RE.match(ln)]
    if not offenders:
        return

    items_fmt = "\n".join(f"  {ln}" for ln in offenders)
    raise CommandError(
        f"Error: ticket {ticket_id} has unchecked Acceptance Criteria items:\n"
        f"{items_fmt}\n"
        "Resolve each item before closing, then check the box (edit the description).\n"
        "Items with done-evidence outside the snapshot may be tagged [non-codebase] "
        "to exempt them from this check.\n"
        'To override: --force="<reason>"',
        returncode=1,
    )


def ensure_attested_items_valid(ticket_id: str, tracker: str) -> None:
    """Fail the close (CommandError, exit 1) on an invalid ``[non-codebase]`` AC item.

    Deterministic, pre-LLM (bug 2f56-313f-6175-41b1). The completion verifier classifies
    criteria SOLELY from the author tag (ADR-0043) — by design it never second-guesses
    ``[non-codebase]`` — so a LAUNDERED tag (a code-verifiable criterion tagged to dodge
    repository verification) must be rejected HERE, before the tag buys anything. Two checks,
    in remedy order:

    1. **Laundering** — a tagged item whose own text (or continuation lines) cites exact repo
       path/symbol evidence. Its remedy is UNTAG (the verifier can check the repository), so
       it is reported first — never coached into decorating the mistag with provenance.
    2. **Provenance shape** — a tagged item missing its complete ``provenance:`` continuation
       line (ADR-0043 x ADR-0016; the same detector the advisory review-side P6 lint uses,
       promoted to blocking on the close path).

    Silently returns on any read / reduce failure so an unreadable ticket is never blocked
    here (other guards own that). ``--force`` bypasses it upstream, like every close precheck."""
    try:
        state = reduce_ticket(os.path.join(tracker, ticket_id))
        if not isinstance(state, dict):
            return
        description = state.get("description", "") or ""
    except Exception:  # noqa: BLE001
        return

    from rebar.llm.plan_review import det_attestation_launder, det_measurement_provenance

    laundered = det_attestation_launder.laundering_gaps(description)
    if laundered:
        items_fmt = "\n".join(
            f"  {line.strip()}\n    cites: {', '.join(cites)}" for line, cites in laundered
        )
        raise CommandError(
            f"Error: ticket {ticket_id} has [non-codebase] Acceptance Criteria items "
            f"whose evidence is repository-resident (attestation laundering):\n{items_fmt}\n"
            "A criterion proved by exact repo paths/symbols is code-verifiable: remove the "
            "[non-codebase] tag and let the completion verifier check the repository.\n"
            "If the criterion MIXES repository and external evidence, SPLIT it: move the "
            "repo-verifiable half (the cited paths/symbols above) to a new UNTAGGED "
            "criterion, and keep the external outcome tagged with its provenance: line.\n"
            "Note: this description edit stales a signed plan-review attestation (material "
            "change) — expect to re-run 'rebar review-plan' before re-claiming or closing.\n"
            'To override: --force="<reason>"',
            returncode=1,
        )

    provenance_gaps = det_measurement_provenance.provenance_gaps(description)
    if provenance_gaps:
        items_fmt = "\n".join(f"  {line.strip()}" for line, _ in provenance_gaps)
        raise CommandError(
            f"Error: ticket {ticket_id} has [non-codebase] Acceptance Criteria items "
            f"without a complete 'provenance:' continuation line:\n{items_fmt}\n"
            "Each tagged item needs an indented continuation line under its checkbox:\n"
            "  provenance: environment=<v>; principal=<v>; "
            "privilege_posture=<production-equivalent|broader|narrower>; "
            "instrument=<live-call|simulation|static-analysis> — <justification>\n"
            "Note: this description edit stales a signed plan-review attestation (material "
            "change) — expect to re-run 'rebar review-plan' before re-claiming or closing.\n"
            'To override: --force="<reason>"',
            returncode=1,
        )
