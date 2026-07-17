"""Outbound comment-diff cluster for bidirectional Jira sync.

The cohesive comment-diff seam extracted from ``outbound_differ.py`` (it grew
past the module-size soft cap; the comment logic is self-contained). Owns the
local→Jira comment comparison and the create-path comment mapping:

    - ``_diff_comments`` — compare local comments to Jira's, emit "add" mutations
      for the ones not already mirrored (the bug-4292 live-fetch safety invariant
      lives here).
    - ``_map_comments_for_create`` — map all local comments to outbound "add"
      mutations for a brand-new issue.
    - ``_normalize_comment_body`` / ``_decorate_outbound_comment`` — the ADF→text
      normalisation and the RECONCILER_MARKER loop-breaker decoration (bug 85a1 /
      Gap 1).
    - ``_is_machine_marker_comment`` — the bridge-internal machine-comment
      exclusion (bug 6afc).

``compute_outbound_mutations`` (in ``outbound_differ``) imports this module; the
dependency is one-way. Like ``inbound_differ``, this module keeps its OWN lazy
``_load_adf`` / ``_load_comment_limits`` loaders (the reconciler modules are
spec-loaded under test, where ``from . import`` does not resolve) so it never has
to import back from ``outbound_differ`` — avoiding an import cycle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
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

# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


# Lazy-loader singletons for the sibling adf / comment_limits modules. Kept
# module-local (each reconciler module owns its own copy) because the differ may
# be imported via ``importlib.util.spec_from_file_location`` in tests, which does
# not establish package context, so ``from . import adf`` would fail.
_ADF_KEY = "rebar_reconciler.adf"
_AdfModule = None

_COMMENT_LIMITS_KEY = "rebar_reconciler.comment_limits"
_CommentLimitsModule = None


def _load_adf():
    """Lazy-load the sibling adf module (own copy; mirrors outbound_differ's).

    Loaded by the canonical dotted sys.modules key so the module is executed
    exactly once across all callers, whether the differ was imported as a normal
    package module (production) or by file path (tests).
    """
    global _AdfModule
    if _AdfModule is None:
        _AdfModule = lazy_load(_ADF_KEY, "adf.py")
    return _AdfModule


def _load_comment_limits():
    """Lazy-load the sibling comment_limits module (own copy).

    Bug 6afc-20ee-84e5-4dd5: the truncation rule MUST be identical on the send
    path (acli.add_comment) and this differ comparison path, so both import the
    single shared ``truncate_comment_body`` helper. Loaded by file path (not
    ``from . import``) because the differ may be imported via
    ``importlib.util.spec_from_file_location`` in tests, which does not establish
    package context.
    """
    global _CommentLimitsModule
    if _CommentLimitsModule is None:
        _CommentLimitsModule = lazy_load(_COMMENT_LIMITS_KEY, "comment_limits.py")
    return _CommentLimitsModule


def _map_comments_for_create(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    """Map all local comments to outbound create mutations.

    Every outbound body is decorated with the reconciler marker (Gap 1
    loop-breaker) so inbound passes can identify our own echoes.
    """
    comments = ticket.get("comments", [])
    return [
        {"action": "add", "body": _decorate_outbound_comment(c.get("body", ""))} for c in comments
    ]


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
