"""Identity-mapping assignee resolution (264f) for the outbound differ.

This leaf module owns the flow-layer logic that turns a local assignee into a
Jira accountId for the outbound sync. It was extracted from ``outbound_differ``
for module size; the outbound differ re-exports every symbol here so that
``outbound_differ.<name>`` attribute access keeps resolving for callers and the
existing test suites (notably ``test_identity_264f_resolve.py``, which pins
``_bootstrap_account_id_via_user_search``).

The engine (acli.py) must NOT reach into rebar core, so ALL identity/mapping
resolution lives here in the flow layer and hands acli only a resolved
accountId string + a bool. These helpers default repo_root to rebar core's
config.repo_root() internally (per ddbe's identity module) and NEVER raise —
rebar core is imported lazily inside each function so this stays a leaf that
does not import from ``outbound_differ`` (no back-reference).

Resolution order for :func:`_resolve_assignee_account_id`:

1. the identity-mapping fast path (``jira_account_id``) — a trusted stored
   accountId;
2. the transient ``/user/search`` bootstrap by email (never persisted); and
3. the legacy ``validate_assignee_exists`` assignable-search string match.
"""

from __future__ import annotations

from typing import Any

# acli_rest exposes the /user/search email→accountId helper under this name; the
# alias list tolerates a differently-named stub client in tests. First present
# attribute wins.
_USER_SEARCH_METHODS: tuple[str, ...] = (
    "search_user_by_email",
    "find_account_id_by_email",
    "user_search_account_id",
)


def _identity_jira_account_id(assignee: str) -> str | None:
    """rebar core's ``identity.jira_account_id`` (local assignee → Jira accountId),
    or ``None`` on any miss/import failure. The trusted, stored-mapping fast path."""
    try:
        from rebar._commands import identity as _identity

        return _identity.jira_account_id(assignee)
    except Exception:  # noqa: BLE001 — resolution is best-effort; degrade to string-match
        return None


def _identity_email(assignee: str) -> str | None:
    """rebar core's ``identity.identity_email`` for the ``/user/search`` bootstrap,
    or ``None`` on any miss/import failure."""
    try:
        from rebar._commands import identity as _identity

        return _identity.identity_email(assignee)
    except Exception:  # noqa: BLE001 — best-effort; degrade without failing
        return None


def _bootstrap_account_id_via_user_search(assignee: str, client: Any) -> str | None:
    """Best-effort TRANSIENT ``/user/search`` bootstrap: obtain the assignee's email
    (identity email, else the assignee itself if it already looks like an email) and
    ask the client for the exact-email accountId. ``None`` on no client / no email /
    miss / ambiguity / any error — the caller then degrades to the string-match path.
    The result is used for THIS run only (never persisted to ``mappings``)."""
    if client is None:
        return None
    email = _identity_email(assignee)
    if not email:
        email = assignee if isinstance(assignee, str) and "@" in assignee else None
    if not email:
        return None
    for name in _USER_SEARCH_METHODS:
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            acct = fn(email)
        except Exception:  # noqa: BLE001 — best-effort bootstrap; degrade on any transport error
            return None
        return acct or None
    return None


def _resolve_assignee_account_id(
    assignee: str, jira_key: str, client: Any
) -> tuple[str | None, bool, bool]:
    """Resolve a local assignee to ``(accountId|None, authoritative, is_account_id)``.

    Order: (1) the identity-mapping fast path (``jira_account_id``) — a trusted stored
    accountId; (2) the transient ``/user/search`` bootstrap by email; (3) the legacy
    ``validate_assignee_exists`` assignable-search string match. ``is_account_id`` is
    ``True`` when the returned value is an ALREADY-RESOLVED accountId (paths 1 & 2) so
    the applier/acli submits it directly and skips the assignable search. ``authoritative``
    is ``True`` when the outcome is trustworthy (a resolved accountId, or a definitive
    ``AssigneeNotFoundError`` → unassigned); ``False`` only when the mapping is unknown
    (no client, or a transient lookup error) so the caller keeps the legacy string match."""
    acct = _identity_jira_account_id(assignee)
    if acct:
        return (acct, True, True)
    acct = _bootstrap_account_id_via_user_search(assignee, client)
    if acct:
        return (acct, True, True)
    if client is None or not jira_key:
        return (None, False, False)
    try:
        acct = client.validate_assignee_exists(assignee, issue_key=jira_key)
        return (acct or None, True, False)
    except Exception as exc:  # noqa: BLE001 — classify the resolution outcome
        # AssigneeNotFoundError ⇒ definitively unassignable (→ unassigned).
        # Any other (transient/transport) error ⇒ unknown → string-match fallback.
        if type(exc).__name__ == "AssigneeNotFoundError":
            return (None, True, False)
        return (None, False, False)
