"""Outbound comment-diff cluster for bidirectional Jira sync.

The cohesive comment-diff seam extracted from ``outbound_differ.py`` (it grew
past the module-size soft cap; the comment logic is self-contained). Owns the
local→Jira comment comparison and the create-path comment mapping:

    - ``_diff_comments`` — compare local comments to Jira's, emit "add" mutations
      for the ones not already mirrored (the bug-4292 live-fetch safety invariant
      lives here).
    - ``_map_comments_for_create`` — map all local comments to outbound "add"
      mutations for a brand-new issue.
    - ``_normalize_comment_body`` / ``_decorate_outbound_comment`` — the rich-text→text
      normalisation and the RECONCILER_MARKER loop-breaker decoration (bug 85a1 /
      Gap 1).
    - ``_is_machine_marker_comment`` — the bridge-internal machine-comment
      exclusion (bug 6afc).

``compute_outbound_mutations`` (in ``outbound_differ``) imports this module; the
dependency is one-way.

Ticket 21ca: the ADF decode + comment-limit truncation are routed through the
Backend port (``InboundMapper.normalize_rich_text`` / ``FieldSanitizer.fit_comment``)
instead of this module's own lazy vendor loaders — this module now carries NO
``"rebar_reconciler.adapters.jira"`` literal. ``_normalize_comment_body`` and
``_diff_comments`` accept an optional injected ``inbound_mapper``/``sanitizer``
(mirroring ``outbound_differ.compute_outbound_mutations``'s injection seam) that
default to ``None`` and are resolved lazily via ``select_backend(load_config())``
INSIDE the function body — never at import time — because this module is
spec-loaded standalone in tests, where package-relative config resolution may not
be available at import.
"""

from __future__ import annotations

import sys
from typing import Any

# Sentinel: presence of the "comment" key in a snapshot entry confirms the
# snapshot carries real comment data (fixture/synthetic path). Absence means
# the entry came from a live Jira search result, which never includes comments.
_COMMENT_FIELD_KEY = "comment"

# Bug 85a1 (Gap 1): marker token embedded in every outbound comment body so the
# inbound differ can identify and filter our own echoes when the reconciler reads
# Jira comments back on the next pass. Without the marker every outbound comment
# would re-appear inbound as a "new Jira comment" and the bridge would loop. Kept
# identical here and in inbound_differ.py so both directions agree on the
# loop-breaker pattern.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


def _resolve_inbound_mapper(inbound_mapper: Any | None) -> Any:
    """Resolve the injected ``InboundMapper``, falling back to the configured
    backend (ticket 21ca; mirrors ``outbound_differ.compute_outbound_mutations``'s
    injection seam). Resolved LAZILY here — never at import time — because this
    module is spec-loaded standalone in tests, where ``select_backend(load_config())``
    may not resolve config."""
    if inbound_mapper is not None:
        return inbound_mapper
    from rebar.config import load_config
    from rebar_reconciler._backend_registry import select_backend

    return select_backend(load_config()).inbound


def _resolve_sanitizer(sanitizer: Any | None) -> Any:
    """Resolve the injected ``FieldSanitizer``, falling back to the configured
    backend (ticket 21ca; same injection seam as :func:`_resolve_inbound_mapper`)."""
    if sanitizer is not None:
        return sanitizer
    from rebar.config import load_config
    from rebar_reconciler._backend_registry import select_backend

    return select_backend(load_config()).sanitizer


def _map_comments_for_create(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    """Map all local comments to outbound create mutations.

    Every outbound body is decorated with the reconciler marker (Gap 1
    loop-breaker) so inbound passes can identify our own echoes.
    """
    comments = ticket.get("comments", [])
    return [
        {"action": "add", "body": _decorate_outbound_comment(c.get("body", ""))} for c in comments
    ]


def _normalize_comment_body(body: Any, inbound_mapper: Any | None = None) -> str:
    """Coerce a comment body to a comparable plain-text string.

    Jira comments are returned with ``body`` as an Atlassian Document Format
    (ADF) dict (``{"type": "doc", ...}``). Local comments store ``body`` as a
    plain string. Direct dict-vs-string comparison always reports them as
    different — driving spurious duplicate pushes (Phase 2 verify-no-
    duplicate-comments: "found 2 copies") and the dict-as-key crash in
    ``_diff_comments`` (Phase 3+ "unhashable type: 'dict'" when an ADF body
    flows into a ``set[str]`` insertion).

    Normalize via the Backend port's ``InboundMapper.normalize_rich_text`` (ticket
    21ca; Jira: ``adf.adf_to_text``) so the canonical comparison is on text. Bug
    85a1. The reconciler marker token (Gap 1) is also stripped so dedup compares
    the *user content* on both sides — without the strip, a previously-pushed
    Jira body ``"hello\\n\\n<marker>"`` would never match a local ``"hello"`` and
    the diff would re-emit the same comment.

    ``inbound_mapper``: the injected Backend-port ``InboundMapper`` (ticket 21ca);
    ``None`` resolves the configured backend's mapper via :func:`_resolve_inbound_mapper`.
    """
    text = _resolve_inbound_mapper(inbound_mapper).normalize_rich_text(body)
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
    *,
    inbound_mapper: Any | None = None,
    sanitizer: Any | None = None,
) -> list[dict[str, Any]]:
    """Compare local comments to Jira comments. Return mutations for new comments.

    Matching rule: emit a comment "add" only for local comment bodies NOT
    already present in Jira, after normalising both sides via
    :func:`_normalize_comment_body` (rich-text→text conversion + RECONCILER_MARKER
    strip + whitespace strip). Body equality after normalisation → skip
    (already mirrored); otherwise emit with outbound decoration.

    ``inbound_mapper``/``sanitizer``: the injected Backend-port ``InboundMapper``/
    ``FieldSanitizer`` (ticket 21ca); ``None`` resolves the configured backend via
    :func:`_resolve_inbound_mapper`/:func:`_resolve_sanitizer`. Threaded from
    ``outbound_differ.compute_outbound_mutations`` (which already holds the backend).

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
    # Resolve the injected Backend-port members ONCE per call (ticket 21ca) rather
    # than re-resolving per comment below.
    inbound_mapper = _resolve_inbound_mapper(inbound_mapper)
    sanitizer = _resolve_sanitizer(sanitizer)

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
        # Snapshot-carried path (fixtures, synthetic, or the bulk get_comment_map
        # enrichment). Normally used directly — do NOT call the client.
        comment_field = jira_issue[_COMMENT_FIELD_KEY]
        embedded = comment_field.get("comments", []) if isinstance(comment_field, dict) else []
        total = comment_field.get("total") if isinstance(comment_field, dict) else None
        jira_comments: list = embedded if isinstance(embedded, list) else []
        # Truncation guard (bug 1f3d): the bulk ``/search/jql`` enrichment
        # (get_comment_map) CAPS embedded comments at ~20 per issue while reporting
        # the true ``total``. Deduping against a truncated set re-posts every comment
        # past the cap — the exact 5000-comment inflation this fix exists to stop
        # (the per-ticket get_comments pagination fix alone does NOT close it, because
        # the SNAPSHOT path is production's primary source). When the field is
        # demonstrably truncated and a live client is available, fetch the COMPLETE
        # paginated set per-ticket instead.
        if (
            isinstance(total, int)
            and len(jira_comments) < total
            and local_comments
            and client is not None
        ):
            try:
                fetched = client.get_comments(jira_key)
                if isinstance(fetched, list):
                    jira_comments = fetched
            except Exception as exc:  # noqa: BLE001 — fail-open to the truncated subset (bug 4292)
                print(  # noqa: T201
                    f"WARNING: outbound_differ: live comment re-fetch for {jira_key!r} "
                    f"failed ({exc!r}); using the truncated snapshot set (may re-post).",
                    file=sys.stderr,
                )
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
        except Exception as exc:  # noqa: BLE001 — fail-open: skip comment mutations, warn (bug 4292)
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
        jira_bodies.add(_normalize_comment_body(raw, inbound_mapper=inbound_mapper))

    mutations: list[dict[str, Any]] = []
    for c in local_comments:
        raw = c.get("body", "") if isinstance(c, dict) else c
        body = _normalize_comment_body(raw, inbound_mapper=inbound_mapper)
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
        compare_body = sanitizer.fit_comment(body)
        if compare_body and compare_body not in jira_bodies:
            # Decorate the outbound body with the reconciler marker so the
            # inbound differ can identify (and filter) our own echoes on the
            # next pass (Gap 1 loop-breaker).
            mutations.append({"action": "add", "body": _decorate_outbound_comment(body)})
    return mutations
