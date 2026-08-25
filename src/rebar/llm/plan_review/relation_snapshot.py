"""Canonical ticket-relation material captured before plan-review signing.

This module deliberately stays below the signing and orchestration layers.  It
reduces the ticket store once, normalizes the plan's direct material relations,
and returns the clean tracker revision that the later atomic-signing work uses
as its optimistic-concurrency token.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from rebar import config
from rebar._engine_support import reads as ticket_reads
from rebar.reducer import reduce_all_tickets

logger = logging.getLogger(__name__)

PlanMaterialRole = Literal["child", "prerequisite"]
PlanRelationSnapshotReason = Literal[
    "missing-target",
    "ambiguous-reference",
    "malformed-reference",
    "reducer-error",
    "canonical-id-mismatch",
    "store-read-failure",
]

_NATIVE_CANONICAL_ID_RE = re.compile(r"^[a-z0-9]{4}(?:-[a-z0-9]{4}){3}$")
_JIRA_CANONICAL_ID_RE = re.compile(r"^jira-[a-z][a-z0-9]+-[0-9]+$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def is_canonical_ticket_id(value: object) -> bool:
    """Whether ``value`` is a canonical on-disk local ticket-directory ID.

    Native tickets use the historical four-by-four form. Jira-originated
    tickets deterministically use ``jira-<lowercase-project>-<issue-number>``.
    Aliases and prefixes are deliberately excluded from this storage grammar.
    """

    text = str(value) if isinstance(value, str) else ""
    return bool(_NATIVE_CANONICAL_ID_RE.fullmatch(text) or _JIRA_CANONICAL_ID_RE.fullmatch(text))


@dataclass(frozen=True, order=True)
class PlanMaterialPin:
    role: PlanMaterialRole
    canonical_id: str
    material_fingerprint: str


@dataclass(frozen=True)
class PlanRelationSnapshot:
    subject_state: dict
    ticket_states_by_id: dict[str, dict]
    child_ids: tuple[str, ...]
    prerequisite_ids: tuple[str, ...]
    related_material: tuple[PlanMaterialPin, ...]
    ticket_store_revision: str


class PlanRelationSnapshotError(RuntimeError):
    """A closed, stable failure contract for relation-snapshot collection."""

    REASONS = frozenset(
        {
            "missing-target",
            "ambiguous-reference",
            "malformed-reference",
            "reducer-error",
            "canonical-id-mismatch",
            "store-read-failure",
        }
    )

    def __init__(
        self,
        reason: PlanRelationSnapshotReason,
        *,
        canonical_id: str | None = None,
        reference: str | None = None,
    ) -> None:
        if reason not in self.REASONS:
            raise ValueError(f"unknown plan relation snapshot reason: {reason}")
        self.reason = reason
        self.canonical_id = canonical_id
        self.reference = reference
        super().__init__(reason)


def _store_error(tracker: str | os.PathLike[str]) -> PlanRelationSnapshotError:
    return PlanRelationSnapshotError("store-read-failure", reference=str(tracker))


_UNMERGED_XY = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


def _status_line_is_dirt(line: str) -> bool:
    """Whether a ``git status --porcelain`` line must fail the strict tracker read.

    Index-only entries (a staged first column with a CLEAN worktree column) are the
    normal footprint of another writer inside its own LOCKED ``git add`` →
    ``git commit`` window (``_store/event_append.py`` runs them as two subprocesses);
    an unlocked reader observing that gap must not collapse it to
    ``store-read-failure`` — that starved sign-review at a measured 58% rate under
    one concurrent writer (bug a83f). Untracked entries (strict mode only —
    ``--untracked-files=no`` suppresses them otherwise), unmerged entries, and any
    worktree-side modification remain dirt: nothing in the canonical write path
    produces them, so they mean a genuinely unsafe tracker.
    """
    xy = line[:2]
    if len(xy) < 2 or xy in _UNMERGED_XY:
        return True
    if xy[0] in "?!":
        return True
    return xy[1] != " "


def tracker_head_sha(tracker: str | os.PathLike[str], *, ignore_untracked: bool = False) -> str:
    """Return a clean tickets-tracker HEAD, or fail with one stable reason.

    Freshness is established before all three strict git reads.  Dirty worktree,
    index-conflict, process, path, IO, and malformed-output failures intentionally
    collapse to ``store-read-failure``; callers must never interpret a best-effort
    or ``unknown`` revision as a safe signing token.  Another writer's index-only
    staged entries are tolerated (see :func:`_status_line_is_dirt`).
    """

    tracker_text = str(tracker)
    try:
        # Validate the raw value BEFORE Path normalization or freshness.  In
        # particular, ``Path("")`` means the ambient current directory; allowing
        # that through would let ensure_fresh create lock/throttle artifacts in
        # an unrelated repository.  A tracker is a non-empty git worktree (its
        # ``.git`` may be a file for a linked worktree or a directory).
        if not tracker_text.strip():
            raise _store_error(tracker_text)
        tracker_path = Path(tracker_text)
        if not tracker_path.is_dir() or not (tracker_path / ".git").exists():
            raise _store_error(tracker_text)

        ticket_reads.ensure_fresh(tracker_text)

        # raw-git-ok: generic command runner, argv supplied by caller
        def run(*args: str) -> str:
            proc = subprocess.run(
                ["git", "-C", tracker_text, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.stdout or ""

        status_args = (
            ("status", "--porcelain", "--untracked-files=no")
            if ignore_untracked
            else (
                "status",
                "--porcelain",
            )
        )
        status = run(*status_args)
        if any(_status_line_is_dirt(line) for line in status.splitlines() if line):
            raise _store_error(tracker_text)
        if run("ls-files", "-u"):
            raise _store_error(tracker_text)
        head = run("rev-parse", "HEAD").strip()
        if not _HEAD_RE.fullmatch(head):
            raise _store_error(tracker_text)
        return head
    except PlanRelationSnapshotError:
        raise
    except Exception:  # noqa: BLE001 — every strict tracker read failure has one contract
        raise _store_error(tracker_text) from None


def _valid_reference(reference: object) -> bool:
    if not isinstance(reference, str) or not reference or reference.strip() != reference:
        return False
    if reference in (".", "..") or reference.startswith(".") or "\x00" in reference:
        return False
    return not any(sep and sep in reference for sep in ("/", "\\", os.sep, os.altsep))


def _resolve_reference(
    reference: object,
    states: dict[str, dict],
    aliases: dict[str, set[str]],
) -> str:
    if not _valid_reference(reference):
        raise PlanRelationSnapshotError("malformed-reference", reference=str(reference))
    ref = str(reference)
    if ref in states:
        return ref

    matches = set(aliases.get(ref, set()))
    # Canonical prefix and historical 8-character/8-hex-shaped forms share the
    # same unambiguous prefix rule.  Aliases win only when they are themselves
    # unique; a collision across forms is ambiguous rather than order-dependent.
    if len(ref) >= 4:
        matches.update(ticket_id for ticket_id in states if ticket_id.startswith(ref))
    if len(matches) > 1:
        raise PlanRelationSnapshotError("ambiguous-reference", reference=ref)
    if not matches:
        raise PlanRelationSnapshotError("missing-target", reference=ref)
    return next(iter(matches))


def _holds_no_events(ticket_dir: Path) -> bool:
    """Whether ``ticket_dir`` holds no live event file at all.

    An event-less ticket directory is the debris of a write that died between
    ``os.makedirs`` and the event's atomic rename (see ``_store.event_append``).
    It carries no CREATE/SNAPSHOT — and therefore no relation material — by
    construction, which is what makes it safe for the relation snapshot to skip
    (bug 043f). The predicate mirrors the reducer's own event-file rule: a live
    event is a non-dot ``*.json`` that is not a folded ``*.retired`` source.

    Deliberately NARROW: a directory that DOES hold events but still fails to
    reduce may carry relations we would silently drop, so it keeps failing closed.
    """
    from rebar.reducer._cache import is_active_event

    try:
        return not any(
            entry.name.endswith(".json")
            and not entry.name.startswith(".")
            and is_active_event(entry.name)
            and entry.is_file()
            for entry in ticket_dir.iterdir()
        )
    except OSError:
        return False


def _load_states(tracker: Path) -> tuple[dict[str, dict], dict[str, set[str]]]:
    try:
        entries = sorted(
            entry.name
            for entry in tracker.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
    except (OSError, ValueError, TypeError):
        raise _store_error(tracker) from None
    try:
        reduced = reduce_all_tickets(
            tracker,
            exclude_archived=False,
            exclude_deleted=False,
            exclude_session_logs=False,
        )
    except Exception:  # noqa: BLE001 — a failed store reduction is a stable reducer error
        raise PlanRelationSnapshotError("reducer-error") from None
    if len(entries) != len(reduced):
        raise _store_error(tracker)

    states: dict[str, dict] = {}
    aliases: dict[str, set[str]] = {}
    for directory_id, state in zip(entries, reduced, strict=True):
        if not isinstance(state, dict) or state.get("status") in ("error", "fsck_needed"):
            # Bug 043f: an EVENT-LESS directory is skippable debris, not a broken
            # ticket. Failing the whole reduction on one made every plan review in
            # the clone return an unsigned INDETERMINATE naming an UNRELATED ticket,
            # and git cannot see the artifact (it cannot track empty directories) so
            # no .gitignore remedy reaches it. Skip it, and NAME THE PATH so the
            # operator can delete it.
            ticket_dir = tracker / directory_id
            if _holds_no_events(ticket_dir):
                logger.warning(
                    "skipping ticket directory with no CREATE/SNAPSHOT event: %s "
                    "(debris of an interrupted write; safe to delete)",
                    ticket_dir,
                )
                continue
            raise PlanRelationSnapshotError("reducer-error", reference=directory_id)
        canonical_id = state.get("ticket_id")
        if canonical_id != directory_id or not is_canonical_ticket_id(canonical_id):
            raise PlanRelationSnapshotError(
                "canonical-id-mismatch",
                canonical_id=str(canonical_id) if canonical_id is not None else None,
                reference=directory_id,
            )
        states[directory_id] = state
        alias = state.get("alias")
        if isinstance(alias, str) and alias:
            aliases.setdefault(alias, set()).add(directory_id)
    return states, aliases


def counts_as_plan_material_child(state: dict) -> bool:
    """The single predicate for "does this child count as a container's plan material".

    A child is plan material only while it is LIVE: ``archived`` and ``deleted``
    children are no longer part of the plan. Both the signer side (this module's
    child enumeration, which feeds ``generation.own_material`` and the
    ``plan-material-pin`` manifest) and the claim gate
    (:func:`attest.current_material_fingerprint`) MUST obtain their child set
    through this one helper. Spelling the status test independently at each site —
    the signer admitting ``status != "deleted"`` while the gate relied on
    ``list_tickets(... include_archived=False)`` — is exactly what let the two
    fingerprints diverge and made any container with an archived child permanently
    unclaimable (bug b7a2)."""
    return state.get("status") not in ("deleted", "archived")


@dataclass
class _MaterialChildIndexState:
    """Holder for the ContextVar-scoped child index (bug 3d57): ``by_parent`` stays
    ``None`` until the first :func:`live_material_children` call inside the context,
    so a scope whose fingerprint never reads the store costs zero scans."""

    repo_root: Any = None
    by_parent: dict[str, list[dict]] | None = None


_material_child_index: ContextVar[_MaterialChildIndexState | None] = ContextVar(
    "_material_child_index", default=None
)


@contextmanager
def material_child_index(*, repo_root=None) -> Iterator[None]:
    """Scope in which :func:`live_material_children` answers from ONE shared
    parent_id→children index instead of a per-parent full-store scan (bug 3d57: 89
    identical full-store reductions per ``rebar show``). The index is built lazily
    on first use from a single unfiltered wide ``list_tickets`` scan. Activation is
    idempotent — a nested ``with`` is a no-op passthrough; only the outermost
    context sets the ContextVar and restores its prior value on exit."""
    if _material_child_index.get() is not None:
        yield
        return
    token = _material_child_index.set(_MaterialChildIndexState(repo_root=repo_root))
    try:
        yield
    finally:
        _material_child_index.reset(token)


def _indexed_children(state: _MaterialChildIndexState, canonical_id: str) -> list[dict]:
    """Children of ``canonical_id`` from the shared snapshot, building it on first
    use. One wide, unfiltered ``list_tickets`` scan grouped by ``parent_id`` yields
    per-parent lists byte-identical to the per-parent enumeration (``list_tickets``
    order is ticket-id directory order, which grouping preserves per parent)."""
    if state.by_parent is None:
        from rebar import _reads

        by_parent: dict[str, list[dict]] = {}
        for ticket in _reads.list_tickets(include_archived=True, repo_root=state.repo_root) or []:
            parent = ticket.get("parent_id")
            if parent:
                by_parent.setdefault(parent, []).append(ticket)
        state.by_parent = by_parent
    return state.by_parent.get(canonical_id, [])


def live_material_children(canonical_id: str, *, repo_root=None) -> list[dict]:
    """Gate-side counterpart to the signer's child enumeration: list the container's
    children WIDE (``include_archived=True``) and keep only those that count as plan
    material, through :func:`counts_as_plan_material_child`. Centralising the gate's
    enumeration here is what guarantees the claim gate and the signer cannot drift
    apart (bug b7a2). Inside an active :func:`material_child_index` context the
    enumeration answers from the shared one-scan snapshot instead of its own
    full-store scan (bug 3d57); BOTH paths filter through the same
    :func:`counts_as_plan_material_child` predicate."""
    from rebar._engine_support.reads import current_ticket_view

    view = current_ticket_view()
    if view is not None:
        # Material fingerprints observe membership plus the live/archived status predicate,
        # not every field on every child. Recording full child states here would make an
        # unrelated child comment invalidate a completion receipt and recreate contention.
        return [
            child
            for child_id in view.direct_child_ids(canonical_id)
            if counts_as_plan_material_child(
                child := {
                    "ticket_id": child_id,
                    "status": view.field_value(child_id, "status"),
                }
            )
        ]
    index_state = _material_child_index.get()
    if index_state is not None:
        return [
            k
            for k in _indexed_children(index_state, canonical_id)
            if counts_as_plan_material_child(k)
        ]
    from rebar import _reads

    return [
        k
        for k in (
            _reads.list_tickets(parent=canonical_id, include_archived=True, repo_root=repo_root)
            or []
        )
        if counts_as_plan_material_child(k)
    ]


def current_plan_context(ticket_id: str, *, repo_root=None) -> Any:
    """The LIGHT :class:`PlanContext` the material fingerprint is computed from (the ticket
    plus its child ids — no full child fetch, no LLM), or ``None`` for a deleted target.

    Shared by the composite fingerprint below and by the per-component view in
    :mod:`material_diff` (bug 94a3), so the explainer diffs exactly the state the gate
    decided on rather than a second, independently-read one."""
    from rebar import _reads
    from rebar._engine_support.reads import current_ticket_view

    from .det_floor import PlanContext

    view = current_ticket_view()
    if view is None:
        state = _reads.show_ticket(ticket_id, repo_root=repo_root)
    else:
        canonical = view.resolve(ticket_id)
        if canonical is None:
            return None
        fields = (
            "status",
            "ticket_type",
            "title",
            "description",
            "file_impact",
            "file_impact_scope",
            "no_file_impact_reason",
        )
        state = {
            "ticket_id": canonical,
            **{field: view.field_value(canonical, field) for field in fields},
        }
    if state.get("status") == "deleted":
        return None
    canonical = state.get("ticket_id", ticket_id)
    try:
        kids = live_material_children(canonical, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — live fingerprinting keeps legacy best-effort
        from rebar._engine_support.reads import reraise_pinned_read_failure

        reraise_pinned_read_failure(exc)
        kids = []
    return PlanContext(
        ticket_id=canonical,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=[{"ticket_id": k.get("ticket_id")} for k in kids],
    )


def current_material_fingerprint_impl(
    ticket_id: str,
    *,
    repo_root=None,
    normalize_checkboxes: bool = True,
    normalize_whitespace: bool | None = None,
    normalize_reason: bool = True,
) -> str | None:
    """Recompute the ticket's material fingerprint from a LIGHT read (the ticket + its
    child ids only — no full child fetch, no LLM), matching
    :func:`pass1.material_fingerprint`. Returns None for a deleted target or on any read
    error. ``normalize_checkboxes=False`` recomputes under the PRE-normalization
    (pre-330c) algorithm — the raw description, with neither checkbox-state nor
    whitespace canonicalization — the validity-on-read grandfather basis (bug 96d1).

    (Body moved here from ``attest.current_material_fingerprint`` — which now delegates —
    because its one non-trivial dependency, :func:`live_material_children`, lives here.)"""
    from .pass1 import material_fingerprint

    try:
        ctx = current_plan_context(ticket_id, repo_root=repo_root)
        if ctx is None:
            return None
        return material_fingerprint(
            ctx,
            normalize_checkboxes=normalize_checkboxes,
            normalize_whitespace=normalize_whitespace,
            normalize_reason=normalize_reason,
        )
    except Exception as exc:
        from rebar._engine_support.reads import reraise_pinned_read_failure

        reraise_pinned_read_failure(exc)
        # Cannot compute the current fingerprint → caller treats material as unknown
        # (the gate fails closed / re-review). Log so the cause is observable.
        logger.warning("could not compute material fingerprint for %s", ticket_id, exc_info=True)
        return None


def _context_for(
    ticket_id: str,
    states: dict[str, dict],
    aliases: dict[str, set[str]],
) -> Any:
    from .det_floor import PlanContext

    state = states[ticket_id]
    children: list[dict] = []
    for candidate_id, candidate in states.items():
        parent = candidate.get("parent_id")
        if not parent:
            continue
        try:
            canonical_parent = _resolve_reference(parent, states, aliases)
        except PlanRelationSnapshotError:
            continue
        if canonical_parent == ticket_id and counts_as_plan_material_child(candidate):
            children.append({"ticket_id": candidate_id})
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=children,
    )


def collect_plan_relation_snapshot(
    ticket_id: str, *, repo_root=None, ignore_untracked: bool = False
) -> PlanRelationSnapshot:
    """Collect canonical direct-child/prerequisite material in one store reduction."""

    tracker = Path(config.tracker_dir(repo_root))
    revision = (
        tracker_head_sha(tracker, ignore_untracked=True)
        if ignore_untracked
        else tracker_head_sha(tracker)
    )
    states, aliases = _load_states(tracker)
    subject_id = _resolve_reference(ticket_id, states, aliases)
    subject = states[subject_id]
    if subject.get("status") == "deleted":
        raise PlanRelationSnapshotError(
            "missing-target", canonical_id=subject_id, reference=ticket_id
        )

    child_ids: set[str] = set()
    prerequisite_ids: set[str] = set()
    for candidate_id, candidate in states.items():
        parent = candidate.get("parent_id")
        if parent:
            try:
                canonical_parent = _resolve_reference(parent, states, aliases)
            except PlanRelationSnapshotError:
                canonical_parent = None
            if canonical_parent == subject_id and counts_as_plan_material_child(candidate):
                child_ids.add(candidate_id)

        for dep in candidate.get("deps") or []:
            if not isinstance(dep, dict):
                continue
            relation = dep.get("relation")
            reference = dep.get("target_id", dep.get("target"))
            if relation == "depends_on" and candidate_id == subject_id:
                prerequisite_ids.add(_resolve_reference(reference, states, aliases))
            elif relation == "blocks":
                target_id = _resolve_reference(reference, states, aliases)
                if target_id == subject_id:
                    prerequisite_ids.add(candidate_id)

    for target_id in sorted(child_ids | prerequisite_ids):
        if target_id not in states or states[target_id].get("status") == "deleted":
            raise PlanRelationSnapshotError(
                "missing-target", canonical_id=target_id, reference=target_id
            )

    pins: list[PlanMaterialPin] = []
    # Keep ordinary manifest/claim-gate imports free of the optional runner
    # stack; relation collection itself runs at the LLM-operation boundary.
    from .pass1 import material_fingerprint

    for role, ids in (("child", child_ids), ("prerequisite", prerequisite_ids)):
        for target_id in sorted(ids):
            try:
                fingerprint = material_fingerprint(_context_for(target_id, states, aliases))
            except PlanRelationSnapshotError:
                raise
            except Exception:  # noqa: BLE001 — malformed reduced material fails closed
                raise PlanRelationSnapshotError(
                    "reducer-error", canonical_id=target_id, reference=target_id
                ) from None
            pins.append(
                PlanMaterialPin(
                    cast(PlanMaterialRole, role),
                    target_id,
                    fingerprint,
                )
            )

    return PlanRelationSnapshot(
        subject_state=subject,
        ticket_states_by_id=states,
        child_ids=tuple(sorted(child_ids)),
        prerequisite_ids=tuple(sorted(prerequisite_ids)),
        related_material=tuple(sorted(pins)),
        ticket_store_revision=revision,
    )
