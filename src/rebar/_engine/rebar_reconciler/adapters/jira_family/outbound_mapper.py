"""The shared ``map_fields_to_remote`` implementation (story J3, epic e369).

The Jira-family backends (Cloud, and a future Data Center) all map a CANONICAL
changed-fields dict (local field names -> local values) to the same shape of
vendor mutation fields — field-name reconciliation (local ``title`` -> Jira
``summary``), value mapping (``status``/``priority`` -> the Jira name), and
rich-text fitting for ``description``. The only thing that differs between
Cloud and DC is the rich-text codec, so this module takes a ``RichTextCodec``
as a constructor dependency and is the SOLE implementation: PR #120's mistake
was copying this method per adapter and changing one line, which let the other
thirty drift. ``adapters/jira/backend.py``'s ``_JiraOutbound.map_fields_to_remote``
now delegates here instead of carrying a second real body.

Imports the ``RichTextCodec`` Protocol from ``rich_text.py`` and the value maps
from ``value_maps.py`` — nothing from ``adapters/jira/`` (this package's
one-way dependency rule; see ``adapters/jira_family/__init__.py``).
"""

from __future__ import annotations

import sys
from typing import Any

from rebar_reconciler.adapters.jira_family.rich_text import RichTextCodec
from rebar_reconciler.adapters.jira_family.value_maps import (
    LOCAL_PRIORITY_TO_JIRA,
    LOCAL_STATUS_TO_JIRA,
)


def reset_drift_warnings() -> None:
    """Clear the per-process drift-warning dedupe set.

    The drift alert in :func:`resolve_outbound_status` is emitted at most once per
    distinct local status per process, so a persistently drifting status does not
    re-flood stderr on every mapper call (every reconcile pass). Tests reset the set to
    assert emission counts deterministically."""
    _DRIFT_WARNED.clear()


# Per-process set of local statuses already warned about (bug: unthrottled drift alert
# re-printed on every reconcile pass). Deduped by status name so a stuck status warns
# once, not once-per-call.
_DRIFT_WARNED: set[str] = set()


def resolve_outbound_status(value: Any, status_map: dict[str, str] | None) -> str | None:
    """Resolve a local status to its Jira target under map-or-drift semantics.

    ``status_map`` is the effective forward map (``config.effective_status_map``);
    ``None`` falls back to the built-in ``LOCAL_STATUS_TO_JIRA``. When the local
    ``value`` has NO target (absent / dropped as ``SKIP`` upstream) this returns
    ``None`` — the caller OMITS the ``status`` field entirely (Jira left unchanged),
    never coercing to ``"To Do"`` — and emits a non-fatal drift warning to stderr
    naming the status. The warning is deduped per-status per-process (see
    :func:`reset_drift_warnings`) so a persistently drifting status does not re-print
    every pass. Shared by the UPDATE mapper and both CREATE paths so the map-or-drift
    rule has ONE implementation."""
    effective = LOCAL_STATUS_TO_JIRA if status_map is None else status_map
    target = effective.get(value)
    if target is None:
        if value not in _DRIFT_WARNED:
            _DRIFT_WARNED.add(value)
            print(
                f"rebar-reconciler: local status {value!r} has no Jira target in the "
                "effective status map; leaving the Jira status field unchanged "
                "(map-or-drift).",
                file=sys.stderr,
            )
        return None
    return target


class OutboundFieldMapper:
    """Maps a canonical changed-fields dict to vendor mutation fields.

    Constructed with the deployment's ``RichTextCodec`` (``AdfCodec`` for Cloud,
    ``WikiTextCodec`` for Data Center) so the ADF-vs-wiki difference is a
    parameter rather than a branch or a duplicated method body.
    """

    def __init__(self, codec: RichTextCodec) -> None:
        self._codec = codec

    @property
    def codec(self) -> RichTextCodec:
        """The deployment's ``RichTextCodec`` (emersed-specific-mutt).

        Exposed so the comment-diff path can normalize its LOCAL dedup key with the
        SAME codec this mapper renders descriptions through — one codec instance per
        backend, never a second one built at the diff site."""
        return self._codec

    def map_fields_to_remote(
        self,
        changed: dict[str, Any],
        ticket: dict[str, Any] | None = None,
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        status_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map a CANONICAL changed-fields dict (local field names -> local values) to
        the vendor-shaped mutation fields, at the emission boundary (ticket 625b).

        Field-name reconciliation (local ``title`` -> Jira ``summary``) and value
        mapping (``status``/``priority`` -> the Jira name; ``description`` fitted
        via the injected ``RichTextCodec``) reuse the shared local->Jira value
        maps. ``status_map`` is the effective per-project forward map
        (``config.effective_status_map``); ``None`` falls back to the built-in
        ``LOCAL_STATUS_TO_JIRA``. A local status with NO target (map-or-drift) causes
        the ``status`` field to be OMITTED entirely — never coerced — with a
        non-fatal warning to stderr. ``assignee``/``parent``/``reporter`` values are
        already resolved by the core diff and pass through unchanged, as does the
        ``_assignee_is_account_id`` dispatch sentinel."""
        out: dict[str, Any] = {}
        for name, value in changed.items():
            if name == "title":
                out["summary"] = value
            elif name == "description":
                # The length fit runs FIRST, then soft-wrap normalization — read the
                # call below inside-out: ``normalize_outbound(fit_outbound(value))``.
                #
                # This ordering is load-bearing, and the comment that used to sit here
                # described it BACKWARDS ("normalization runs BEFORE the length fit"),
                # which is why it is spelled out now: ``fit_outbound`` has to measure
                # the ADF the send path actually serializes, so it must see the value
                # before normalization shrinks it. The body Jira then stores is read
                # back through the codec's inbound decode — i.e. normalized — so
                # composing in this order makes it its own fixed point (both halves are
                # idempotent and normalization only ever shrinks the ADF). That is what
                # keeps the send value and every description comparison routing through
                # this port — including ``reconcile_check`` — on one convergent value.
                # Swapping the two re-introduces a diff that never converges.
                out["description"] = (
                    self._codec.normalize_outbound(self._codec.fit_outbound(value))
                    if isinstance(value, str)
                    else value
                )
            elif name == "status":
                target = resolve_outbound_status(value, status_map)
                if target is not None:
                    out["status"] = target
            elif name == "priority":
                out["priority"] = LOCAL_PRIORITY_TO_JIRA.get(value, "Medium")
            else:
                # assignee / parent / reporter (already resolved) + the
                # _assignee_is_account_id sentinel pass through by their own name.
                out[name] = value
        return out
