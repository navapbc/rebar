"""The shared ``map_fields_to_remote`` implementation (story J3, epic e369).

The Jira-family backends, Cloud and Data Center, map a CANONICAL
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
    LOCAL_TYPE_TO_JIRA,
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


def merge_create_defaults(
    create_defaults: dict[str, str] | None, result: dict[str, Any]
) -> dict[str, Any]:
    """Merge per-project ``create_defaults`` UNDER a computed CREATE body.

    ``create_defaults`` (``config.effective_create_defaults``) is a str-valued map of
    vendor field name -> literal value for required-beyond-baseline Jira fields. The
    baseline computed ``result`` ALWAYS wins on collision — a default that names a field
    the mapper already computes (``summary``/``priority``/...) never overrides it — so a
    default only ever INJECTS a field the baseline body omits. CREATE-only: the UPDATE
    mapper applies no defaults. Shared by both CREATE paths (Cloud + DC) so the merge has
    ONE implementation."""
    return {**(create_defaults or {}), **result}


def resolve_outbound_priority(value: Any, priority_map: dict[str, str] | None) -> str | None:
    """Resolve a local priority to its Jira target NAME under map-or-drift semantics.

    ``priority_map`` is the effective per-project forward map
    (``config.effective_priority_map``, str-keyed); ``None`` falls back to a str-keyed VIEW
    of the built-in ``LOCAL_PRIORITY_TO_JIRA`` (which is INT-keyed here — this module must
    not import ``config``, so the str-keyed fallback is built locally, not imported). The
    lookup is always by ``str(value)`` so an int local priority and a str config key meet.

    When the local ``value`` has NO target this returns ``None`` — the caller OMITS the
    ``priority`` field entirely (Jira left unchanged), never coercing to ``"Medium"`` (this
    REPLACES the old unconditional ``"Medium"`` fallback) — and emits a non-fatal drift
    warning to stderr naming the priority. The warning reuses the SAME ``_DRIFT_WARNED``
    dedupe set (and :func:`reset_drift_warnings`) as :func:`resolve_outbound_status`, keyed
    by ``str(value)``, so a persistently drifting priority warns at most once per process.
    Shared by the UPDATE mapper and both CREATE paths so the rule has ONE implementation."""
    effective = (
        {str(k): v for k, v in LOCAL_PRIORITY_TO_JIRA.items()}
        if priority_map is None
        else priority_map
    )
    key = str(value)
    target = effective.get(key)
    if target is None:
        if key not in _DRIFT_WARNED:
            _DRIFT_WARNED.add(key)
            print(
                f"rebar-reconciler: local priority {value!r} has no Jira target in the "
                "effective priority map; leaving the Jira priority field unchanged "
                "(map-or-drift).",
                file=sys.stderr,
            )
        return None
    return target


def resolve_outbound_type(value: Any, type_map: dict[str, str] | None) -> str:
    """Resolve a local ticket type to its Jira issue-type NAME.

    ``type_map`` is the effective per-project forward map (``config.effective_type_map``);
    ``None`` falls back to the built-in ``LOCAL_TYPE_TO_JIRA``. Unlike ``status`` (which is
    OMITTED on a miss — a create carries no ``status``), ``issuetype`` is MANDATORY on a
    create, so a type with no target falls back to ``"Task"`` — preserving the built-in
    ``.get(ticket_type, "Task")`` behaviour EXACTLY when ``type_map`` is ``None``. The
    up-front ``config.assert_type_decisions_complete`` gate already fail-closes on an
    undecided syncable type before any mutation is built, so on the configured path this
    fallback is only reached by a type the operator deliberately left unmapped. Shared by
    both CREATE paths (Cloud + DC) so the rule has ONE implementation."""
    effective = LOCAL_TYPE_TO_JIRA if type_map is None else type_map
    return effective.get(value, "Task")


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
        priority_map: dict[str, str] | None = None,
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
        non-fatal warning to stderr. ``priority_map`` is the effective per-project
        forward map (``config.effective_priority_map``); ``None`` falls back to the
        built-in map, and a local priority with NO target is likewise OMITTED (map-or-drift),
        never coerced to ``"Medium"``. ``assignee``/``parent``/``reporter`` values are
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
                # this port on one convergent value.
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
                target = resolve_outbound_priority(value, priority_map)
                if target is not None:
                    out["priority"] = target
            else:
                # assignee / parent / reporter (already resolved) + the
                # _assignee_is_account_id sentinel pass through by their own name.
                out[name] = value
        return out
