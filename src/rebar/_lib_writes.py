"""Library wrappers for ticket genesis and optimistic status transitions.

The package facade re-exports this public API. Leaf mutations and ``_python_leaf``
live in ``_lib_mutations``; identity and signing live in ``_lib_identity``. Both
remain re-exported here so pre-split ``rebar`` and ``_lib_writes`` imports resolve.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Literal, cast, overload

from rebar import config
from rebar._commands.gates import log_advisory_warning as _warn_advisory
from rebar._commands.gates import log_description_cap_warning as _warn_description_cap
from rebar._errors import ConcurrencyError, RebarError

# ── Re-exports of the concerns split out of this module ──────────────────────
# Kept so ``rebar/__init__.py`` (which re-exports all 27 names from here) and
# ``rebar/_lib_gates.py`` (which imports ``_python_leaf``) need no edit, and so any
# ``rebar._lib_writes.<name>`` reference keeps resolving.
from rebar._lib_identity import (  # noqa: F401
    add_identity_key,
    create_identity,
    ensure_identity_for,
    resolve_current_identity,
    revoke_identity_key,
    sign_manifest,
    use_identity,
    verify_signature,
)
from rebar._lib_mutations import (  # noqa: F401
    _python_leaf,
    append_session_log,
    archive,
    attach_commits,
    comment,
    compact,
    edit_ticket,
    link,
    start_session_log,
    tag,
    unlink,
    untag,
)
from rebar._lib_warn import emit_cross_session_warning

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING —
    # ``from __future__ import annotations`` makes every annotation a string, so
    # these names never need to exist at runtime (zero import cost, no cycle).
    from rebar.types import ClaimResult, CreateResult, TransitionResult


# ── Initialization ───────────────────────────────────────────────────────────
def init_repo(*, repo_root=None, force_new_store: bool = False) -> None:
    """Initialize or mount the ticket system explicitly without prompting.

    Remote discovery fails closed by default; ``force_new_store=True`` deliberately
    permits bootstrap only while reachability is unknown. Other library calls do not
    auto-init and require this to have run first (or ``rebar init`` interactively)."""
    from rebar._commands import init as _init_cmd

    rc = _init_cmd.init_core(repo_root, silent=True, force_new_store=force_new_store)
    if rc != 0:
        raise RebarError(f"rebar init failed (exit {rc})", returncode=rc)


# ── Write path (subprocess → dispatcher) ─────────────────────────────────────
@overload
def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = ...,
    priority: int | None = ...,
    assignee: str | None = ...,
    description: str | None = ...,
    tags: list[str] | None = ...,
    source: dict | None = ...,
    bridge_project: str | None = ...,
    repos: list[str] | None = ...,
    return_alias: Literal[False] = ...,
    repo_root=...,
    _creation_channel: str = ...,
) -> str: ...


@overload
def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = ...,
    priority: int | None = ...,
    assignee: str | None = ...,
    description: str | None = ...,
    tags: list[str] | None = ...,
    source: dict | None = ...,
    bridge_project: str | None = ...,
    repos: list[str] | None = ...,
    return_alias: Literal[True],
    repo_root=...,
    _creation_channel: str = ...,
) -> CreateResult: ...


def create_ticket(
    ticket_type: str,
    title: str,
    *,
    parent: str | None = None,
    priority: int | None = None,
    assignee: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    source: dict | None = None,
    bridge_project: str | None = None,
    repos: list[str] | None = None,
    return_alias: bool = False,
    repo_root=None,
    _creation_channel: str = "python",
) -> str | CreateResult:
    """Create a ticket.

    Returns the canonical 16-hex ticket id (default). With ``return_alias=True``,
    returns ``{"id", "alias", "description_warning", "duplicate_warning"}`` (the save-time
    description-cap notice and the create-time same-title duplicate advisory, both also
    logged) so agents skip a second ``show`` (WS5e).

    ``source`` (P1.2 import): optional provenance dict — keys ``source_id``,
    ``source_created_at``, ``source_author``, ``source_env`` are recorded on the
    CREATE event and surfaced in compiled state, so an imported ticket preserves
    where it came from while still getting a fresh local id + HLC timestamp.

    ``_creation_channel`` is INTERNAL (leading underscore; not part of the documented
    public signature): a direct library call defaults to ``"python"``, and the MCP
    adapter passes ``"mcp"`` through it so a genesis CREATE records its interface. A
    later import story overrides it via the ``source=`` path.
    """
    # Composed in-process via the shared create_core (validation/alias/CREATE
    # event); the bash create path was retired with the Tier B cutover.
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    try:
        res = composer.create_core(
            ticket_type,
            title,
            parent=parent,
            priority=priority,
            assignee=assignee,
            description=description,
            tags=tags,
            source=source,
            bridge_project=bridge_project,
            repos=repos,
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar create failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    warning = _warn_description_cap(res.get("description_warning"))
    dup_warning = _warn_advisory(res.get("duplicate_warning"))
    if not return_alias:
        return res["id"]
    return {
        "id": res["id"],
        "alias": res["alias"] or "",
        "description_warning": warning,
        "duplicate_warning": dup_warning,
    }


def idea(
    title: str,
    *,
    description: str | None = None,
    return_alias: bool = False,
    repo_root=None,
    _creation_channel: str = "python",
) -> str | CreateResult:
    """Capture an undesigned idea: create an ``epic`` in status ``idea`` atomically.

    The idea is born in status ``idea`` via a single CREATE event (no intervening
    STATUS event), so it is never momentarily ``open``/claimable. It is excluded from
    ``ready``/``next-batch``, and ``idea -> closed`` (reject) skips the completion
    gates. Promote a kept idea with ``transition(id, "idea", "open")``.

    Returns the canonical 16-hex ticket id (default), or the ``{"id", "alias",
    "description_warning", "duplicate_warning"}`` dict with ``return_alias=True`` (as
    :func:`create_ticket`).

    ``_creation_channel`` is INTERNAL (see :func:`create_ticket`): defaults to
    ``"python"``; the MCP adapter passes ``"mcp"``.
    """
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    try:
        res = composer.create_core(
            "epic",
            title,
            description=description,
            status="idea",
            repo_root=repo_root,
            creation_channel=_creation_channel,
        )
    except CommandError as exc:
        raise RebarError(
            f"rebar idea failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    warning = _warn_description_cap(res.get("description_warning"))
    dup_warning = _warn_advisory(res.get("duplicate_warning"))
    if not return_alias:
        return res["id"]
    return {
        "id": res["id"],
        "alias": res["alias"] or "",
        "description_warning": warning,
        "duplicate_warning": dup_warning,
    }


def _normalize_transition_force(force: str | None) -> str | None:
    """Validate the public transition force-bypass surface.

    The single approved shape is ``force: str | None`` — the audit reason — exactly like
    :func:`claim`: ``None`` means "not forcing"; any string forces, bypassing whichever gate
    this transition hits (start-work OR completion-verify close). An empty supplied string is
    still a present force and is rendered as ``"(no reason given)"`` by the command core.

    The retired boolean ``force=True`` compatibility spelling is rejected at the library
    boundary so stale callers do not silently bypass gates after the pre-1.0 removal."""
    if isinstance(force, bool):
        message = f'rebar.transition(force={force}) was removed; use force="<explicit reason>"'
        raise TypeError(message)
    return force


def _reject_removed_transition_kwargs(
    func: Callable[..., TransitionResult],
) -> Callable[..., TransitionResult]:
    @wraps(func)
    def wrapper(*args: object, **kwargs: object) -> TransitionResult:
        if "force_close" in kwargs:
            raise TypeError(
                'rebar.transition(force_close=...) was removed; use force="<explicit reason>"'
            )
        return func(*args, **kwargs)

    return wrapper


@_reject_removed_transition_kwargs
def transition(
    ticket_id: str,
    current_status: str,
    target_status: str,
    *,
    force: str | None = None,
    reason: str = "",
    close_class: str = "",
    caused_by: str = "",
    ref: str | None = None,
    repo_root=None,
) -> TransitionResult:
    """Transition status with optimistic concurrency and configured gates.

    A stale ``current_status`` raises :class:`ConcurrencyError`; unreadable gate
    config fails closed, and other command failures raise :class:`RebarError`.
    ``force=None`` keeps gates enforced; any supplied string bypasses the encountered
    gate as its audit reason, with ``""`` recorded as ``"(no reason given)"``. It never
    bypasses the open-children invariant, and a forced close has no completion
    signature. ``reason`` is only an eligible administrative close reason. Bug closes
    use ``close_class`` and optional ``caused_by``; ``ref`` selects the tree verified
    and signed by the close gate.
    """
    # In-process (Tier E E3): resolve the id, then run the shared transition core
    # (ticket-transition.sh was retired from this path). The structured result
    # {ticket_id, from, to, newly_unblocked[]} is the single source of truth.
    from rebar._commands import close_disposition
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    emit_cross_session_warning(ticket_id, repo_root=repo_root)
    force = _normalize_transition_force(force)

    # Mirror the CLI's admission rule (tickets 3803 + fc20 + bug d54b): the free-text
    # ``reason`` is persisted as ``close_reason`` ONLY on a non-force close whose class
    # admits one. Any other combination discards it here exactly as the CLI refuses it,
    # so the library and CLI paths cannot drift.
    admits_close_reason = force is None and (
        close_class in close_disposition.REASON_REQUIRED_CLASSES or close_class == "env_integration"
    )

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar transition failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        result = _transition.transition_compute(
            resolved,
            current_status,
            target_status,
            reason=reason,
            close_reason=(reason if admits_close_reason else ""),
            force_reason=force,
            close_class=close_class,
            caused_by=caused_by,
            ref=ref,
            repo_root=repo_root,
        )
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"transition rejected: {ticket_id} is no longer '{current_status}'. {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar transition failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None
    out: TransitionResult = {
        "ticket_id": result["ticket_id"],
        "from": result["from"],
        "to": result["to"],
        "newly_unblocked": result["newly_unblocked"],
    }
    # Forward the completion-signature marker when the close path produced one. This dict is
    # rebuilt field by field, so an unforwarded key is silently dropped — which would leave
    # library and MCP callers unable to detect a close that landed WITHOUT its signature
    # (bug silvern-dewy-damselfly). Absent for transitions with no signature in play.
    if "completion_signature" in result:
        out["completion_signature"] = result["completion_signature"]
    return out


def claim(
    ticket_id: str, *, assignee=None, force: str | None = None, repo_root=None
) -> ClaimResult:
    """Atomically move an open ticket to ``in_progress`` and assign it.

    A concurrent claim raises :class:`ConcurrencyError`; unreadable plan-gate config
    fails closed, and other failures raise :class:`RebarError`. When enabled, the gate
    requires a fresh certificate. ``force=None`` enforces it; any supplied string is
    an audited bypass reason, with ``""`` recorded as ``"(no reason given)"``.
    """
    # Resolve in process and return the shared core's structured claim result.
    from rebar._commands import transition as _transition
    from rebar._commands._seam import CommandError
    from rebar._commands.txn import ConcurrencyMismatch
    from rebar._engine_support.resolver import resolve_ticket_id

    tracker = str(config.tracker_dir(repo_root))
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise RebarError(
            f"rebar claim failed (exit 1): Error: ticket '{ticket_id}' not found",
            returncode=1,
            stderr=f"Error: ticket '{ticket_id}' not found\n",
        )
    try:
        # Preserve ``None`` for the configured default; explicit empty clears assignment.
        return cast(
            "ClaimResult",
            _transition.claim_compute(
                resolved,
                assignee=assignee,
                force_reason=(force or "(no reason given)") if force is not None else "",
                repo_root=repo_root,
            ),
        )
    except ConcurrencyMismatch as exc:
        raise ConcurrencyError(
            f"claim rejected: {ticket_id} is not open (already claimed). {exc.message}",
            returncode=10,
            stderr=exc.message,
        ) from None
    except CommandError as exc:
        raise RebarError(
            f"rebar claim failed (exit {exc.returncode}): {exc.message}",
            returncode=exc.returncode,
            stderr=exc.message,
        ) from None


def reopen(ticket_id: str, *, repo_root=None) -> TransitionResult:
    """Reopen a closed ticket with an optimistic parent-first cascade.

    Closed ancestors reopen recursively before the child; other parent states remain.
    The cascade is sequential and fail-fast, so a parent failure aborts the child and
    completed ancestor reopens are not rolled back.
    """
    return transition(ticket_id, "closed", "open", repo_root=repo_root)
