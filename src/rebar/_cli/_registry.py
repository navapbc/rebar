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


# Core-command parser-factory census (RP-05 S2b): each core spelling maps to a lazy
# ``"module:attr"`` reference under ``rebar._cli._parsers.core`` resolved only at
# build time, never at import (mirrors the advanced-family map in ``_intercepts``).
_CORE_P = "rebar._cli._parsers.core"
_CORE_FACTORIES: dict[str, str] = {
    "show": f"{_CORE_P}.reads:build_show",
    "list": f"{_CORE_P}.reads:build_list",
    "next-batch": f"{_CORE_P}.reads:build_next_batch",
    "deps": f"{_CORE_P}.reads:build_deps",
    "ready": f"{_CORE_P}.reads:build_ready",
    "search": f"{_CORE_P}.reads:build_search",
    "session-logs": f"{_CORE_P}.reads:build_session_logs",
    "validate": f"{_CORE_P}.reads:build_validate",
    "get-file-impact": f"{_CORE_P}.field_reads:build_get_file_impact",
    "get-verify-commands": f"{_CORE_P}.field_reads:build_get_verify_commands",
    "exists": f"{_CORE_P}.lookups:build_exists",
    "resolve": f"{_CORE_P}.lookups:build_resolve",
    "format": f"{_CORE_P}.lookups:build_format",
    "list-descendants": f"{_CORE_P}.descendants:build",
    "clarity-check": f"{_CORE_P}.gates:build_clarity_check",
    "check-ac": f"{_CORE_P}.gates:build_check_ac",
    "quality-check": f"{_CORE_P}.gates:build_quality_check",
    "summary": f"{_CORE_P}.gates:build_summary",
    "sign": f"{_CORE_P}.signing:build_sign",
    "verify-signature": f"{_CORE_P}.signing:build_verify_signature",
    "compact": f"{_CORE_P}.compact:build_compact",
    "compact-all": f"{_CORE_P}.compact:build_compact_all",
    "export": f"{_CORE_P}.io:build_export",
    "import": f"{_CORE_P}.io:build_import",
    "transition": f"{_CORE_P}.lifecycle:build_transition",
    "reopen": f"{_CORE_P}.lifecycle:build_reopen",
    "claim": f"{_CORE_P}.lifecycle:build_claim",
    "create": f"{_CORE_P}.writes:build_create",
    "idea": f"{_CORE_P}.writes:build_idea",
    "comment": f"{_CORE_P}.writes:build_comment",
    "link": f"{_CORE_P}.writes:build_link",
    "unlink": f"{_CORE_P}.writes:build_unlink",
    "revert": f"{_CORE_P}.writes:build_revert",
    "edit": f"{_CORE_P}.writes:build_edit",
    "tag": f"{_CORE_P}.writes:build_tag",
    "untag": f"{_CORE_P}.writes:build_untag",
    "archive": f"{_CORE_P}.writes:build_archive",
    "set-file-impact": f"{_CORE_P}.writes:build_set_file_impact",
    "set-verify-commands": f"{_CORE_P}.writes:build_set_verify_commands",
    "attach-commits": f"{_CORE_P}.writes:build_attach_commits",
    "session-log": f"{_CORE_P}.writes:build_session_log",
    "init": f"{_CORE_P}.bootstrap:build_init",
    "scratch": f"{_CORE_P}.bootstrap:build_scratch",
    "delete": f"{_CORE_P}.delete:build",
    "fsck": f"{_CORE_P}.repair:build_fsck",
    "fsck-recover": f"{_CORE_P}.repair:build_fsck_recover",
    "tracker-maintenance": f"{_CORE_P}.repair:build_tracker_maintenance",
    "doctor": f"{_CORE_P}.repair:build_doctor",
    "grounding-info": f"{_CORE_P}.grounding:build",
}


def _core_factory(name: str) -> str:
    """The core parser-factory reference for ``name`` (KeyError if unmapped)."""
    return _CORE_FACTORIES[name]


def _reads_init_only() -> tuple[Route, ...]:
    return tuple(
        Route(name, group="reads_init_only", parser_factory=_core_factory(name))
        for name in ("show", "list", "next-batch", "deps", "ready", "search", "session-logs")
    )


def _simple_read_groups() -> tuple[Route, ...]:
    return (
        Route("validate", group="reads_no_init", parser_factory=_core_factory("validate")),
        Route(
            "get-file-impact",
            group="field_reads",
            parser_factory=_core_factory("get-file-impact"),
        ),
        Route(
            "get-verify-commands",
            group="field_reads",
            parser_factory=_core_factory("get-verify-commands"),
        ),
        Route("exists", group="lookups", parser_factory=_core_factory("exists")),
        Route("resolve", group="lookups", parser_factory=_core_factory("resolve")),
        Route("format", group="lookups", parser_factory=_core_factory("format")),
        Route(
            "list-descendants",
            group="descendants",
            parser_factory=_core_factory("list-descendants"),
        ),
        Route("clarity-check", group="gates", parser_factory=_core_factory("clarity-check")),
        Route("check-ac", group="gates", parser_factory=_core_factory("check-ac")),
        Route("quality-check", group="gates", parser_factory=_core_factory("quality-check")),
        Route("summary", group="gates", parser_factory=_core_factory("summary")),
        Route("sign", group="signing", parser_factory=_core_factory("sign")),
        Route(
            "verify-signature",
            group="signing",
            parser_factory=_core_factory("verify-signature"),
        ),
        Route("compact", group="compact", parser_factory=_core_factory("compact")),
        Route("compact-all", group="compact", parser_factory=_core_factory("compact-all")),
        Route("export", group="io", parser_factory=_core_factory("export")),
        Route("import", group="io", parser_factory=_core_factory("import")),
    )


def _lifecycle() -> tuple[Route, ...]:
    return (
        Route(
            "transition",
            group="lifecycle",
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("transition"),
        ),
        Route(
            "reopen",
            group="lifecycle",
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("reopen"),
        ),
        Route(
            "claim",
            group="lifecycle",
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("claim"),
        ),
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
        Route(
            name,
            group="writes_full",
            confirmable=True,
            legacy_output=name in legacy,
            parser_factory=_core_factory(name),
        )
        for name in names
    )


def _intercepts() -> tuple[Route, ...]:
    # Pure-intercept subcommands routed above the set-based arms. ``metrics`` and
    # ``audit`` are also individually-routed arms; they carry a non-policy group
    # but keep intercept=True so ``_INTERCEPTS`` derives correctly.
    #
    # Each advanced family carries a lazy ``parser_factory`` reference (RP-05 S2c):
    # a ``"module:attr"`` string resolved only at build time, never at import.
    _P = "rebar._cli._parsers.advanced"
    factories: dict[str, str] = {
        "reconcile": f"{_P}.reconcile:build",
        "review-code": f"{_P}.llm:build_review_code",
        "scan-spec": f"{_P}.llm:build_scan_spec",
        "verify-completion": f"{_P}.llm:build_verify_completion",
        "review-plan": f"{_P}.llm:build_review_plan",
        "sign-review": f"{_P}.llm:build_sign_review",
        "enrich": f"{_P}.enrich:build",
        "explain": f"{_P}.llm:build_explain",
        "verify-commit-ticket": f"{_P}.verify:build_commit_ticket",
        "verify-identity": f"{_P}.verify:build_identity",
        "verify-authorship": f"{_P}.verify:build_identity",
        "verify-opcert": f"{_P}.verify:build_opcert",
        "trusted-env": f"{_P}.certs:build_trusted_env",
        "remote-cert": f"{_P}.certs:build_remote_cert",
        "workflow": f"{_P}.workflow:build",
        "llm": f"{_P}.llm_eval:build_llm",
        "jira-onboard": f"{_P}.jira:build",
        "prompt": f"{_P}.llm_eval:build_prompt",
        "criteria": f"{_P}.llm_eval:build_criteria",
        "identity": f"{_P}.identity:build",
        "config": f"{_P}.config:build",
    }
    names = tuple(factories)
    routes = [
        Route(name, group="intercept", intercept=True, parser_factory=factories[name])
        for name in names
    ]
    routes.append(
        Route(
            "metrics",
            group="static_read",
            intercept=True,
            parser_factory=f"{_P}.metrics:build",
        )
    )
    routes.append(
        Route(
            "audit",
            group="static_read",
            intercept=True,
            parser_factory=f"{_P}.audit:build",
        )
    )
    return tuple(routes)


def _bridge_and_arms() -> tuple[Route, ...]:
    _P = "rebar._cli._parsers.advanced"
    return (
        Route("bridge", group="bridge", parser_factory=f"{_P}.bridge:build"),
        Route(
            "bridge-status",
            group="bridge",
            hidden=True,
            parser_factory=f"{_P}.bridge:build",
        ),
        Route("bridge-fsck", group="bridge", parser_factory=f"{_P}.bridge_arms:build_fsck"),
        Route(
            "init",
            group="bootstrap",
            no_auto_mount=True,
            parser_factory=_core_factory("init"),
        ),
        Route(
            "scratch",
            group="bootstrap",
            no_auto_mount=True,
            parser_factory=_core_factory("scratch"),
        ),
        Route("delete", group="delete", parser_factory=_core_factory("delete")),
        Route("fsck", group="repair", parser_factory=_core_factory("fsck")),
        Route("fsck-recover", group="repair", parser_factory=_core_factory("fsck-recover")),
        Route(
            "tracker-maintenance",
            group="repair",
            parser_factory=_core_factory("tracker-maintenance"),
        ),
        Route("doctor", group="repair", parser_factory=_core_factory("doctor")),
        Route("bridge-probe", group="repair", parser_factory=f"{_P}.bridge_arms:build_probe"),
        Route(
            "grounding-info",
            group="static_read",
            parser_factory=_core_factory("grounding-info"),
        ),
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


def derive_policy_sets(routes: tuple[Route, ...] = ROUTES) -> dict[str, frozenset[str]]:
    """Rebuild the router's live ``_cli`` policy frozensets from a route table.

    Retired spellings are unrouted, so they contribute to no derived set -
    neither the group-derived sets nor the flag-derived overlays.
    """

    live = tuple(r for r in routes if not r.retired)
    derived: dict[str, frozenset[str]] = {}
    for group, set_name in _GROUP_TO_SET.items():
        derived[set_name] = frozenset(r.name for r in live if r.group == group)
    derived["_INTERCEPTS"] = frozenset(r.name for r in live if r.intercept)
    derived["_HIDDEN_ALIASES"] = frozenset(r.name for r in live if r.hidden)
    derived["_NO_AUTO_MOUNT"] = frozenset(r.name for r in live if r.no_auto_mount)
    derived["_LEGACY_OUTPUT"] = frozenset(r.name for r in live if r.legacy_output)
    derived["_CONFIRM_SCOPE"] = frozenset(r.name for r in live if r.confirmable)
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
