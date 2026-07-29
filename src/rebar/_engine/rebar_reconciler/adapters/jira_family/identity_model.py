"""The ``UserIdentityModel`` contract (story J4, epic e369).

User identity is one of the three real Cloud/Data-Center differences: Jira Cloud
identifies users by opaque ``accountId`` (username and userkey were removed for
GDPR), while Data Center identifies them by ``name``.

This module pins that difference as a contract with two operations:

* ``resolve(local_value, remote_identity)`` -> ``(value, authoritative, is_account_id)``
  — the 3-state account-resolution fast-path the core diff consults before emitting
  an assignee change (ADR 0035 §(d) canonical-comparison corollary);
* ``to_payload(value)`` -> the deployment's assignee field shape.

Both implementations take their lookup resolver as an EXPLICIT constructor
parameter. Nothing is discovered with ``getattr``: a resolver that silently goes
missing would make every resolution non-authoritative, and a permanently
non-authoritative assignee makes the outbound diff re-emit a change it can never
converge — the churn class epic ``ace2`` exists to fix (PR #120's defect, which
this contract makes unwritable: there is no optional attribute to be missing,
only a required constructor parameter).

The 3-state resolution state machine is Jira Cloud's pre-existing
``JiraBackend.resolve_assignee`` behaviour (ticket 625b; 264f semantics),
reproduced here verbatim and shared between both deployments — parameterized only
by the remote-identity key it compares against (``account_id`` for Cloud,
``name`` for DC) and by whether a resolved value is ever an accountId (Cloud only;
DC has no accountId at all, so it is always ``False``). That single shared
implementation is the point of this story: the state machine exists ONCE, not
copy-pasted per deployment (the mistake PR #120 made).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

# A local value -> (resolved_value | None, authoritative, is_account_id) callable,
# bound by the caller to whatever remote key/session the current resolution needs.
Resolver = Callable[[str], tuple[Any, bool, bool]]


class UserIdentityModel(Protocol):
    """The vendor-neutral user-identity contract the core diff consults.

    ``resolve`` implements the 3-state account-resolution fast-path (ADR 0035
    §(d) canonical-comparison corollary); ``to_payload`` shapes a resolved value
    into the deployment's assignee field for a create/update mutation.
    """

    def resolve(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        """Return ``(value, authoritative, is_account_id)`` for ``local_value``."""
        ...

    def to_payload(self, value: str) -> dict[str, Any]:
        """Shape a resolved value into this deployment's assignee field."""
        ...


def _resolve(
    resolver: Resolver | None,
    local_value: str,
    remote_identity: dict[str, Any] | None,
    *,
    remote_key: str,
    force_is_account_id_false: bool,
    trust_resolved_on_mismatch: bool,
) -> tuple[Any, bool, bool]:
    """The shared 3-state resolution state machine (Cloud's ``resolve_assignee``,
    reproduced verbatim), parameterized by:

    * ``remote_key`` — the remote-identity key to compare against
      (``account_id`` for Cloud, ``name`` for DC);
    * ``force_is_account_id_false`` — DC has no accountId at all, so its returned
      flag is always ``False`` regardless of what the resolver reports;
    * ``trust_resolved_on_mismatch`` — whether an authoritative, resolvable, but
      MISMATCHED lookup should emit the freshly resolved value rather than the
      local string. Cloud only does this when the resolver flags the value as an
      accountId (its "fast path"); DC's ``name`` IS the identity, so a resolved-
      but-mismatched DC lookup always emits the resolved username.

    Returns ``(value, authoritative, is_account_id)``:

    * empty/None ``local_value`` -> ``("", False, False)`` (nothing to resolve —
      non-authoritative, no live account search);
    * no injected resolver (fixture path) -> ``(local_value, False, False)`` so the
      caller keeps the legacy permissive string match;
    * authoritative + the resolved value matches the remote identity's value ->
      ``(None, True, …)`` (the CONVERGED signal — caller emits nothing);
    * authoritative + unmappable (no resolved value) -> ``("", True, False)``
      (desired unassigned);
    * authoritative resolved-and-trusted -> ``(resolved, True, is_account_id)``
      (Cloud's accountId fast-path, or DC's direct-name path);
    * authoritative resolvable-but-untrusted-mismatch ->
      ``(local_value, True, False)``.
    """
    if not local_value:
        return ("", False, False)
    if resolver is None:
        return (local_value, False, False)
    resolved, authoritative, is_account_id = resolver(local_value)
    if force_is_account_id_false:
        is_account_id = False
    if not authoritative:
        return (local_value, False, is_account_id)
    remote_value = (remote_identity or {}).get(remote_key)
    if (resolved or None) == (remote_value or None):
        return (None, True, is_account_id)  # converged
    if resolved is None:
        return ("", True, False)  # unmappable → desired unassigned
    if trust_resolved_on_mismatch or is_account_id:
        return (resolved, True, is_account_id)  # fast-path
    return (local_value, True, False)  # resolvable but mismatched → emit local string


class AccountIdIdentity:
    """Jira Cloud's user identity: the opaque ``accountId``.

    Reproduces ``JiraBackend.resolve_assignee``'s CURRENT behavior verbatim; the
    Cloud backend delegates to this class rather than carrying its own copy of
    the state machine.
    """

    def __init__(self, *, resolver: Resolver | None) -> None:
        self._resolver = resolver

    def resolve(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        return _resolve(
            self._resolver,
            local_value,
            remote_identity,
            remote_key="account_id",
            force_is_account_id_false=False,
            trust_resolved_on_mismatch=False,
        )

    def to_payload(self, value: str) -> dict[str, Any]:
        return {"accountId": value}


class NameIdentity:
    """Jira Data Center's user identity: the ``name`` (username).

    Same shared state machine as ``AccountIdIdentity``, compared against the
    remote identity's ``name`` instead of ``account_id``. ``is_account_id`` is
    always ``False`` — DC has no accountId at all, so the core diff's 3-state
    fast-path never treats a DC value as one.
    """

    def __init__(self, *, resolver: Resolver | None) -> None:
        self._resolver = resolver

    def resolve(
        self, local_value: str, remote_identity: dict[str, Any] | None
    ) -> tuple[Any, bool, bool]:
        return _resolve(
            self._resolver,
            local_value,
            remote_identity,
            remote_key="name",
            force_is_account_id_false=True,
            trust_resolved_on_mismatch=True,
        )

    def to_payload(self, value: str) -> dict[str, Any]:
        return {"name": value}
