"""Parent-drift breadcrumb for the outbound comment differ (S7 + bug 9ebb-3114).

When a local parent cannot be represented in Jira — the parent is a non-epic
and the project permits only Epic parents — the outbound differ suppresses the
parent field (``adapters/jira/outbound_fields.py``), and the child lands as an
under-defined leaf. This module builds the echo-safe breadcrumb comment that
points the Jira user at the nearest ancestor that IS represented in Jira
(S7, 2c66-205d-92e1-4419). It never re-ingests inbound: the caller decorates
the body with ``RECONCILER_MARKER``, which the inbound comment differ filters.

Split out of ``outbound_comments.py`` (bug 9ebb-3114-4d0e-4528) along the
existing call-graph seam — :func:`_build_parent_breadcrumb` and
:func:`_breadcrumb_target` form a pure, self-contained cluster that
``outbound_comments._diff_comments`` calls — when the live-search breadcrumb
fix pushed that module past the 800-line cap. ``outbound_comments`` re-exports
these names, so every existing import site keeps resolving.

Dedup / append-once contract: the breadcrumb carries the stable identity tag
:data:`PARENT_BREADCRUMB_TAG`. Dedup keys on THIS tag, never on the (variable)
ancestor Jira key, so the first-writer-wins guard holds even if a later pass
would name a different ancestor. Unlike ``RECONCILER_MARKER`` the tag is NOT
stripped by ``outbound_comments._normalize_comment_body``, so it survives into
the resolved-comment dedup set.

To avoid a circular import this module does not import ``outbound_comments``;
the outbound decoration is injected as the ``decorate`` callable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Stable identity tag carried by a parent-drift breadcrumb comment (S7). See
# the module docstring for the append-once contract it keys.
PARENT_BREADCRUMB_TAG = "<!-- rebar:parent-breadcrumb -->"


def _breadcrumb_target(
    ticket: dict[str, Any],
    *,
    binding_store: Any | None,
    local_parents: dict[str, Any] | None,
    local_ticket_types: dict[str, Any] | None,
) -> tuple[str, bool] | None:
    """S7: the parent-drift breadcrumb's target, or ``None`` when none applies.

    The pure precondition checks + nearest-bound-ancestor walk, split from
    :func:`_build_parent_breadcrumb` (bug 9ebb-3114-4d0e-4528) so the
    live-search path can ask "would a breadcrumb apply?" before deciding
    whether a live comment fetch is needed. Returns
    ``(nearest_bound_jira_key, skipped_intervening)`` or ``None`` when the
    ancestor maps / binding store are absent, there is no drift (missing or
    epic parent type), or no ancestor has a bound Jira key.
    """
    if local_parents is None or local_ticket_types is None or binding_store is None:
        return None
    parent_id = ticket.get("parent_id")
    if not parent_id:
        return None
    parent_type = local_ticket_types.get(parent_id)
    if parent_type is None or str(parent_type).lower() == "epic":
        return None
    skipped_intervening = False
    ancestor: Any = parent_id
    while ancestor is not None:
        key = binding_store.get_jira_key(ancestor)
        if key is not None:
            return key, skipped_intervening
        skipped_intervening = True
        ancestor = local_parents.get(ancestor)
    return None


def _build_parent_breadcrumb(
    ticket: dict[str, Any],
    jira_bodies: set[str],
    *,
    binding_store: Any | None,
    local_parents: dict[str, Any] | None,
    local_ticket_types: dict[str, Any] | None,
    decorate: Callable[[str], str],
) -> list[dict[str, Any]]:
    """S7: build the echo-safe parent-drift breadcrumb mutations (0 or 1), pure.

    A pure helper (kept out of ``_diff_comments`` so that function's
    cyclomatic complexity does not rise past the CI ratchet). Returns a list of
    at most one ``{"action": "add", "body": <decorated>}`` mutation — a list so
    the caller can ``extend`` without adding a branch of its own. ``decorate``
    is the caller's outbound decoration
    (``outbound_comments._decorate_outbound_comment``), injected to keep this
    module import-cycle-free.

    Emit conditions (the precondition checks + ancestor walk live in
    :func:`_breadcrumb_target`):
      * the ancestor maps must both be provided AND a binding store available —
        otherwise the feature is a strict no-op (every existing caller omits the
        maps, so behaviour is unchanged);
      * DRIFT: the ticket has a ``parent_id`` whose direct parent is present in
        ``local_ticket_types`` with a non-epic type (mirrors the parent-field
        suppression in ``adapters/jira/outbound_fields.py``). A missing/epic parent
        type means the parent field is NOT suppressed → no breadcrumb;
      * APPEND-ONCE: no already-resolved Jira comment carries
        :data:`PARENT_BREADCRUMB_TAG` (first-writer-wins; dedup keys on the tag);
      * a NEAREST bound ancestor exists: walking upward from the direct parent via
        ``local_parents``, some ancestor has a bound Jira key. If none does, emit
        nothing.

    When ≥1 intervening ancestor was skipped for lacking a bound key, the body
    additionally states that intervening levels are not represented.
    """
    target = _breadcrumb_target(
        ticket,
        binding_store=binding_store,
        local_parents=local_parents,
        local_ticket_types=local_ticket_types,
    )
    if target is None:
        return []
    if any(PARENT_BREADCRUMB_TAG in body for body in jira_bodies):
        return []
    nearest_key, skipped_intervening = target

    sentences = [
        "This ticket's parent hierarchy could not be fully represented in Jira.",
        f"Nearest tracked ancestor: {nearest_key}.",
    ]
    if skipped_intervening:
        sentences.append("One or more intervening parent levels are not represented in Jira.")
    sentences.append("Full parent context is maintained in rebar.")
    body = " ".join(sentences) + "\n" + PARENT_BREADCRUMB_TAG
    return [{"action": "add", "body": decorate(body)}]
