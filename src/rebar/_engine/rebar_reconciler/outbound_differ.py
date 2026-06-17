"""Outbound differ for bidirectional Jira sync.

Compares local ticket state against the Jira working-set snapshot and emits
a list of OutboundMutation objects describing changes to push from local to
Jira. Uses a BindingStore (from PR #401) to map local ticket IDs to Jira keys.

Local is the source of truth. Unbound local tickets emit "create" mutations;
bound tickets whose fields diverge from Jira emit "update" mutations with
only the changed fields.

This module is predominantly pure, with one controlled I/O seam: when the
caller passes a ``client`` argument to :func:`compute_outbound_mutations`, the
differ may call ``client.get_comments(jira_key)`` for bound tickets whose
snapshot entry lacks a ``comment`` field (the live Jira search shape — Jira
search does NOT return comment data). All other code paths remain pure.

Dependency: BindingStore interface (PR #401). This module codes against the
interface — get_jira_key(local_id) -> str|None, is_bound(local_id) -> bool —
and does not import the concrete class.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import urllib.error
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment (DSO_* support removed)."""
    return os.environ.get(f"REBAR_{name}", default)


# Sentinel: presence of the "comment" key in a snapshot entry confirms the
# snapshot carries real comment data (fixture/synthetic path). Absence means
# the entry came from a live Jira search result, which never includes comments.
_COMMENT_FIELD_KEY = "comment"

# ---------------------------------------------------------------------------
# Bug 1e08-1a35-0267-4ca6 — bound-but-absent direct-GET sentinels / config
# ---------------------------------------------------------------------------
# A bound local ticket whose Jira key is ABSENT from this pass's search
# snapshot (deleted, or status=Done beyond the fetcher's _DONE_RECENT_CAP
# window) used to diff every field against "" and re-emit every pass. The fix
# replaces ``jira_snapshot.get(jira_key, {})`` with a membership discriminator
# plus a bounded direct GET for the absent case. These module-level singleton
# objects are identity-compared (``is``) so they can never collide with a real
# ``fields`` dict.
_DELETED = object()  # _safe_get_issue: HTTPError 404 (issue gone)
_TRANSPORT_ERROR = object()  # _safe_get_issue: non-404 HTTPError / URLError / timeout

# Per-pass bounded GET budget (K) and consecutive-404 retirement grace. Env
# vars because the reconciler has no dotted-config reader (matches fetcher.py /
# applier.py). Parsed defensively at use-site so a typo'd ops value degrades to
# the default rather than aborting the pass.
_DEFAULT_ABSENT_GET_BUDGET = 20


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var defensively: malformed → default; clamp >= minimum."""
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _rest_issue_to_snapshot_fields(issue: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``fields`` block of a REST GET payload (NO normalization).

    The fetcher stores each snapshot entry as a verbatim copy of the issue's
    ``fields`` (``fetcher.py``), and ALL normalization happens downstream in
    ``_diff_fields`` / ``_extract_jira_field``. Re-normalizing here would
    double-normalize and reintroduce phantom re-emits — so this helper is a
    deliberate one-liner kept only so the C2 parity test has a real symbol.
    """
    return issue.get("fields", {})


def _safe_get_issue(client: Any, jira_key: str) -> Any:
    """Direct GET a single Jira issue's raw fields, classifying failures.

    Returns:
        - the raw ``fields`` dict on HTTP 200,
        - the ``_DELETED`` sentinel on HTTPError 404 (issue gone),
        - the ``_TRANSPORT_ERROR`` sentinel on any non-404 HTTPError, URLError,
          timeout, or OSError (transient — caller emits nothing and defers).

    ``get_issue_by_rest`` re-raises ``HTTPError`` without retry, so a 404 from
    a deleted issue surfaces here as a raised ``HTTPError`` (not a return).
    ``HTTPError`` is a subclass of ``URLError``, so it MUST be caught first.
    """
    try:
        return client.get_issue_by_rest(jira_key).get("fields", {})
    except urllib.error.HTTPError as exc:
        return _DELETED if exc.code == 404 else _TRANSPORT_ERROR
    except (urllib.error.URLError, TimeoutError, OSError):
        return _TRANSPORT_ERROR


def _is_retired(binding_store: Any, jira_key: str) -> bool:
    """``binding_store.is_retired`` with graceful fallback for legacy stubs."""
    fn = getattr(binding_store, "is_retired", None)
    if fn is None:
        return False
    try:
        return bool(fn(jira_key))
    except Exception:  # noqa: BLE001
        return False


def _last_get_pass(binding_store: Any, jira_key: str) -> str:
    """``binding_store.last_get_pass`` with fallback to the "" sentinel."""
    fn = getattr(binding_store, "last_get_pass", None)
    if fn is None:
        return ""
    try:
        return fn(jira_key) or ""
    except Exception:  # noqa: BLE001
        return ""


def _set_last_get(binding_store: Any, jira_key: str, pass_id: str) -> None:
    """``binding_store.set_last_get`` no-op when the store predates the method."""
    fn = getattr(binding_store, "set_last_get", None)
    if fn is not None:
        try:
            fn(jira_key, pass_id)
        except Exception:  # noqa: BLE001
            pass


def _note_absent(binding_store: Any, jira_key: str) -> None:
    """``binding_store.note_absent`` no-op when the store predates the method."""
    fn = getattr(binding_store, "note_absent", None)
    if fn is not None:
        try:
            fn(jira_key)
        except Exception:  # noqa: BLE001
            pass


def _clear_absent(binding_store: Any, jira_key: str) -> None:
    """``binding_store.clear_absent`` no-op when the store predates the method."""
    fn = getattr(binding_store, "clear_absent", None)
    if fn is not None:
        try:
            fn(jira_key)
        except Exception:  # noqa: BLE001
            pass


_ADF_KEY = "rebar_reconciler.adf"
_AdfModule = None

_COMMENT_LIMITS_KEY = "rebar_reconciler.comment_limits"
_CommentLimitsModule = None


def _load_comment_limits():
    """Lazy-load the sibling comment_limits module (same pattern as _load_adf).

    Bug 6afc-20ee-84e5-4dd5: the truncation rule MUST be identical on the send
    path (acli.add_comment) and this differ comparison path, so both
    import the single shared ``truncate_comment_body`` helper. Loaded by file
    path (not ``from . import``) because the differ may be imported via
    ``importlib.util.spec_from_file_location`` in tests, which does not establish
    package context.
    """
    global _CommentLimitsModule
    if _CommentLimitsModule is not None:
        return _CommentLimitsModule
    if _COMMENT_LIMITS_KEY in sys.modules:
        _CommentLimitsModule = sys.modules[_COMMENT_LIMITS_KEY]
        return _CommentLimitsModule
    cl_path = Path(__file__).parent / "comment_limits.py"
    spec = importlib.util.spec_from_file_location(_COMMENT_LIMITS_KEY, cl_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"comment_limits.py not found at {cl_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_COMMENT_LIMITS_KEY] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _CommentLimitsModule = mod
    return mod


def _load_adf():
    """Lazy-load the sibling adf module.

    The differ may be imported either as a normal package module (production)
    or via ``importlib.util.spec_from_file_location`` (tests). The latter
    does not establish package context, so ``from . import adf`` fails. Use
    the canonical dotted sys.modules key (same pattern as applier's
    _load_mutation_module) so the module is loaded exactly once across all
    callers.
    """
    global _AdfModule
    if _AdfModule is not None:
        return _AdfModule
    if _ADF_KEY in sys.modules:
        _AdfModule = sys.modules[_ADF_KEY]
        return _AdfModule
    adf_path = Path(__file__).parent / "adf.py"
    spec = importlib.util.spec_from_file_location(_ADF_KEY, adf_path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"adf.py not found at {adf_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_ADF_KEY] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _AdfModule = mod
    return mod


# ---------------------------------------------------------------------------
# BindingStore protocol — codes against PR #401's interface
# ---------------------------------------------------------------------------


@runtime_checkable
class BindingStoreProtocol(Protocol):
    """Minimal interface for the binding store (PR #401)."""

    def get_jira_key(self, local_id: str) -> str | None: ...
    def is_bound(self, local_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# OutboundMutation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundMutation:
    """A single outbound change to push to Jira."""

    local_id: str
    jira_key: str | None  # None for create (not yet assigned)
    action: str  # "create" | "update" | "delete"
    fields: dict[str, Any]  # changed fields only for update; all fields for create
    comments: list[dict[str, Any]] = dataclass_field(default_factory=list)
    labels: list[dict[str, Any]] = dataclass_field(default_factory=list)
    links: list[dict[str, Any]] = dataclass_field(default_factory=list)


# ---------------------------------------------------------------------------
# Field mapping constants
# ---------------------------------------------------------------------------

_LOCAL_TO_JIRA_TYPE: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

_LOCAL_TO_JIRA_PRIORITY: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
}

_LOCAL_TO_JIRA_STATUS: dict[str, str] = {
    "open": "To Do",
    "in_progress": "In Progress",
    # blocked/cancelled have no direct equivalent in the live DIG workflow
    # ({To Do, In Progress, In Review, Done} only). Map to the nearest live
    # state; lossless information is preserved via rebar-status: annotation
    # labels emitted/removed by status logic (see _status_annotation_labels).
    "blocked": "In Progress",
    "closed": "Done",
    "cancelled": "Done",
    "deleted": "Done",
}

# Local statuses that need an annotation label to preserve lossless intent.
# Maps local_status -> rebar-status:<label> emitted when that status is active.
_STATUS_ANNOTATION_LABEL: dict[str, str] = {
    "blocked": "rebar-status:blocked",
    "cancelled": "rebar-status:cancelled",
}


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------


def _map_local_to_jira_fields(
    ticket: dict[str, Any],
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Map local ticket fields to Jira field names/values.

    Use ``.get(key) or default`` (not ``.get(key, default)``) for string
    fields so an explicit ``None`` value normalises to the empty-string
    default. ``.get(key, default)`` only falls back when the key is
    MISSING — if the key exists with value ``None`` (e.g. unassigned
    tickets where the ticket reducer initialises ``assignee: None``),
    .get returns None, not the default. None then propagates through
    ``_diff_fields`` and becomes the literal string ``"None"`` after
    str() conversion at the ACLI boundary, causing ACLI to reject the
    edit with exit 1.

    Parent resolution (ticket 8b25): when ``binding_store`` is supplied and
    the ticket carries a ``parent_id``, attempt to resolve it to a Jira key
    via ``binding_store.get_jira_key(parent_id)``.  An unbound parent is
    silently omitted from the returned dict (not ``None`` / empty-string) so
    the diff layer can distinguish "no parent set" from "parent set but
    unbound this pass" and skip / retry accordingly.
    """
    result: dict[str, Any] = {
        "summary": ticket.get("title") or "",
        "description": ticket.get("description") or "",
        "issuetype": _LOCAL_TO_JIRA_TYPE.get(ticket.get("ticket_type", "task"), "Task"),
        "priority": _LOCAL_TO_JIRA_PRIORITY.get(ticket.get("priority", 2), "Medium"),
        "status": _LOCAL_TO_JIRA_STATUS.get(ticket.get("status", "open"), "To Do"),
        "assignee": ticket.get("assignee") or "",
    }
    # Parent sync (ticket 8b25): resolve local parent_id → Jira key.
    # Omit the key entirely when unbound so _diff_fields skips it (retry next pass).
    local_parent_id = ticket.get("parent_id") or None
    if local_parent_id and binding_store is not None:
        import logging as _logging

        # Hierarchy pre-check (ticket 8b25): on this next-gen Jira project only
        # an Epic may be a parent. A non-epic parent (e.g. Task→Task) is
        # rejected by Jira with HTTP 400. Suppress the parent diff entirely
        # when the resolved local parent's ticket_type != "epic" so the differ
        # never perpetually re-emits a parent mutation Jira will always reject.
        # This is a sync exclusion mirroring the bug-36af issuetype pattern.
        # When parent-type info is unavailable (map not supplied / parent
        # absent from the map), fall through to the existing behaviour rather
        # than guess — the applier-side 400-skip remains the backstop.
        if local_ticket_types is not None and local_parent_id in local_ticket_types:
            parent_type = (local_ticket_types.get(local_parent_id) or "").lower()
            if parent_type != "epic":
                _logging.getLogger(__name__).debug(
                    "_map_local_to_jira_fields: parent_id=%r for ticket %r is a "
                    "non-epic (%r); suppressing parent diff (Jira hierarchy only "
                    "permits Epic parents — sync exclusion, ticket 8b25)",
                    local_parent_id,
                    ticket.get("ticket_id"),
                    parent_type,
                )
                return result

        jira_parent_key = binding_store.get_jira_key(local_parent_id)
        if jira_parent_key:
            result["parent"] = jira_parent_key
        else:
            _logging.getLogger(__name__).debug(
                "_map_local_to_jira_fields: parent_id=%r for ticket %r is unbound "
                "this pass; skipping parent field (will retry next pass)",
                local_parent_id,
                ticket.get("ticket_id"),
            )
    return result


def _extract_jira_field(jira_fields: dict[str, Any], field: str) -> Any:
    """Extract a Jira field value, handling nested structures.

    Jira API returns some fields as nested objects (priority.name,
    issuetype.name, status.name, assignee.displayName), and description is
    returned as an Atlassian Document Format (ADF) dict, not a plain string.

    Bug 85a1: before this fix, description ADF dicts were extracted as
    ``""`` because the generic ``raw.get("name", raw.get("displayName", ""))``
    fallback found neither key on ADF (``{"type": "doc", "version": 1,
    "content": [...]}``). The differ then reported description as changed on
    every pass for every bound ticket — the 21-mutation idempotency churn
    documented in the e2e probe Phase 6.

    Fix: dispatch by field name. Description ADF dicts are decoded via
    ``adf.adf_to_text``; assignee continues to return ``displayName`` (the
    canonical form local probe tickets store); priority / status / issuetype
    use the existing ``.name`` extraction. Plain string values (including
    legacy snapshots from before ADF migration) pass through unchanged.
    """
    raw = jira_fields.get(field)
    if raw is None:
        return ""

    # Description: ADF dict → plain text via the project's ADF walker.
    if field == "description":
        if isinstance(raw, dict):
            return _load_adf().adf_to_text(raw)
        return raw  # legacy plain-string snapshot

    if isinstance(raw, dict):
        # Jira nested objects: {name: ..., id: ...}
        return raw.get("name", raw.get("displayName", ""))
    return raw


def _assignee_matches(local_val: str, jira_raw: Any) -> bool:
    """Permissive assignee equality (bug 85a1, Gap 4).

    Jira returns assignee as a dict with at least ``{accountId, displayName,
    emailAddress}``; local tickets store assignee as a bare string that may
    be an email (ticket-create.sh default), a displayName (probe), or
    "Test" (git-config default), depending on how the ticket was made.
    A direct ``local_val == _extract_jira_field(...)`` comparison fires on
    every pass for any user not stored under the same identity form as
    Jira returns — Phase 6 idempotency churn AND spurious outbound updates.

    Treat ``local_val`` as matching when it equals ANY of {emailAddress,
    accountId, displayName}. Both sides empty (unassigned) also match.
    """
    if jira_raw is None:
        return local_val == ""
    if not isinstance(jira_raw, dict):
        return local_val == str(jira_raw)
    candidates = {
        (jira_raw.get("emailAddress") or "").strip(),
        (jira_raw.get("accountId") or "").strip(),
        (jira_raw.get("displayName") or "").strip(),
    }
    candidates.discard("")
    return (local_val or "").strip() in candidates


def _diff_fields(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare local ticket to Jira fields. Return only changed fields.

    Uses local-wins: if local differs, push outbound regardless of Jira state.
    Assignee comparison is shape-tolerant via ``_assignee_matches`` so
    Jira's ``{accountId, displayName, emailAddress}`` dict matches a local
    string in any of those three forms (bug 85a1, Gap 4).

    Bug b859 (Part 0d): when ``REBAR_RECONCILER_VERBOSE=1`` is set, emit a
    one-line RECON record per detected field-diff with truncated local /
    jira values so operators can debug parity issues directly from the
    probe's side-car log. Off by default to keep production stderr quiet.

    Parent diff (ticket 8b25): when ``binding_store`` is provided and the
    ticket carries a ``parent_id``, the resolved Jira parent key is diffed
    against ``jira_fields["parent"]["key"]``.  Unbound parents are omitted
    from the mapped dict and therefore never emitted as changes.
    """
    import sys

    verbose = _rebar_env("RECONCILER_VERBOSE", "0") == "1"
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "<no-id>"

    local_mapped = _map_local_to_jira_fields(
        ticket, binding_store=binding_store, local_ticket_types=local_ticket_types
    )
    changed: dict[str, Any] = {}
    for field_name, local_val in local_mapped.items():
        # Bug 36af: ticket_type/issuetype is governed by an approved sync
        # exception — updates do NOT propagate in either direction once
        # the ticket is bound. The local 'epic' type has no faithful Jira
        # reverse-mapping and Jira workflows often reject issuetype edits
        # cross-hierarchy (Bug<->Epic). issuetype IS still emitted at
        # CREATE time (it's a Jira-required field for issue creation),
        # but the diff loop here only runs for bound update mutations,
        # so excluding the field here only affects updates.
        if field_name == "issuetype":
            continue
        if field_name == "assignee":
            if not _assignee_matches(local_val, jira_fields.get("assignee")):
                changed[field_name] = local_val
                if verbose:
                    print(  # noqa: T201
                        f"RECON: field_diff ticket={ticket_id} "
                        f"field=assignee local={local_val!r:.80} "
                        f"jira={jira_fields.get('assignee')!r:.80}",
                        file=sys.stderr,
                    )
            continue
        if field_name == "parent":
            # Jira returns parent as {"key": "DIG-N"}; local_val is the resolved
            # Jira key string. Extract the Jira-side parent key for comparison.
            jira_parent_raw = jira_fields.get("parent")
            jira_parent_key = (
                jira_parent_raw.get("key") if isinstance(jira_parent_raw, dict) else None
            )
            if local_val != jira_parent_key:
                changed[field_name] = local_val
                if verbose:
                    print(  # noqa: T201
                        f"RECON: field_diff ticket={ticket_id} "
                        f"field=parent local={local_val!r:.80} "
                        f"jira={jira_parent_key!r:.80}",
                        file=sys.stderr,
                    )
            continue
        jira_val = _extract_jira_field(jira_fields, field_name)
        # Bug (plateau): Jira's ADF normalization strips trailing
        # whitespace from descriptions (and titles) on every write. If
        # local carries trailing ``\n\n`` (or any trailing whitespace),
        # the next fetch returns the stripped form — diff fires again —
        # apply pushes the original — infinite phantom-mutation loop.
        # Discovered during 20-batch live verification (2026-05-29):
        # DIG-4175 plateaued at 339 outbound updates for batches 7-20
        # because local description was 3701 chars, jira-decoded was
        # 3699 (delta = trailing ``\n\n``). Compare with rstrip() so
        # trailing-whitespace differences don't trigger the diff.
        if (
            isinstance(local_val, str)
            and isinstance(jira_val, str)
            and local_val.rstrip() == jira_val.rstrip()
        ):
            continue
        if local_val != jira_val:
            changed[field_name] = local_val
            if verbose:
                # Truncate value repr to keep one-line records reasonable.
                _l = repr(local_val)
                _j = repr(jira_val)
                if len(_l) > 80:
                    _l = _l[:77] + "..."
                if len(_j) > 80:
                    _j = _j[:77] + "..."
                print(  # noqa: T201
                    f"RECON: field_diff ticket={ticket_id} field={field_name} local={_l} jira={_j}",
                    file=sys.stderr,
                )
    return changed


# ---------------------------------------------------------------------------
# Comment diff
# ---------------------------------------------------------------------------


def _map_comments_for_create(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    """Map all local comments to outbound create mutations.

    Every outbound body is decorated with the reconciler marker (Gap 1
    loop-breaker) so inbound passes can identify our own echoes.
    """
    comments = ticket.get("comments", [])
    return [
        {"action": "add", "body": _decorate_outbound_comment(c.get("body", ""))} for c in comments
    ]


# Bug 85a1 (Gap 1): marker token embedded in every outbound comment body
# so the inbound differ can identify and filter our own echoes when the
# reconciler reads Jira comments back on the next pass. Without the marker
# every outbound comment would re-appear inbound as a "new Jira comment"
# and the bridge would loop. Kept identical here and in inbound_differ.py
# so both directions agree on the loop-breaker pattern.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


def _normalize_comment_body(body: Any) -> str:
    """Coerce a comment body to a comparable plain-text string.

    Jira comments are returned with ``body`` as an Atlassian Document Format
    (ADF) dict (``{"type": "doc", ...}``). Local comments store ``body`` as a
    plain string. Direct dict-vs-string comparison always reports them as
    different — driving spurious duplicate pushes (Phase 2 verify-no-
    duplicate-comments: "found 2 copies") and the dict-as-key crash in
    ``_diff_comments`` (Phase 3+ "unhashable type: 'dict'" when an ADF body
    flows into a ``set[str]`` insertion).

    Normalize via ``adf.adf_to_text`` so the canonical comparison is on
    text. Bug 85a1. The reconciler marker token (Gap 1) is also stripped
    so dedup compares the *user content* on both sides — without the strip,
    a previously-pushed Jira body ``"hello\\n\\n<marker>"`` would never match
    a local ``"hello"`` and the diff would re-emit the same comment.
    """
    if isinstance(body, dict):
        text = _load_adf().adf_to_text(body)
    else:
        text = str(body) if body is not None else ""
    return text.replace(RECONCILER_MARKER, "").strip()


def _decorate_outbound_comment(body: str) -> str:
    """Append the reconciler marker to an outbound comment body (Gap 1).

    Two paragraphs of separation keeps the marker visually below the user
    content. The marker survives ADF round-trip (each paragraph maps to
    its own ADF node and back).
    """
    return f"{body}\n\n{RECONCILER_MARKER}"


# Reconciler-internal machine-comment exclusion.
#
# These prefixes mark reconciler-generated machine comments that must NEVER be
# mirrored outbound to Jira (they are internal monitoring noise, not human Jira
# content). Only reconciler-internal markers are listed here — a standalone
# rebar produces no skill-to-skill payload comments, so none are listed.
#
# Kept here beside the comment-diff logic for locality with the only consumer
# (_diff_comments). Human comments are never excluded.
#
# Prefixes:
#   - BRIDGE_CANARY_ALERT: heartbeat-canary staleness alert. The canary appends a
#       freshly-TIMESTAMPED "Still stale as of <ts>: ..." comment to its alert
#       ticket every run; mirrored outbound, the volatile timestamp never matches
#       a prior Jira body, so the comment re-adds every pass and accumulates
#       duplicate Jira comments. Internal monitoring noise — exclude it.
#   - "Still stale as of"  LEGACY pre-marker form of the same canary alert comment
#       (the BRIDGE_CANARY_ALERT: marker only tags future canary comments; this
#       excludes the existing unmarked backlog too). The canary is the only
#       producer of this exact phrasing (a human would not write it).
_EXCLUDED_COMMENT_PREFIXES: tuple[str, ...] = (
    "BRIDGE_CANARY_ALERT:",
    "Still stale as of",
)


def _is_machine_marker_comment(normalized_body: str) -> bool:
    """True when a normalised comment body is a bridge-internal machine payload.

    Match is prefix-based on the already-normalised (ADF→text, marker-stripped,
    leading/trailing-whitespace-stripped) body so it is robust to ADF round-trip
    and the RECONCILER_MARKER decoration.
    """
    return normalized_body.startswith(_EXCLUDED_COMMENT_PREFIXES)


def _diff_comments(
    ticket: dict[str, Any],
    jira_key: str,
    jira_snapshot: dict[str, Any],
    client: Any = None,
) -> list[dict[str, Any]]:
    """Compare local comments to Jira comments. Return mutations for new comments.

    Matching rule: emit a comment "add" only for local comment bodies NOT
    already present in Jira, after normalising both sides via
    :func:`_normalize_comment_body` (ADF→text conversion + RECONCILER_MARKER
    strip + whitespace strip). Body equality after normalisation → skip
    (already mirrored); otherwise emit with outbound decoration.

    Snapshot lookup: the Jira REST API places comments at
    fields["comment"]["comments"] (outer key is "comment", not "comments").
    The fetcher writes snapshot[jira_key] = {k: fields[k] for k in fields},
    so we read jira_issue["comment"]["comments"] (bug 4572 fix).

    Live search path (bug 4292 fix): Jira search results do NOT include the
    ``comment`` field. When the snapshot entry for *jira_key* lacks the
    ``comment`` key, the comment state is unknown. If a ``client`` is provided,
    fetch comments live via ``client.get_comments(jira_key)``; this is bounded
    (one call per ticket with local comments). If the live fetch FAILS, skip
    comment mutations for this ticket entirely and emit a loud warning —
    never emit blind adds against unknown Jira comment state (the root cause
    of DIG-5301 reaching 14 duplicate comments).

    When the snapshot entry DOES carry a ``comment`` key (fixture/synthetic path),
    use it directly — the client is NOT consulted (fixture path preserved).

    Note: PR #402 (ADF walker + comment ID binding) will provide exact ID-
    based binding once available; this body-equality match is the baseline.
    """
    local_comments = ticket.get("comments", [])
    jira_issue = jira_snapshot.get(jira_key, {})

    # Safety invariant (bug 4292): distinguish "snapshot has comment data" from
    # "snapshot lacks comment field entirely" (live search shape).
    #
    # Jira search returns fields WITHOUT the comment field. The fetcher copies
    # fields verbatim: snapshot[key] = {k: fields[k] for k in fields}. So a
    # live snapshot entry will never have a "comment" key. A fixture/synthetic
    # entry built for tests WILL have it. We use the key's presence as the
    # discriminator, not the value (empty dict is a valid Jira response for an
    # issue with no comments, and indistinguishable from an absent key if we
    # only check truthiness).
    if _COMMENT_FIELD_KEY in jira_issue:
        # Snapshot-carried path (fixtures, synthetic, or snapshot enriched with
        # comment data). Use directly — do NOT call client.
        comment_field = jira_issue[_COMMENT_FIELD_KEY]
        if isinstance(comment_field, dict):
            jira_comments: list = comment_field.get("comments", [])
        else:
            jira_comments = []
    else:
        # Live search path: snapshot lacks comment field.
        # When there are no local comments, nothing to compare — skip the
        # live fetch (avoid an unnecessary API call).
        if not local_comments:
            return []

        if client is None:
            # No client provided. We cannot know the Jira comment state.
            # Emit a warning and skip comment mutations to avoid blind duplicates.
            print(  # noqa: T201
                f"WARNING: outbound_differ: snapshot for {jira_key!r} lacks "
                f"'comment' field (live search shape) and no client was provided. "
                f"Skipping comment mutations to avoid blind duplicate adds. "
                f"Pass a client to compute_outbound_mutations to enable live "
                f"comment fetch.",
                file=sys.stderr,
            )
            return []

        # Fetch comments live. One call per ticket — bounded by the set of
        # bound tickets with local comments.
        try:
            jira_comments = client.get_comments(jira_key)
            if not isinstance(jira_comments, list):
                jira_comments = []
        except Exception as exc:  # noqa: BLE001
            # Live fetch failed. Skip comment mutations entirely for this ticket.
            # Never emit blind adds when comment state is unknown (bug 4292 safety
            # invariant). Emit a loud warning + log to stderr.
            print(  # noqa: T201
                f"WARNING: outbound_differ: live comment fetch for {jira_key!r} "
                f"failed ({exc!r}). Skipping comment mutations for this ticket "
                f"to avoid emitting duplicate adds against unknown Jira state. "
                f"Alert: jira_key={jira_key!r}",
                file=sys.stderr,
            )
            return []

    jira_bodies: set[str] = set()
    for c in jira_comments:
        raw = c.get("body", "") if isinstance(c, dict) else c
        jira_bodies.add(_normalize_comment_body(raw))

    mutations: list[dict[str, Any]] = []
    for c in local_comments:
        raw = c.get("body", "") if isinstance(c, dict) else c
        body = _normalize_comment_body(raw)
        # Bug 6afc-20ee-84e5-4dd5: never mirror skill-to-skill machine-marker
        # comments (BRIDGE_CANARY_ALERT:, etc.) outbound to Jira. Symmetric with
        # the label _EXCLUDED_PREFIXES exclusion.
        if _is_machine_marker_comment(body):
            continue
        # Bug 6afc-20ee-84e5-4dd5 (convergence): the send path truncates an
        # over-length body to Jira's 32,767-char limit before it lands, so the
        # body that comes back in jira_bodies is the TRUNCATED form. Apply the
        # SAME shared truncation to the expected local body before the membership
        # test; otherwise the full local body never matches the truncated Jira
        # body and the diff re-emits forever. The local store is NOT mutated —
        # `body` here is an in-memory comparison key only.
        compare_body = _load_comment_limits().truncate_comment_body(body)
        if compare_body and compare_body not in jira_bodies:
            # Decorate the outbound body with the reconciler marker so the
            # inbound differ can identify (and filter) our own echoes on the
            # next pass (Gap 1 loop-breaker).
            mutations.append({"action": "add", "body": _decorate_outbound_comment(body)})
    return mutations


# ---------------------------------------------------------------------------
# Label diff
# ---------------------------------------------------------------------------

# NOTE: applier.py writes the bridge-internal binding label as
# f"rebar-id:{local_id}" (COLON separator). Legacy code paths used a HYPHEN
# separator ("rebar-id-<local_id>"); both forms must be excluded from outbound
# diffs to avoid emitting spurious remove mutations for identity labels.
# See bug 68a4-f9d5-5540-4b95.
# rebar-status: labels are reconciler-managed annotation labels (emitted/removed
# by status logic only); they must be excluded from the normal user-tag diff
# so that rebar-status: labels on Jira do not produce spurious REMOVE mutations
# via the tag diff path (ticket 929a).
_EXCLUDED_PREFIXES: tuple[str, ...] = ("rebar-id:", "rebar-id-", "imported:", "rebar-status:")


def _diff_labels(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compare local tags to Jira labels. Exclude bridge-internal labels.

    Bug a06c — REMOVE intent gating: when ``intent_set`` is provided
    (non-None), a label in ``jira_labels - local_tags`` only produces a
    REMOVE mutation when it appears in ``intent_set`` (the local
    "ever-seen" set computed by ``local_label_intent``). This prevents
    spurious REMOVEs for labels Jira side-added that local never had —
    the root cause of T3 IB-ADD silently dropping under PR #457 bidir
    suppression.

    When ``intent_set`` is None, the legacy "remove anything in jira
    but not in local" behavior is preserved (backwards compatible for
    every existing test and call site).
    """
    local_tags: set[str] = set(
        t for t in ticket.get("tags", []) if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
    )
    jira_labels: set[str] = set(
        label
        for label in (jira_fields.get("labels") or [])
        if not any(label.startswith(p) for p in _EXCLUDED_PREFIXES)
    )

    mutations: list[dict[str, Any]] = []
    for label in sorted(local_tags - jira_labels):
        if intent_set is not None and label not in intent_set:
            # Label is in local's current tag set but was never user-added
            # (only inbound-applied). Suppress the outbound ADD so a
            # subsequent Jira-side REMOVE is not cancelled by a spurious
            # re-ADD (T4 IB-REMOVE regression). See bug a06c.
            continue
        mutations.append({"action": "add", "label": label})
    for label in sorted(jira_labels - local_tags):
        if intent_set is not None and label not in intent_set:
            # Label was never in local's history -> suppress spurious REMOVE.
            continue
        mutations.append({"action": "remove", "label": label})
    return mutations


# ---------------------------------------------------------------------------
# Status annotation label helpers (ticket 929a)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Link diff (story 25ae-92e6-2927-49b6, Cycle 2)
# ---------------------------------------------------------------------------
#
# Relation <-> Jira link-type mapping. The canonical definition lives in
# acli_graph._RELATION_TO_JIRA_LINK (Cycle 1), but the differ is loaded
# standalone via spec_from_file_location in tests (no package context, so
# ``from rebar_reconciler.acli_graph import ...`` is not reliably importable
# and would pull the whole ACLI client import chain). We re-declare a local
# copy here — the same single-source-of-vocabulary pattern as the local
# _LOCAL_TO_JIRA_* constants above. Keep in sync with acli_graph.
#
# Each entry maps a rebar relation -> (jira_link_type, swap_endpoints).
# ``swap_endpoints`` records that "A relation B" maps to a Jira link with the
# endpoints reversed: "A depends_on B" == "B blocks A". Relations with no
# reliable Jira link type (duplicates / supersedes / discovered_from) are
# intentionally ABSENT and SKIPPED by the differ.
_RELATION_TO_JIRA_LINK: dict[str, tuple[str, bool]] = {
    "blocks": ("Blocks", False),
    "depends_on": ("Blocks", True),  # A depends_on B == B blocks A
    "relates_to": ("Relates", False),
}


def _existing_jira_links(jira_fields: dict[str, Any]) -> set[tuple[str, str]]:
    """Index a Jira issue's ``issuelinks`` as a ``{(type_name, target_key)}`` set.

    Direction semantics (verified live): for the issue X carrying this
    ``issuelinks`` array, an entry with ``inwardIssue.key == Y`` names X as the
    OUTWARD (e.g. blocker) side and Y the inward side; an entry with
    ``outwardIssue.key == Y`` names Y the outward side. The dedup key we build is
    ``(type_name, the-other-issue-key)`` REGARDLESS of direction — an ADD-only
    outbound diff just needs to know "does a link of this type to that key
    already exist in either direction", which is what avoids per-pass churn.
    """
    existing: set[tuple[str, str]] = set()
    for link in jira_fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        type_name = link_type.get("name") if isinstance(link_type, dict) else None
        if not type_name:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict) and side.get("key"):
                existing.add((type_name, side.get("key")))
    return existing


def _diff_links(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
) -> list[dict[str, Any]]:
    """Compare a local ticket's ``deps`` to its Jira issuelinks. ADD-only.

    For each local dep ``{target_id, relation, link_uuid}``:
      - resolve ``target_id`` -> Jira key (skip unbound, mirroring the
        parent-unbound skip in ``_map_local_to_jira_fields``);
      - map ``relation`` -> Jira link type via ``_RELATION_TO_JIRA_LINK``
        (skip unmapped relations: duplicates / supersedes / discovered_from);
      - DEDUP against the issue's existing ``issuelinks`` by
        ``(jira_link_type, target_key)`` so an already-present link emits
        nothing (critical to avoid re-emitting a `set_relationship` every pass);
      - emit ``{"action":"add","type":...,"to_key":...,"relation":...,
        "link_uuid":...}``.

    No REMOVE mutations are emitted (additive-only, mirroring the create-time
    label behaviour). The applier (Cycle 3) consumes ``to_key`` as the link
    target. The recorded ``relation`` is the rebar relation; ``swap_endpoints``
    is handled by the applier when issuing the directional Jira call.
    """
    deps = ticket.get("deps") or []
    if not deps:
        return []
    existing = _existing_jira_links(jira_fields)

    mutations: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        relation = dep.get("relation")
        mapped = _RELATION_TO_JIRA_LINK.get(relation)
        if mapped is None:
            continue  # no reliable Jira link type — skip (no-op)
        jira_type, _swap = mapped
        target_id = dep.get("target_id")
        if not target_id:
            continue
        target_key = binding_store.get_jira_key(target_id)
        if not target_key:
            continue  # unbound target — skip, retry next pass
        key = (jira_type, target_key)
        if key in existing or key in emitted:
            continue  # already present in Jira (either direction) or already queued
        emitted.add(key)
        mutations.append(
            {
                "action": "add",
                "type": jira_type,
                "to_key": target_key,
                "relation": relation,
                "link_uuid": dep.get("link_uuid"),
            }
        )
    return mutations


def _diff_status_annotation_labels(
    local_status: str,
    jira_labels: list[str],
) -> list[dict[str, Any]]:
    """Compute add/remove mutations for rebar-status: annotation labels.

    These labels encode lossless status information for statuses that have no
    direct equivalent in the live DIG Jira workflow (currently blocked and
    cancelled, which map to In Progress and Done respectively).

    Rules:
    - When local_status is in _STATUS_ANNOTATION_LABEL, emit ADD for the
      corresponding rebar-status: label if Jira does not already carry it.
    - When a rebar-status: annotation label is present on Jira but local_status
      no longer matches it, emit REMOVE to clean up the stale label.
    """
    mutations: list[dict[str, Any]] = []
    desired_annotation = _STATUS_ANNOTATION_LABEL.get(local_status)
    jira_annotation_labels = {label for label in jira_labels if label.startswith("rebar-status:")}

    # Add desired annotation if not already present
    if desired_annotation is not None and desired_annotation not in jira_annotation_labels:
        mutations.append({"action": "add", "label": desired_annotation})

    # Remove stale annotations (rebar-status: labels that no longer match)
    for stale in sorted(jira_annotation_labels):
        if stale != desired_annotation:
            mutations.append({"action": "remove", "label": stale})

    return mutations


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_outbound_mutations(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, Any],
    binding_store: BindingStoreProtocol,
    excluded_statuses: set[str] | None = None,
    local_label_intent: dict[str, set[str]] | None = None,
    client: Any = None,
    pass_id: str = "",
    absent_alive_fields: dict[str, dict[str, Any]] | None = None,
) -> list[OutboundMutation]:
    """Diff local tickets against Jira snapshot and return outbound mutations.

    Args:
        local_tickets: List of local ticket dicts. Each has: ticket_id, title,
            description, status, priority, ticket_type, assignee, tags, comments,
            deps.
        jira_snapshot: Dict of {jira_key: {fields...}} from the fetcher.
        binding_store: A BindingStore instance providing get_jira_key(local_id),
            is_bound(local_id).
        excluded_statuses: Statuses to skip (default: {"archived", "deleted"}).
        local_label_intent: Optional dict mapping local_id -> "ever-seen" tag
            set (from ``local_label_intent.compute_label_intent_map``). When
            provided, gates outbound label REMOVE emission to only labels
            local user actually had at some point (bug a06c — prevents
            spurious REMOVEs cancelling legitimate inbound ADDs under the
            PR #457 local-wins bidir suppression contract). Tickets missing
            from the map receive an empty intent set, which is the lazy
            first-pass safety mode (suppress all REMOVEs for that ticket).
            Legacy callers omit this argument and retain the pre-fix behavior.
        client: Optional AcliClient (or compatible duck-typed object). When
            provided, used by ``_diff_comments`` to fetch live Jira comment
            state for tickets whose snapshot entry lacks a ``comment`` field
            (the live Jira search shape). When None, _diff_comments skips
            comment mutations for such tickets rather than emitting blind adds
            (bug 4292 safety invariant).
        pass_id: This pass's monotonic id (``%Y-%m-%dT%H-%M-%S`` timestamp).
            Used as the rotation bookkeeping key for bound-but-absent direct
            GETs (bug 1e08) — recorded via ``binding_store.set_last_get`` so the
            least-recently-GET'd absent keys are serviced first next pass.
        absent_alive_fields: Optional out-param dict. When provided, each
            bound-but-absent jira_key that the bounded direct GET resolves as
            ALIVE (HTTP 200) this pass is recorded as
            ``absent_alive_fields[jira_key] = <raw fields dict>``. This is the
            inbound-direction GET-sharing seam (bug 0702-3b6d-c1db-4ed3): the
            reconcile orchestrator merges these entries into the snapshot it
            hands to the inbound differ, so each out-of-window-alive key is
            GET'd exactly ONCE per pass and BOTH directions consume the result.
            404/deleted and transport-error keys are deliberately NOT recorded
            (a gone issue must not be inbound-mirrored; retirement stays owned
            by the outbound 404-counter). None → no recording (legacy callers).

    Returns:
        List of OutboundMutation objects describing changes to push to Jira.
    """
    if excluded_statuses is None:
        excluded_statuses = {"archived", "deleted"}

    mutations: list[OutboundMutation] = []

    # Bug 1e08 — rotation pre-selection for bound-but-absent direct GETs.
    # Compute the set of jira_keys eligible for a GET this pass: bound,
    # non-pending, non-retired, and ABSENT from this pass's search snapshot.
    # Select the K least-recently-GET'd (sorted by last_get_pass ascending; the
    # "" never-GET'd sentinel sorts first), bounding servicing of every absent
    # key to <= ceil(N/K) passes (anti-starvation, I3/I4).
    # Deletion-probe budget (GET probes to confirm a Jira issue is really deleted),
    # resolved through the typed config: [tool.rebar.reconciler].deletion_probe_limit
    # (default 20), overridden by env REBAR_RECONCILER_DELETION_PROBE_LIMIT (deprecated
    # alias RECONCILER_ABSENT_GET_BUDGET), then `rebar -c reconciler.deletion_probe_limit=…`.
    # An unreadable config falls back to the default rather than failing the pass.
    from rebar.config import ConfigError, load_config

    try:
        _budget = load_config().reconciler.deletion_probe_limit
    except ConfigError:
        _budget = _DEFAULT_ABSENT_GET_BUDGET
    _absent_candidates: list[str] = []
    _seen_absent: set[str] = set()
    # Without a client we cannot direct-GET, so there is nothing to select.
    for _t in local_tickets if client is not None else ():
        if _t.get("status", "") in excluded_statuses:
            continue
        _lid = _t.get("ticket_id")
        if not _lid:
            continue
        _jk = binding_store.get_jira_key(_lid)
        if _jk is None or _jk in jira_snapshot or _jk in _seen_absent:
            continue
        if _is_retired(binding_store, _jk):
            continue
        _seen_absent.add(_jk)
        _absent_candidates.append(_jk)
    _absent_candidates.sort(key=lambda k: _last_get_pass(binding_store, k))
    _selected_for_get_this_pass: set[str] = set(_absent_candidates[:_budget])

    # Hierarchy pre-check map (ticket 8b25): {local_id → ticket_type}. Used to
    # suppress parent diffs whose resolved parent is a non-epic — Jira only
    # permits Epic parents on this project, so emitting such a parent mutation
    # would re-fail (HTTP 400) every pass. Cheap O(n) build over local state.
    local_ticket_types: dict[str, str] = {
        t["ticket_id"]: t.get("ticket_type", "") for t in local_tickets if t.get("ticket_id")
    }

    for ticket in local_tickets:
        status = ticket.get("status", "")
        if status in excluded_statuses:
            continue

        local_id = ticket["ticket_id"]
        jira_key = binding_store.get_jira_key(local_id)

        if jira_key is None:
            # Unbound -> outbound create
            # ticket 929a: for new issues the Jira side has no labels yet,
            # so the annotation label only needs an ADD (never a REMOVE).
            annotation_mutations = _diff_status_annotation_labels(
                local_status=status,
                jira_labels=[],
            )
            mutations.append(
                OutboundMutation(
                    local_id=local_id,
                    jira_key=None,
                    action="create",
                    fields=_map_local_to_jira_fields(
                        ticket,
                        binding_store=binding_store,
                        local_ticket_types=local_ticket_types,
                    ),
                    comments=_map_comments_for_create(ticket),
                    labels=(
                        [
                            {"action": "add", "label": t}
                            for t in sorted(ticket.get("tags", []))
                            if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
                        ]
                        + annotation_mutations
                    ),
                    links=[],  # links resolved after all creates
                )
            )
        else:
            # Bound -> compare fields, emit update if different.
            #
            # Bug 1e08-1a35-0267-4ca6: discriminate on MEMBERSHIP, not value.
            # A bound key ABSENT from this pass's search snapshot must NOT diff
            # against ``{}`` (that re-emits every field every pass). Two absence
            # sub-classes: (a) deleted → direct GET 404; (b) status=Done beyond
            # _DONE_RECENT_CAP → alive (HTTP 200) but absent from the search
            # snapshot. We resolve the real fields via a bounded direct GET.
            if jira_key in jira_snapshot:
                # EXISTING path — key present in the search snapshot.
                jira_fields = jira_snapshot[jira_key]
                comment_snapshot = jira_snapshot
            else:
                # Bound-but-absent from THIS pass's working set.
                if client is None:
                    # No client → we cannot direct-GET to resolve the absence.
                    # Skip (defer) rather than diff against {} — that re-emit
                    # against an empty dict was the original defect (bug 1e08).
                    # Mirrors the _diff_comments no-client safety pattern.
                    continue
                if _is_retired(binding_store, jira_key):
                    continue  # known-dead; no GET, no emit (budget preserved)
                if jira_key not in _selected_for_get_this_pass:
                    continue  # not selected this pass → DEFERRED (no emit)

                fields = _safe_get_issue(client, jira_key)
                # Record the GET regardless of outcome (rotation bookkeeping).
                _set_last_get(binding_store, jira_key, pass_id)

                if fields is _DELETED:
                    # HTTPError 404 — issue gone. Bump the consecutive-404
                    # counter (may retire at GRACE). Emit nothing.
                    _note_absent(binding_store, jira_key)
                    continue
                if fields is _TRANSPORT_ERROR:
                    # Non-404 HTTPError / URLError / timeout — transient.
                    # Emit nothing, warn, defer; counter untouched.
                    print(  # noqa: T201
                        f"WARNING: outbound_differ: direct GET for bound-but-absent "
                        f"{jira_key!r} failed (transport error). Deferring this "
                        f"key's sync to a later pass (no mutation emitted).",
                        file=sys.stderr,
                    )
                    continue

                # HTTP 200 — issue is alive (out-of-window). Reset the absence
                # counter and build a one-key overlay so the SAME diff path runs.
                _clear_absent(binding_store, jira_key)
                jira_fields = fields
                comment_snapshot = dict(jira_snapshot)
                comment_snapshot[jira_key] = fields
                # Bug 0702: share this alive GET result with the inbound differ
                # so the out-of-window key is mirrored Jira→local without a
                # second GET. Only the alive (200) case is recorded — 404 and
                # transport errors are intentionally left out so a gone issue is
                # never inbound-mirrored (retirement stays outbound-owned).
                if absent_alive_fields is not None:
                    absent_alive_fields[jira_key] = fields

            changed = _diff_fields(
                ticket,
                jira_fields,
                binding_store=binding_store,
                local_ticket_types=local_ticket_types,
            )
            # Comments use the resolved snapshot (the one-key overlay for the
            # bounded-GET path) so the GET's native fields.comment.comments is
            # consulted with NO second network call (C3).
            comment_mutations = _diff_comments(ticket, jira_key, comment_snapshot, client=client)
            # bug a06c: intent-gated REMOVE. When local_label_intent is
            # provided but lacks an entry for this local_id, fall back to
            # an empty intent set (lazy first-pass safety: suppresses all
            # REMOVEs for tickets we have no event-log evidence for).
            intent_set: set[str] | None = None
            if local_label_intent is not None:
                intent_set = local_label_intent.get(local_id, set())
            label_mutations = _diff_labels(ticket, jira_fields, intent_set)
            # ticket 929a: status annotation labels (rebar-status:blocked/cancelled)
            # are managed separately from user tags (excluded from _diff_labels via
            # _EXCLUDED_PREFIXES). Compute and merge annotation mutations here.
            annotation_mutations = _diff_status_annotation_labels(
                local_status=status,
                jira_labels=list(jira_fields.get("labels") or []),
            )
            label_mutations = label_mutations + annotation_mutations
            # story 25ae Cycle 2: diff local deps -> Jira issuelinks (ADD-only,
            # deduped against the snapshot's existing issuelinks so an
            # already-present link emits nothing — no per-pass churn).
            link_mutations = _diff_links(ticket, jira_fields, binding_store)

            if changed or comment_mutations or label_mutations or link_mutations:
                # Sync-hardening P5 / bug 57d1 diagnosis enabler: emit a
                # one-line CHANGED-FIELD BREADCRUMB whenever a bound key gets
                # an outbound UPDATE carrying field diffs. Logs the changed
                # FIELD NAMES only (never values — descriptions/assignees may
                # be large or sensitive) so a re-emitting (non-converging)
                # field becomes visible in CI logs without live Jira creds.
                # The field list is the keys of the same `changed` dict
                # _diff_fields already computed — no recomputation. Comment-
                # only / label-only updates carry no field diff, so `changed`
                # is empty and the breadcrumb is skipped (keeps stderr quiet
                # for the common comment-mirror case).
                print(  # noqa: T201
                    f"RECON: outbound_update key={jira_key} "
                    f"changed=[{','.join(sorted(changed))}] "
                    f"comments={len(comment_mutations)} "
                    f"labels={len(label_mutations)} "
                    f"links={len(link_mutations)}",
                    file=sys.stderr,
                )
                mutations.append(
                    OutboundMutation(
                        local_id=local_id,
                        jira_key=jira_key,
                        action="update",
                        fields=changed,
                        comments=comment_mutations,
                        labels=label_mutations,
                        links=link_mutations,
                    )
                )

    return mutations
