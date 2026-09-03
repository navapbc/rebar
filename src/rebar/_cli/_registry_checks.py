"""Route-table validation for the CLI registry — the checks, and the vocabularies
they validate against.

Extracted from ``_registry.py``, which had reached the 800-line hard cap with no
headroom left for the policy flag ticket ``elfin-decagonal-polarbear`` must add to
``Route`` (ticket ``bc66-4827-355a-43bd``). The cut follows an existing call-graph
seam rather than a line count: these functions call only each other, take the route
table as a PARAMETER, and yield :class:`Finding`. They READ a route table; they never
build one, so nothing here imports ``ROUTES``.

That direction is what keeps the split acyclic. ``_registry`` imports this module;
this module imports nothing from ``_registry``. :func:`validate` therefore stays in
``_registry``, where its ``routes=ROUTES`` default lives, and only the checks move.
``Route`` is needed for annotations alone, so it is imported under ``TYPE_CHECKING``
(annotations are strings here via ``from __future__ import annotations``).

The three closed vocabularies move with the checks because the checks are their only
runtime consumer; ``_registry`` re-exports them, and its ``__all__`` pins that surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rebar._capabilities import CAPABILITY_KEYS

if TYPE_CHECKING:  # pragma: no cover - typing-only, and importing it at runtime would cycle
    from ._registry import Route

# The possible-capability references a route may advertise are the SEMANTIC capability
# keys of the descriptive capability registry (``rebar._capabilities``, ADR 0100 §7) —
# single-sourced here so route validation and the capability seam never drift. This is
# descriptive validation only: a route *advertises* a capability it may exercise; nothing
# is enforced at route/help construction (the ``rebar._capabilities`` module is stdlib-only
# and imports no optional package, so this preserves the registry's import-isolation
# contract). Enforcement happens later, at the selected execution boundary.
KNOWN_CAPABILITIES: frozenset[str] = CAPABILITY_KEYS

# The closed set of invocation-adapter kinds — the exact runtime call shape a
# selected handler is invoked through (RP-05 S3). This is intentionally small and
# fixed: a route selects ONE kind, never a bespoke call site.
#   dispatcher         → handler([name, *rest])          (reads.main / commands.main)
#   argv               → handler([*argv_prefix, *rest])  (module <verb>_cli(rest))
#   argv_tracker       → handler(rest, tracker_dir())
#   argv_tracker_root  → handler(rest, tracker_dir(), None)  # root discovered downstream
ADAPTER_KINDS: frozenset[str] = frozenset(
    {"dispatcher", "argv", "argv_tracker", "argv_tracker_root"}
)

# The closed set of init policies applied before a handler runs (RP-05 S3):
#   none / init_only / full are the static policies; ``doctor`` and
#   ``fsck_recover`` are the two genuinely conditional selectors preserved from
#   the pre-cutover per-arm census.
INIT_POLICIES: frozenset[str] = frozenset({"none", "init_only", "full", "doctor", "fsck_recover"})


@dataclass(frozen=True)
class Finding:
    """A single validation problem detected in a route table."""

    code: str
    spelling: str
    detail: str


def _is_valid_reference(ref: str) -> bool:
    """True if ``ref`` is a well-formed ``"dotted.module.path:attr"`` string."""

    if ref.count(":") != 1:
        return False
    module, _, attr = ref.partition(":")
    if not module or not attr:
        return False
    if not all(part.isidentifier() for part in module.split(".")):
        return False
    return attr.isidentifier()


def _check_duplicates(routes: tuple[Route, ...]) -> Iterator[Finding]:
    names = [r.name for r in routes]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            yield Finding("duplicate", name, "spelling declared more than once")
        seen.add(name)
    name_set = {r.name for r in routes if not r.retired}
    seen_aliases: set[str] = set()
    for route in routes:
        for alias in route.aliases:
            if alias in name_set:
                yield Finding(
                    "duplicate",
                    alias,
                    f"alias of {route.name!r} collides with a canonical spelling",
                )
            elif alias in seen_aliases:
                yield Finding("duplicate", alias, "alias declared more than once")
            seen_aliases.add(alias)


def _check_alias_retired(routes: tuple[Route, ...]) -> Iterator[Finding]:
    retired = {r.name for r in routes if r.retired}
    for route in routes:
        for alias in route.aliases:
            if alias in retired:
                yield Finding(
                    "alias_retired_collision",
                    alias,
                    f"alias of {route.name!r} collides with a retired spelling",
                )


def _check_capabilities(routes: tuple[Route, ...]) -> Iterator[Finding]:
    for route in routes:
        for cap in route.capabilities:
            if cap not in KNOWN_CAPABILITIES:
                yield Finding("unknown_capability", route.name, f"unknown capability {cap!r}")


def _check_references(routes: tuple[Route, ...]) -> Iterator[Finding]:
    for route in routes:
        for label, ref in (("handler", route.handler), ("parser_factory", route.parser_factory)):
            if ref is not None and not _is_valid_reference(ref):
                yield Finding("malformed_reference", route.name, f"malformed {label} {ref!r}")


def _check_contradictions(routes: tuple[Route, ...]) -> Iterator[Finding]:
    for route in routes:
        if route.retired and route.hidden:
            yield Finding("contradiction", route.name, "retired route cannot also be hidden")
        if route.retired and route.handler is not None:
            yield Finding("contradiction", route.name, "retired route cannot carry a handler")


def _check_execution(routes: tuple[Route, ...]) -> Iterator[Finding]:
    """Validate the RP-05 S3 execution metadata on live (non-retired) routes."""
    for route in routes:
        if route.retired:
            continue
        if route.adapter and route.adapter not in ADAPTER_KINDS:
            yield Finding("unknown_adapter", route.name, f"unknown adapter {route.adapter!r}")
        if route.init not in INIT_POLICIES:
            yield Finding("unknown_init", route.name, f"unknown init policy {route.init!r}")
        if route.handler is not None and not route.adapter:
            yield Finding("handler_without_adapter", route.name, "handler set but adapter is empty")
        if route.argv_prefix and route.adapter != "argv":
            yield Finding(
                "prefix_without_argv",
                route.name,
                f"argv_prefix set but adapter is {route.adapter!r}",
            )


#: Run in this order by ``_registry.validate``; the findings are sorted afterwards, so
#: the order here does not affect the result — it is kept stable for readable diffs.
_CHECKS = (
    _check_duplicates,
    _check_alias_retired,
    _check_capabilities,
    _check_references,
    _check_contradictions,
    _check_execution,
)


# Reserved finding vocabulary (kept complete for downstream stories); S2a ships
# no generated parser/help resources, so ``resource_census`` is never emitted.
_RESERVED_CODES: tuple[str, ...] = ("resource_census",)
