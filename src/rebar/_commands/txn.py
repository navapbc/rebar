"""Locked in-process cores for status transitions and atomic claims.

Each operation holds the shared write lock through fresh-state validation, canonical
append-only event writes, staging, and commit. Splitting the commit would reopen a
lost-update race. The cores raise :class:`ConcurrencyMismatch` for status races and
:class:`CommandError` for other failures, leaving channel rendering to callers.
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
from rebar._store.event_commit_git import run_auto_maintenance
from rebar._store.gitutil import _AUTOMAINT_OFF, run_git_write
from rebar.reducer import reduce_ticket
from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES
from rebar.reducer._sort import prefix_ts as _prefix_ts


class ConcurrencyMismatch(CommandError):
    """Optimistic-concurrency rejection (exit 10): the ticket's actual status no
    longer matches the caller's expectation, or a claim target is not ``open``."""

    def __init__(self, message: str) -> None:
        super().__init__(message, returncode=10)


def _stamp_session(status_data: dict) -> None:
    """Stamp available claim-session identifiers into a STATUS event.

    Each opaque session, harness, or remote-session value is added only when present,
    preserving legacy bytes when no provenance resolves."""
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
    """Acquire the cross-platform write lock for the complete transaction.

    Each acquisition pass has a 30-second budget. Configured retries add further passes so
    claims tolerate ordinary contention. Propagate
    :class:`~rebar._store.lock.LockTimeout` details, including wait and holder, through
    :class:`CommandError`."""
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
    """Run a tracker git command and raise exit-2 :class:`CommandError` on failure.

    :func:`run_git_write` recovers stale index locks and retries contention with another
    index writer. Read-only commands do not trigger that retry."""
    cp = run_git_write(tracker_dir, *args, check=False)
    if cp.returncode != 0:
        raise CommandError(f"Error: git operation failed: {cp.stderr}", returncode=2)


# raw-git-ok: locked store seam internal
def _git_commit(tracker_dir: str, message: str) -> None:
    """Commit staged events with automatic maintenance disabled, then run it separately.

    This keeps the commit bounded while maintenance remains serialized under the caller's
    write lock. Since the commit has succeeded, deferred maintenance is best effort."""
    _git(tracker_dir, *_AUTOMAINT_OFF, "commit", "-q", "--no-verify", "-m", message)
    run_auto_maintenance(tracker_dir)


# raw-git-ok: locked store seam internal
def _unstage(tracker_dir: str, *abs_paths: str | None) -> None:
    """Best-effort removal of failed event paths from the index before file cleanup.

    This prevents a later commit from sweeping in an orphaned staged event. The caller holds
    the write lock. Cleanup never raises."""
    rels = [os.path.relpath(p, tracker_dir) for p in abs_paths if p]
    if not rels:
        return
    try:
        subprocess.run(
            ["git", "-C", tracker_dir, "reset", "-q", "--", *rels],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        pass


# Closed bugs require a bounded classification. Non-bugs may use the administrative subset.
# Keep this schema-ordered vocabulary as the shared source for the CLI, write guard, and
# completion precheck.
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
    """Return the refusal for an invalid close disposition, otherwise ``None``.

    The write guard and completion precheck share rules independent of gates. Bugs require a
    known class. Non-bugs accept only administrative classes. Reason-required classes need a
    close reason or force note. ``not_a_bug`` and ``escalated`` may instead use a usable
    replacement link. Missing lookup context requires the reason. ``idea -> closed`` is a
    reject or drop operation and bypasses these disposition rules."""
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
    """Add present-only close metadata to ``* -> closed`` STATUS data.

    Omitting absent keys preserves legacy event bytes. ``close_class`` records classification.
    ``close_reason`` is retained only for reason-required dispositions.
    ``force_close_reason`` audits a bypass under its durable schema name.
    ``completion_expectation`` records why a signature was or was not expected, not the later
    signing outcome."""
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


def prepare_transition_event_locked(
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
    timestamp: int | None = None,
    event_uuid: str | None = None,
) -> dict[str, Any]:
    """Re-read, validate, and compose a STATUS event while the caller holds the write lock."""
    state = reduce_ticket(os.path.join(tracker_dir, ticket_id))
    if state is None:
        raise CommandError(
            "Error: reducer returned no state (ticket may be corrupt or missing events)",
            returncode=1,
        )
    ticket_type = str(state.get("ticket_type", ""))
    if ticket_type in _NON_GRAPH_ARTIFACT_TYPES:
        raise CommandError(
            f"Error: {ticket_type} tickets are lifecycle-exempt and cannot be "
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
            f'Error: current status is "{actual_status}", not "{current_status}". Re-run: {hint}'
        )
    refusal = close_class_refusal(
        ticket_type,
        close_class,
        close_reason=close_reason,
        force_close_reason=force_reason,
        target_status=target_status,
        from_idea=current_status == "idea",
        ticket_id=ticket_id,
        tracker=tracker_dir,
    )
    if refusal:
        raise CommandError(f"Error: {refusal}", returncode=1)
    ticket_dir_path = os.path.join(tracker_dir, ticket_id)
    parent_status_uuid = _parent_status_uuid(ticket_dir_path)
    status_data = {
        "status": target_status,
        "current_status": current_status,
        "parent_status_uuid": parent_status_uuid,
    }
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
        "timestamp": timestamp if timestamp is not None else hlc.next_tick(tracker_dir, ticket_id),
        "uuid": event_uuid or str(uuid.uuid4()),
        "event_type": "STATUS",
        "env_id": env_id,
        "author": author,
        "parent_status_uuid": parent_status_uuid,
        "data": status_data,
    }
    if pre_status_check is not None:
        pre_status_check(state)
    finalize_event(event, ticket_id, "STATUS", status_data, tracker_dir, repo_root)
    return event


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
    """Atomically append and commit a STATUS event under the write lock.

    Fresh state must match ``current_status``. A mismatch raises
    :class:`ConcurrencyMismatch`. The function enforces close-disposition rules, runs an
    optional ``pre_status_check`` on locked state, and raises :class:`CommandError` for
    validation or git failures."""
    handle = _acquire_write_lock(tracker_dir)
    final_path = None
    try:
        event = prepare_transition_event_locked(
            tracker_dir,
            ticket_id,
            current_status,
            target_status,
            env_id=env_id,
            author=author,
            close_class=close_class,
            close_reason=close_reason,
            force_reason=force_reason,
            completion_expectation=completion_expectation,
            repo_root=repo_root,
            pre_status_check=pre_status_check,
        )
        final_filename = event_append.event_filename(event["timestamp"], event["uuid"], "STATUS")
        ticket_dir_path = os.path.join(tracker_dir, ticket_id)
        final_path = os.path.join(ticket_dir_path, final_filename)
        fsutil.atomic_write(final_path, canonical_str(event), encoding="utf-8")

        _git(tracker_dir, "add", f"{ticket_id}/{final_filename}")
        _git_commit(tracker_dir, f"ticket: STATUS {ticket_id}")
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
    """Atomically move an open ticket to ``in_progress`` and optionally assign it.

    The STATUS event is always written. A supplied assignee adds a later EDIT event. Every
    event produced by the claim commits together before the lock releases. A current state
    other than ``open`` raises :class:`ConcurrencyMismatch`. When assignment is requested, no
    reader observes it without the status change."""
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
        _git_commit(tracker_dir, f"ticket: CLAIM {ticket_id}")
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


def ensure_ac_boxes_checked(
    ticket_id: str, tracker: str, *, ticket_state: Mapping[str, object] | None = None
) -> None:
    """Fail the close (CommandError, exit 1) when unchecked ``- [ ]`` AC items remain.

    Deterministic, pre-LLM. Items whose text begins with ``[non-codebase]`` (the
    shared ADR-0043 tag) are exempt. Silently returns on any read / reduce failure so an
    unreadable ticket is never blocked here (other guards own that)."""
    try:
        state = (
            ticket_state
            if ticket_state is not None
            else reduce_ticket(os.path.join(tracker, ticket_id))
        )
        if not isinstance(state, dict):
            return
        description = str(state.get("description", "") or "")
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


def ensure_attested_items_valid(
    ticket_id: str, tracker: str, *, ticket_state: Mapping[str, object] | None = None
) -> None:
    """Reject invalid ``[non-codebase]`` acceptance criteria before completion review.

    Repository citations make a tagged criterion an attestation-laundering finding, so the
    criterion must be untagged. Otherwise the tag requires a complete ``provenance:``
    continuation. The check reports laundering first. Read or reduction failures return
    without error because other close guards own ticket readability. Force bypass occurs
    upstream."""
    try:
        state = (
            ticket_state
            if ticket_state is not None
            else reduce_ticket(os.path.join(tracker, ticket_id))
        )
        if not isinstance(state, dict):
            return
        description = str(state.get("description", "") or "")
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
