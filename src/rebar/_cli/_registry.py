"""Immutable CLI route registry and execution metadata for RP-05.

``ROUTES`` is the single authority for recognized top-level command spellings
and their execution policy. Each ``Route`` stores lazy ``"module.path:attr"``
strings for its handler and parser factory. Importing this module does not
resolve those strings or import command handlers or optional dependencies.

Runtime dispatch calls :func:`route_for` and resolves only the selected handler
through ``rebar._cli._execute``. Help generation walks ``ROUTES``, resolves
parser factories during generation, and writes the committed artifacts that
runtime help serves. :func:`derive_policy_sets` exports compatibility policy
sets from the same table.

Route capability names come from ``rebar._capabilities.CAPABILITIES``. They
describe capabilities that a route may exercise. Capability checks remain at
the selected execution boundary after the mode or backend is known.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ._registry_checks import (
    _CHECKS,
    ADAPTER_KINDS,
    INIT_POLICIES,
    KNOWN_CAPABILITIES,
    Finding,
)

#: The surface consumers actually import from this module, pinned so the
#: ``_registry_checks`` split cannot silently drop a re-export. Derived from a census of
#: every in-tree reference (22 files, named imports and module-handle access alike):
#: these eight names, and no others, are read from outside. ``Finding`` is listed because
#: it is ``validate``'s element type, not because anything imports it — nothing does.
__all__ = [
    "ADAPTER_KINDS",
    "INIT_POLICIES",
    "KNOWN_CAPABILITIES",
    "ROUTES",
    "Finding",
    "Route",
    "derive_policy_sets",
    "route_for",
    "validate",
]

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
    # Advisory cross-session warning: this spelling takes a single ticket id, so another
    # session holding it is worth surfacing before the command runs (mirror F11). WHICH
    # verbs warn is a policy judgement; the flag only makes the set BE route names.
    warn_cross_session: bool = False
    legacy_output: bool = False
    handler: str | None = None
    parser_factory: str | None = None
    capabilities: tuple[str, ...] = ()
    # RP-05 S3 execution metadata. ``adapter`` names one of ADAPTER_KINDS (the
    # runtime call shape); ``init`` names one of INIT_POLICIES; ``argv_prefix`` is
    # prepended to the command remainder before an ``argv`` handler is called
    # (only ``bridge-status`` uses it, to reach ``bridge_cli(["status", ...])``).
    adapter: str = ""
    init: str = "none"
    argv_prefix: tuple[str, ...] = ()


# Core-command parser-factory census (RP-05 S2b): each core spelling maps to a
# lazy core-parser ref, resolved only at build time, never at import.
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
    "reclaim-collapse": f"{_CORE_P}.reclaim:build_reclaim_collapse",
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


_READS_DISPATCHER = "rebar._engine_support.reads:main"
_FIELD_READS_MOD = "rebar._engine_support.field_reads"
_LOOKUPS_MOD = "rebar._engine_support.lookups"
_GATES_MOD = "rebar._engine_support.gates"


def _reads_init_only() -> tuple[Route, ...]:
    return tuple(
        Route(
            name,
            group="reads_init_only",
            parser_factory=_core_factory(name),
            handler=_READS_DISPATCHER,
            adapter="dispatcher",
            init="init_only",
            warn_cross_session=warn,
        )
        # The flag rides the definition site, so renaming a spelling carries its warning
        # instead of leaving a stale entry that silently never matches again (mirror F11).
        for name, warn in (
            ("show", True),
            ("list", False),
            ("next-batch", False),
            ("deps", True),
            ("ready", False),
            ("search", False),
            ("session-logs", False),
        )
    )


def _simple_read_groups() -> tuple[Route, ...]:
    return (
        Route(
            "validate",
            group="reads_no_init",
            parser_factory=_core_factory("validate"),
            handler=_READS_DISPATCHER,
            adapter="dispatcher",
            init="none",
        ),
        Route(
            "get-file-impact",
            group="field_reads",
            parser_factory=_core_factory("get-file-impact"),
            handler=f"{_FIELD_READS_MOD}:file_impact_cli",
            adapter="argv_tracker",
            init="full",
        ),
        Route(
            "get-verify-commands",
            group="field_reads",
            parser_factory=_core_factory("get-verify-commands"),
            handler=f"{_FIELD_READS_MOD}:verify_commands_cli",
            adapter="argv_tracker",
            init="full",
        ),
        Route(
            "exists",
            group="lookups",
            parser_factory=_core_factory("exists"),
            handler=f"{_LOOKUPS_MOD}:exists_cli",
            adapter="argv_tracker",
            init="full",
        ),
        Route(
            "resolve",
            group="lookups",
            parser_factory=_core_factory("resolve"),
            handler=f"{_LOOKUPS_MOD}:resolve_cli",
            adapter="argv_tracker",
            init="full",
        ),
        Route(
            "format",
            group="lookups",
            parser_factory=_core_factory("format"),
            handler=f"{_LOOKUPS_MOD}:format_cli",
            adapter="argv_tracker_root",
            init="full",
        ),
        Route(
            "list-descendants",
            group="descendants",
            parser_factory=_core_factory("list-descendants"),
            handler="rebar._engine_support.descendants:list_descendants_cli",
            adapter="argv_tracker",
            init="full",
        ),
        Route(
            "clarity-check",
            group="gates",
            warn_cross_session=True,
            parser_factory=_core_factory("clarity-check"),
            handler=f"{_GATES_MOD}:clarity_check_cli",
            adapter="argv_tracker_root",
            init="none",
        ),
        Route(
            "check-ac",
            group="gates",
            warn_cross_session=True,
            parser_factory=_core_factory("check-ac"),
            handler=f"{_GATES_MOD}:check_ac_cli",
            adapter="argv_tracker",
            init="none",
        ),
        Route(
            "quality-check",
            group="gates",
            parser_factory=_core_factory("quality-check"),
            handler=f"{_GATES_MOD}:quality_check_cli",
            adapter="argv_tracker",
            init="none",
        ),
        Route(
            "summary",
            group="gates",
            parser_factory=_core_factory("summary"),
            handler=f"{_GATES_MOD}:summary_cli",
            adapter="argv_tracker",
            init="none",
        ),
        Route(
            "sign",
            group="signing",
            parser_factory=_core_factory("sign"),
            handler="rebar.signing:sign_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "verify-signature",
            group="signing",
            parser_factory=_core_factory("verify-signature"),
            handler="rebar.signing:verify_signature_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "compact",
            group="compact",
            parser_factory=_core_factory("compact"),
            handler="rebar._commands.compact:compact_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "compact-all",
            group="compact",
            parser_factory=_core_factory("compact-all"),
            handler="rebar._commands.compact:compact_all_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "reclaim-collapse",
            group="static_read",
            no_auto_mount=True,
            parser_factory=_core_factory("reclaim-collapse"),
            handler="rebar._commands.reclaim_collapse:cli",
            adapter="argv",
        ),
        Route(
            "export",
            group="io",
            parser_factory=_core_factory("export"),
            handler="rebar._io._cli:export_cli",
            adapter="argv",
            init="init_only",
        ),
        Route(
            "import",
            group="io",
            parser_factory=_core_factory("import"),
            handler="rebar._io._cli:import_cli",
            adapter="argv",
            init="full",
        ),
    )


def _lifecycle() -> tuple[Route, ...]:
    return (
        Route(
            "transition",
            group="lifecycle",
            warn_cross_session=True,
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("transition"),
            handler="rebar._commands.transition:transition_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "reopen",
            group="lifecycle",
            warn_cross_session=True,
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("reopen"),
            handler="rebar._commands.transition:reopen_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "claim",
            group="lifecycle",
            confirmable=True,
            legacy_output=True,
            parser_factory=_core_factory("claim"),
            handler="rebar._commands.transition:claim_cli",
            adapter="argv",
            init="full",
        ),
    )


def _writes_full() -> tuple[Route, ...]:
    legacy = {"create", "idea"}
    # (spelling, warns-cross-session) — the flag rides the definition site so a rename
    # carries the warning with it (mirror F11).
    names = (
        ("create", False),
        ("idea", False),
        ("comment", True),
        ("link", True),
        ("unlink", True),
        ("revert", False),
        ("edit", True),
        ("tag", True),
        ("untag", True),
        ("archive", True),
        ("set-file-impact", True),
        ("set-verify-commands", False),
        ("attach-commits", False),
        ("session-log", False),
    )
    return tuple(
        Route(
            name,
            group="writes_full",
            confirmable=True,
            legacy_output=name in legacy,
            parser_factory=_core_factory(name),
            handler="rebar._commands:main",
            adapter="dispatcher",
            init="full",
            warn_cross_session=warn,
        )
        for name, warn in names
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
    # RP-05 S6 execution metadata: each intercept names a LAZY ``"module:attr"``
    # handler (resolved only at dispatch, never at registry construction), invoked
    # through the ``argv`` adapter (``handler(rest)``) with no auto-init — the two
    # intercepts that need init or an extra argument (``identity``/``enrich``) do it
    # inside their own ``rebar._cli`` wrapper.
    _LLM = "rebar._cli._llm_commands"
    handlers: dict[str, str] = {
        "review-code": f"{_LLM}:_review_code",
        "scan-spec": f"{_LLM}:_scan_spec",
        "verify-completion": f"{_LLM}:_verify_completion",
        "review-plan": f"{_LLM}:_review_plan",
        "sign-review": f"{_LLM}:_sign_review",
        "enrich": "rebar._cli:_enrich",
        "explain": f"{_LLM}:_explain",
        "verify-commit-ticket": "rebar._commands.verify_commit:cli",
        "verify-identity": "rebar._commands.verify_authorship:cli",
        "verify-authorship": "rebar._commands.verify_authorship:cli",
        "verify-opcert": "rebar._commands.verify_opcert:cli",
        "trusted-env": "rebar._commands.trusted_env_cmd:cli",
        "remote-cert": "rebar._commands.remote_cert:cli",
        "workflow": "rebar._cli._workflow_commands:_workflow",
        "llm": f"{_LLM}:_llm",
        "jira-onboard": "rebar._cli._jira_onboard:jira_onboard",
        "prompt": f"{_LLM}:_prompt",
        "criteria": f"{_LLM}:_criteria",
        "identity": "rebar._cli:_identity_intercept",
        "config": "rebar._commands.show_config:config_cli",
    }
    names = tuple(factories)
    routes = [
        Route(
            name,
            group="intercept",
            intercept=True,
            parser_factory=factories[name],
            handler=handlers[name],
            adapter="argv",
            init="none",
        )
        for name in names
    ]
    routes.append(
        Route(
            "metrics",
            group="static_read",
            intercept=True,
            parser_factory=f"{_P}.metrics:build",
            handler="rebar._commands.metrics:metrics_cli",
            adapter="argv",
            init="init_only",
        )
    )
    routes.append(
        Route(
            "tracker-footprint",
            group="static_read",
            intercept=True,
            no_auto_mount=True,
            parser_factory=f"{_P}.tracker_footprint:build",
            handler="rebar._commands.tracker_footprint:tracker_footprint_cli",
            adapter="argv",
            init="none",
        )
    )
    routes.append(
        Route(
            "audit",
            group="static_read",
            intercept=True,
            parser_factory=f"{_P}.audit:build",
            handler="rebar._cli._audit_commands:audit_cli",
            adapter="argv",
            init="none",
        )
    )
    return tuple(routes)


def _bridge_and_arms() -> tuple[Route, ...]:
    _P = "rebar._cli._parsers.advanced"
    _BRIDGE_CMDS = "rebar._cli._bridge_commands"
    return (
        Route(
            "bridge",
            group="bridge",
            parser_factory=f"{_P}.bridge:build",
            handler=f"{_BRIDGE_CMDS}:bridge_cli",
            adapter="argv",
            init="none",
        ),
        Route(
            "bridge-status",
            group="bridge",
            hidden=True,
            parser_factory=f"{_P}.bridge:build",
            handler=f"{_BRIDGE_CMDS}:bridge_cli",
            adapter="argv",
            init="none",
            argv_prefix=("status",),
        ),
        Route(
            "bridge-fsck",
            group="bridge",
            parser_factory=f"{_P}.bridge_arms:build_fsck",
            handler=f"{_BRIDGE_CMDS}:bridge_fsck_cli",
            adapter="argv",
            init="none",
        ),
        Route(
            "init",
            group="bootstrap",
            no_auto_mount=True,
            parser_factory=_core_factory("init"),
            handler="rebar._commands.init:init_cli",
            adapter="argv",
            init="none",
        ),
        Route(
            "scratch",
            group="bootstrap",
            no_auto_mount=True,
            parser_factory=_core_factory("scratch"),
            handler="rebar._commands.scratch:scratch_cli",
            adapter="argv",
            init="none",
        ),
        Route(
            "delete",
            group="delete",
            parser_factory=_core_factory("delete"),
            handler="rebar._commands.delete:delete_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "fsck",
            group="repair",
            parser_factory=_core_factory("fsck"),
            handler="rebar._commands.fsck:fsck_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "fsck-recover",
            group="repair",
            parser_factory=_core_factory("fsck-recover"),
            handler="rebar._commands.fsck_recover:fsck_recover_cli",
            adapter="argv",
            init="fsck_recover",
        ),
        Route(
            "tracker-maintenance",
            group="repair",
            parser_factory=_core_factory("tracker-maintenance"),
            handler="rebar._commands.tracker_maintenance:tracker_maintenance_cli",
            adapter="argv",
            init="full",
        ),
        Route(
            "doctor",
            group="repair",
            parser_factory=_core_factory("doctor"),
            handler="rebar._commands.doctor:doctor_cli",
            adapter="argv",
            init="doctor",
        ),
        Route(
            "bridge-probe",
            group="repair",
            parser_factory=f"{_P}.bridge_arms:build_probe",
            handler="rebar._cli:_bridge_probe",
            adapter="argv",
            init="none",
        ),
        Route(
            "grounding-info",
            group="static_read",
            parser_factory=_core_factory("grounding-info"),
            handler="rebar._cli:_grounding_info",
            adapter="argv",
            init="none",
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


def _index(routes: tuple[Route, ...]) -> tuple[dict[str, Route], dict[str, Route]]:
    """Build ``(by_name, by_alias)`` lookup tables for ``routes``.

    ``by_alias`` maps each alias to its owning route for LIVE (non-retired) routes
    only: a retired spelling is unrouted, so its aliases must not resolve either
    (mirrors :func:`derive_policy_sets`' live-only filter).
    """
    by_name = {route.name: route for route in routes}
    by_alias = {alias: route for route in routes if not route.retired for alias in route.aliases}
    return by_name, by_alias


_BY_NAME, _BY_ALIAS = _index(ROUTES)


def route_for(spelling: str, routes: tuple[Route, ...] = ROUTES) -> Route | None:
    """Return the :class:`Route` for a canonical name or alias ``spelling`` (or ``None``).

    Canonical names take precedence over aliases: a canonical spelling always wins,
    and an alias resolves only when no route claims it as a canonical name.
    (Validation forbids an alias colliding with a live canonical name, so this
    precedence is defensive.) An alias never resolves to a retired route, since the
    alias index excludes retired routes; a retired route's own canonical name still
    resolves via the name index. Pass ``routes`` to resolve against a table other
    than the shipped ``ROUTES``.
    """
    by_name, by_alias = (_BY_NAME, _BY_ALIAS) if routes is ROUTES else _index(routes)
    return by_name.get(spelling) or by_alias.get(spelling)


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
    derived["_WARN_CROSS_SESSION"] = frozenset(r.name for r in live if r.warn_cross_session)
    return derived


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
