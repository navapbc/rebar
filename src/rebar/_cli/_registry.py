"""Immutable CLI route registry (RP-05 S2a scaffolding).

A stdlib-only, side-effect-free census of every current ``rebar`` spelling. Its
sole job for S2a is to *shadow* the router: :func:`derive_policy_sets` rebuilds
the router's live ``_cli`` policy frozensets from this table with ZERO delta,
without cutting any routing, help, or execution over to it.

Import isolation is a hard contract: importing this module, calling
:func:`route_for`, and calling :func:`derive_policy_sets` MUST NOT import any
command handler or optional dependency. Handler / parser references are stored
as LAZY dotted strings (``"module.path:attr"``) and are never imported at
construction; they are only shape-validated by :func:`validate`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# Capability ids map to packaging extras that actually exist. Every capability
# id used by any route in ROUTES must be a member of this set.
KNOWN_CAPABILITIES: frozenset[str] = frozenset({"mcp", "reviewbot", "ui", "agents"})

# Group buckets that derive a named policy frozenset (see derive_policy_sets).
# Groups NOT listed here (e.g. "intercept", "bootstrap", "repair") are census
# buckets that carry no policy-set membership of their own.
_GROUP_TO_SET: dict[str, str] = {
    "reads_init_only": "_READS_INIT_ONLY",
    "reads_no_init": "_READS_NO_INIT",
    "field_reads": "_FIELD_READS",
    "lookups": "_LOOKUPS",
    "descendants": "_DESCENDANTS",
    "gates": "_GATES",
    "signing": "_SIGNING",
    "lifecycle": "_LIFECYCLE",
    "compact": "_COMPACT",
    "bridge": "_BRIDGE",
    "io": "_IO",
    "writes_full": "_WRITES_FULL",
}


@dataclass(frozen=True)
class Route:
    """One CLI spelling and the metadata used to derive router policy sets."""

    name: str
    group: str
    aliases: tuple[str, ...] = ()
    hidden: bool = False
    retired: bool = False
    intercept: bool = False
    no_auto_mount: bool = False
    confirmable: bool = False
    legacy_output: bool = False
    handler: str | None = None
    parser_factory: str | None = None
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """A single validation problem detected in a route table."""

    code: str
    spelling: str
    detail: str


def _reads_init_only() -> tuple[Route, ...]:
    return tuple(
        Route(name, group="reads_init_only")
        for name in ("show", "list", "next-batch", "deps", "ready", "search", "session-logs")
    )


def _simple_read_groups() -> tuple[Route, ...]:
    return (
        Route("validate", group="reads_no_init"),
        Route("get-file-impact", group="field_reads"),
        Route("get-verify-commands", group="field_reads"),
        Route("exists", group="lookups"),
        Route("resolve", group="lookups"),
        Route("format", group="lookups"),
        Route("list-descendants", group="descendants"),
        Route("clarity-check", group="gates"),
        Route("check-ac", group="gates"),
        Route("quality-check", group="gates"),
        Route("summary", group="gates"),
        Route("sign", group="signing"),
        Route("verify-signature", group="signing"),
        Route("compact", group="compact"),
        Route("compact-all", group="compact"),
        Route("export", group="io"),
        Route("import", group="io"),
    )


def _lifecycle() -> tuple[Route, ...]:
    return (
        Route("transition", group="lifecycle", confirmable=True, legacy_output=True),
        Route("reopen", group="lifecycle", confirmable=True, legacy_output=True),
        Route("claim", group="lifecycle", confirmable=True, legacy_output=True),
    )


def _writes_full() -> tuple[Route, ...]:
    legacy = {"create", "idea"}
    names = (
        "create",
        "idea",
        "comment",
        "link",
        "unlink",
        "revert",
        "edit",
        "tag",
        "untag",
        "archive",
        "set-file-impact",
        "set-verify-commands",
        "attach-commits",
        "session-log",
    )
    return tuple(
        Route(name, group="writes_full", confirmable=True, legacy_output=name in legacy)
        for name in names
    )


def _intercepts() -> tuple[Route, ...]:
    # Pure-intercept subcommands routed above the set-based arms. ``metrics`` and
    # ``audit`` are also individually-routed arms; they carry a non-policy group
    # but keep intercept=True so ``_INTERCEPTS`` derives correctly.
    names = (
        "reconcile",
        "review-code",
        "scan-spec",
        "verify-completion",
        "review-plan",
        "sign-review",
        "enrich",
        "explain",
        "verify-commit-ticket",
        "verify-identity",
        "verify-authorship",
        "verify-opcert",
        "trusted-env",
        "remote-cert",
        "workflow",
        "llm",
        "jira-onboard",
        "prompt",
        "criteria",
        "identity",
        "config",
    )
    routes = [Route(name, group="intercept", intercept=True) for name in names]
    routes.append(Route("metrics", group="static_read", intercept=True))
    routes.append(Route("audit", group="static_read", intercept=True))
    return tuple(routes)


def _bridge_and_arms() -> tuple[Route, ...]:
    return (
        Route("bridge", group="bridge"),
        Route("bridge-status", group="bridge", hidden=True),
        Route("bridge-fsck", group="bridge"),
        Route("init", group="bootstrap", no_auto_mount=True),
        Route("scratch", group="bootstrap", no_auto_mount=True),
        Route("delete", group="delete"),
        Route("fsck", group="repair"),
        Route("fsck-recover", group="repair"),
        Route("tracker-maintenance", group="repair"),
        Route("doctor", group="repair"),
        Route("bridge-probe", group="repair"),
        Route("grounding-info", group="static_read"),
    )


def _build_routes() -> tuple[Route, ...]:
    return (
        *_reads_init_only(),
        *_simple_read_groups(),
        *_lifecycle(),
        *_writes_full(),
        *_intercepts(),
        *_bridge_and_arms(),
    )


ROUTES: tuple[Route, ...] = _build_routes()

_BY_NAME: dict[str, Route] = {route.name: route for route in ROUTES}


def route_for(spelling: str) -> Route | None:
    """Return the :class:`Route` for a canonical ``spelling`` (or ``None``)."""

    return _BY_NAME.get(spelling)


def derive_policy_sets() -> dict[str, frozenset[str]]:
    """Rebuild the router's live ``_cli`` policy frozensets from :data:`ROUTES`."""

    derived: dict[str, frozenset[str]] = {}
    for group, set_name in _GROUP_TO_SET.items():
        derived[set_name] = frozenset(r.name for r in ROUTES if r.group == group and not r.retired)
    derived["_INTERCEPTS"] = frozenset(r.name for r in ROUTES if r.intercept)
    derived["_HIDDEN_ALIASES"] = frozenset(r.name for r in ROUTES if r.hidden)
    derived["_NO_AUTO_MOUNT"] = frozenset(r.name for r in ROUTES if r.no_auto_mount)
    derived["_LEGACY_OUTPUT"] = frozenset(r.name for r in ROUTES if r.legacy_output)
    derived["_CONFIRM_SCOPE"] = frozenset(r.name for r in ROUTES if r.confirmable)
    return derived


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
    name_set = set(names)
    for route in routes:
        for alias in route.aliases:
            if alias in name_set:
                yield Finding(
                    "duplicate",
                    alias,
                    f"alias of {route.name!r} collides with a canonical spelling",
                )


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


_CHECKS = (
    _check_duplicates,
    _check_alias_retired,
    _check_capabilities,
    _check_references,
    _check_contradictions,
)


def validate(routes: Iterable[Route] = ROUTES) -> tuple[Finding, ...]:
    """Validate a route table, returning findings in a deterministic order.

    Uses ONLY route metadata (never importing handlers). The ``resource_census``
    code is part of the finding vocabulary but is not emitted for S2a, which
    ships no generated parser/help resources.
    """

    materialized = tuple(routes)
    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check(materialized))
    findings.sort(key=lambda f: (f.spelling, f.code, f.detail))
    return tuple(findings)


# Reserved finding vocabulary (kept complete for downstream stories); S2a ships
# no generated parser/help resources, so ``resource_census`` is never emitted.
_RESERVED_CODES: tuple[str, ...] = ("resource_census",)
