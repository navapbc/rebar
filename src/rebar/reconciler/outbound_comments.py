"""Compare local comments with Jira and build marker-safe outbound mutations.

Persistent comment IDs are the primary identity check; decoded body equality
backs it up after a lost map write. The comparison key comes from the exact
fitted wire text sent, decoded through the same inbound mapper as Jira bodies.
Unknown live comment state suppresses adds. Backend mapper, sanitizer, and
codec resolve lazily, keeping this module independent of a vendor adapter.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rebar.reconciler.parent_breadcrumb import (  # noqa: F401 — tag re-exported (split seam)
    PARENT_BREADCRUMB_TAG,
    _breadcrumb_target,
    _build_parent_breadcrumb,
)

if TYPE_CHECKING:
    from ._backend import TicketTransport

# Sentinel: presence of the "comment" key in a snapshot entry confirms the
# snapshot carries real comment data (fixture/synthetic path). Absence means
# the entry came from a live Jira search result, which never includes comments.
_COMMENT_FIELD_KEY = "comment"

# Mark outbound bodies so inbound diffing can discard their echoes. Both
# directions share this exact token.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"

# Parent breadcrumbs are imported from ``parent_breadcrumb``. Their tag
# survives normalization into the resolved-comment dedup set.


def _resolve_inbound_mapper(inbound_mapper: Any | None) -> Any:
    """Use an injected mapper or resolve the configured backend lazily."""
    if inbound_mapper is not None:
        return inbound_mapper
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    return select_backend(compose_config()).inbound


def _resolve_sanitizer(sanitizer: Any | None) -> Any:
    """Resolve the injected ``FieldSanitizer``, falling back to the configured
    backend (ticket 21ca; same injection seam as :func:`_resolve_inbound_mapper`)."""
    if sanitizer is not None:
        return sanitizer
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    return select_backend(compose_config()).sanitizer


class _IdentityCodec:
    """Identity fallback when a configured backend lacks ``comment_codec``."""

    def normalize_outbound(self, text: str) -> str:
        return text

    def to_wire(self, text: str) -> Any:
        """Identity wire shape (bug 17c3-2f6e: the key derivation predicts the
        as-sent wire via ``to_wire``, so the fallback must expose it too)."""
        return text


def _resolve_codec(codec: Any | None) -> Any:
    """Use an injected codec or the configured backend's codec lazily.

    The local dedup key decodes ``to_wire`` through the inbound mapper. Older
    backends without ``comment_codec`` use the identity fallback.
    """
    if codec is not None:
        return codec
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    resolved = getattr(select_backend(compose_config()).outbound, "comment_codec", None)
    return resolved if resolved is not None else _IdentityCodec()


def _map_comments_for_create(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    """Decorate create comments and carry HLC keys for returned Jira IDs."""
    comments = ticket.get("comments", [])
    return [
        {
            "action": "add",
            "body": _decorate_outbound_comment(c.get("body", "")),
            "local_comment_key": c.get("timestamp"),
        }
        for c in comments
    ]


def _normalize_comment_body(body: Any, inbound_mapper: Any | None = None) -> str:
    """Decode rich text to comparable user text and strip the echo marker."""
    text = _resolve_inbound_mapper(inbound_mapper).normalize_rich_text(body)
    return text.replace(RECONCILER_MARKER, "").strip()


def _decorate_outbound_comment(body: str) -> str:
    """Append the reconciler marker to an outbound comment body (Gap 1).

    Two paragraphs of separation keeps the marker visually below the user
    content. The marker survives ADF round-trip (each paragraph maps to
    its own ADF node and back).
    """
    return f"{body}\n\n{RECONCILER_MARKER}"


def fit_preserving_marker(text: str, fit: Callable[[str], str]) -> str:
    """Fit content within the vendor budget while preserving its echo marker.

    An ordinary right truncation cuts the trailing marker and can create an
    inbound echo loop. Undecorated or already fitting bodies pass through;
    oversized decorated bodies keep the largest fitting content prefix plus
    the vendor's probed truncation notice. The injected fitter supplies the
    limit and notice, while the decorator supplies marker spacing.
    """
    if not text.endswith(RECONCILER_MARKER):
        return fit(text)
    probe = fit(text)
    if probe == text:
        # Already within the limit: send it completely unchanged.
        return text
    # The vendor notice is whatever the probe appended past the point it cut.
    common = len(_common_prefix(text, probe))
    notice = probe[common:]
    # Strip the decoration to recover the user content: the separator's length is
    # taken from the decorator so no separator literal is duplicated here.
    separator_len = len(_decorate_outbound_comment("")) - len(RECONCILER_MARKER)
    content = text[: len(text) - len(RECONCILER_MARKER) - separator_len]
    lo, hi, best = 0, len(content), None
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _decorate_outbound_comment(content[:mid] + notice)
        if fit(candidate) == candidate:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    # Pathological: not even the bare decoration fits. Fall back to the vendor
    # fitter's own answer rather than emitting something over the limit.
    return best if best is not None else probe


def fit_comment_as_sent(text: str, fit: Callable[[str], str]) -> str:
    """Return the landed user text using the same decorated fit as the send path.

    The marker consumes budget, so fitting bare text would give a different
    dedup key and re-post long comments. In-limit text remains byte-identical;
    each backend supplies its own comment fitter.
    """
    decoration = _decorate_outbound_comment("")
    landed = fit_preserving_marker(_decorate_outbound_comment(text), fit)
    if landed.endswith(decoration):
        return landed[: len(landed) - len(decoration)]
    # Pathological: not even the bare decoration fit, so fit_preserving_marker fell
    # back to the vendor fitter's own (undecorated) answer. Pass it through as-is.
    return landed


@dataclass(frozen=True)
class FittedComment:
    """Exact decorated send text and its marker-free decoded dedup key."""

    landed_text: str
    wire_body: str


def fit_for_send(
    body: str,
    *,
    sanitizer: Any | None = None,
    codec: Any | None = None,
    inbound_mapper: Any | None = None,
) -> FittedComment:
    """Derive send text and decoded comparison key from one backend fit.

    The mutation carries ``wire_body`` to the transport, so ``landed_text``
    describes what is sent rather than a separately predicted fit. Backend
    collaborators resolve lazily when not injected.
    """
    fitted = _resolve_sanitizer(sanitizer).fit_comment(body)
    wire_body = _decorate_outbound_comment(fitted)
    landed_text = _normalize_comment_body(
        _resolve_codec(codec).to_wire(wire_body), inbound_mapper=inbound_mapper
    )
    return FittedComment(landed_text=landed_text, wire_body=wire_body)


def _common_prefix(left: str, right: str) -> str:
    """The shared leading run of two strings (returned as the prefix itself).

    Used by :func:`fit_preserving_marker` to isolate what a vendor fitter APPENDED
    when it truncated, without importing or restating the vendor's notice literal.
    """
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


# Exclude current and legacy canary alerts: their changing timestamps would
# create a new Jira comment every pass. Human comments remain eligible.
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


def _snapshot_comment_set(
    jira_key: str,
    comment_field: Any,
    local_comments: list[Any],
    client: TicketTransport | None,
) -> list[Any]:
    """Use snapshot comments, fetching all pages if their reported total exceeds them.

    Deduping against a capped snapshot would re-post comments beyond its cap.
    """
    embedded = comment_field.get("comments", []) if isinstance(comment_field, dict) else []
    total = comment_field.get("total") if isinstance(comment_field, dict) else None
    jira_comments: list = embedded if isinstance(embedded, list) else []
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
    return jira_comments


def _live_comment_set(
    jira_key: str,
    client: TicketTransport | None,
    *,
    has_local_comments: bool,
    breadcrumb_applies: bool,
) -> list[Any] | None:
    """Fetch live comments, or return ``None`` to suppress all adds.

    Unknown state never permits blind additions. A pending parent breadcrumb
    still fetches when the ticket has no local comments, preserving append-once
    deduplication.
    """
    if not has_local_comments and not breadcrumb_applies:
        return None
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
        return None
    # Fetch comments live. One call per ticket — bounded by the set of bound
    # tickets with local comments (or a pending parent-drift breadcrumb).
    try:
        fetched = client.get_comments(jira_key)
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
        return None
    return fetched if isinstance(fetched, list) else []


def _diff_comments(
    ticket: dict[str, Any],
    jira_key: str,
    jira_snapshot: dict[str, Any],
    client: TicketTransport | None = None,
    *,
    inbound_mapper: Any | None = None,
    sanitizer: Any | None = None,
    codec: Any | None = None,
    binding_store: Any | None = None,
    local_parents: dict[str, Any] | None = None,
    local_ticket_types: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Emit only comments absent from Jira's resolved comment set.

    Persistent HLC/Jira IDs skip already mapped or inbound comments first.
    Body equality is a fallback: ``fit_for_send`` derives the local key from
    the actual fitted wire, and the inbound mapper decodes both keys. A
    snapshot ``comment`` field supplies known state; live search lacks it and
    requires a successful fetch, including for a pending parent breadcrumb.
    Unknown state emits no comment mutations.
    """
    # Resolve the injected Backend-port members ONCE per call (ticket 21ca) rather
    # than re-resolving per comment below.
    inbound_mapper = _resolve_inbound_mapper(inbound_mapper)
    sanitizer = _resolve_sanitizer(sanitizer)
    codec = _resolve_codec(codec)

    local_comments = ticket.get("comments", [])
    jira_issue = jira_snapshot.get(jira_key, {})

    # Key presence, not truthiness, separates known snapshot comments from
    # live search's unknown state; an empty comment field is still observed.
    resolved: list[Any] | None
    if _COMMENT_FIELD_KEY in jira_issue:
        # Snapshot-carried path (fixtures, synthetic, or the bulk get_comment_map
        # enrichment). Normally used directly — do NOT call the client (the
        # bug-1f3d truncation guard inside is the one exception).
        resolved = _snapshot_comment_set(
            jira_key, jira_issue[_COMMENT_FIELD_KEY], local_comments, client
        )
    else:
        # Live search path: snapshot lacks the comment field (bug 4292). None
        # means "emit no comment mutations" — unknown Jira state or nothing to do.
        resolved = _live_comment_set(
            jira_key,
            client,
            has_local_comments=bool(local_comments),
            breadcrumb_applies=_breadcrumb_target(
                ticket,
                binding_store=binding_store,
                local_parents=local_parents,
                local_ticket_types=local_ticket_types,
            )
            is not None,
        )
    if resolved is None:
        return []
    jira_comments: list = resolved

    jira_bodies: set[str] = set()
    for c in jira_comments:
        raw = c.get("body", "") if isinstance(c, dict) else c
        jira_bodies.add(_normalize_comment_body(raw, inbound_mapper=inbound_mapper))

    mutations: list[dict[str, Any]] = []
    for c in local_comments:
        raw = c.get("body", "") if isinstance(c, dict) else c
        body = _normalize_comment_body(raw, inbound_mapper=inbound_mapper)
        local_comment_key = c.get("timestamp") if isinstance(c, dict) else None
        # Persistent HLC or Jira IDs are the primary skip; body equality must
        # not collapse distinct same-text comments or replay inbound ones.
        if isinstance(c, dict) and c.get("jira_comment_id"):
            continue
        if (
            binding_store is not None
            and local_comment_key is not None
            and binding_store.is_comment_mapped(local_comment_key)
        ):
            continue
        # Bug 6afc-20ee-84e5-4dd5: never mirror skill-to-skill machine-marker
        # comments (BRIDGE_CANARY_ALERT:, etc.) outbound to Jira. Symmetric with
        # the label _EXCLUDED_PREFIXES exclusion.
        if _is_machine_marker_comment(body):
            continue
        # Body equality backs up IDs after a lost map write. ``fit_for_send``
        # supplies both the exact truncated wire body and its decoded key;
        # the same decoder handles fetched Jira bodies. The local store stays
        # untouched, and rich-text normalization cannot drift between sides.
        fitted = fit_for_send(body, sanitizer=sanitizer, codec=codec, inbound_mapper=inbound_mapper)
        compare_body = fitted.landed_text
        if compare_body and compare_body not in jira_bodies:
            # Mark the body against inbound echoes and carry its HLC key for
            # recording the returned Jira comment ID.
            mutations.append(
                {
                    "action": "add",
                    "body": _decorate_outbound_comment(body),
                    # The bytes the transport must send, decided HERE (story 4cee).
                    # ``body`` stays for the pre-cutover fallback and for readers.
                    "wire_body": fitted.wire_body,
                    "local_comment_key": local_comment_key,
                }
            )
    # S7: after the resolved Jira dedup set is built, append at most one
    # parent-drift breadcrumb (append-once, keyed on PARENT_BREADCRUMB_TAG).
    mutations.extend(
        _build_parent_breadcrumb(
            ticket,
            jira_bodies,
            binding_store=binding_store,
            local_parents=local_parents,
            local_ticket_types=local_ticket_types,
            decorate=_decorate_outbound_comment,
        )
    )
    return mutations
