"""Typed core configuration schema and section registry.

The dataclasses define nonsecret core settings and their defaults.
``Config.from_mapping`` applies the coercers registered for each section. The
public ``rebar.config`` module reexports this surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TypeVar

from rebar._config_coercion import ConfigError as ConfigError
from rebar._config_coercion import InsecureUrlError as InsecureUrlError
from rebar._config_coercion import _as_bool as _as_bool
from rebar._config_coercion import _as_choice as _as_choice
from rebar._config_coercion import _as_float as _as_float
from rebar._config_coercion import _as_git_ref as _as_git_ref
from rebar._config_coercion import _as_git_remote as _as_git_remote
from rebar._config_coercion import _as_int as _as_int
from rebar._config_coercion import _as_str as _as_str
from rebar._config_coercion import _as_str_list as _as_str_list
from rebar._config_coercion import _as_str_tuple as _as_str_tuple
from rebar._config_coercion import _as_tracker_dir as _as_tracker_dir
from rebar._config_coercion import _src as _src
from rebar._config_coercion import (
    _validate_https_url,
    _validate_reconciler_tls,
)
from rebar._config_coercion import _warn_unknown as _warn_unknown
from rebar._config_sections import _ALIASES as _ALIASES
from rebar._config_sections import _RESERVED_SECTIONS as _RESERVED_SECTIONS
from rebar._config_sections import _SECTIONS as _SECTIONS
from rebar._config_sections import coerce_sparse as coerce_sparse
from rebar._config_sections import merge_sparse as merge_sparse

logger = logging.getLogger("rebar.config")

_FieldValue = TypeVar("_FieldValue")


def _documented(default: _FieldValue, description: str) -> _FieldValue:
    """Build a dataclass field carrying its client-facing description."""
    return field(default=default, metadata={"public_description": description})


# Typed settings remain in this file because static configuration-read checks inspect it.


@dataclass
class VerifyConfig:
    max_ticket_description_chars: int = _documented(
        8_000,
        "Sets the ticket description limit used by plan review and completion verification.",
    )
    enforce_plan_material_pins: bool = _documented(
        False,
        "Requires plan-review signatures to pin reviewed ticket material.",
    )
    # read-via: _commands/gates.py gate_enabled string key
    require_completion_verification_for_close: bool = _documented(
        False,
        "Requires a passing completion verification before a work ticket can close.",
    )
    completion_pinned_ticket_view: bool = _documented(
        False,
        "Uses the experimental non-epic lazy ticket view and atomic completion-close bundle "
        "when sync.push is always.",
    )
    # read-via: _commands/gates.py string key
    require_plan_review_for_close: bool = _documented(
        False,
        "Requires a current plan-review attestation when a work ticket closes.",
    )
    # read-via: _commands/gates.py string key
    require_plan_review_for_claim: bool = _documented(
        False,
        "Requires a current passing plan-review attestation before a work ticket can be claimed.",
    )
    suggest_duplicate_tickets: bool = _documented(
        False,
        "Adds duplicate, supersession, and dependency suggestions plus recent-title warnings.",
    )
    require_ticket_for_commit: bool = _documented(
        False,
        "Requires each checked commit to reference a ticket that resolves in the store.",
    )
    enable_code_review: bool = _documented(
        False,
        "Enables automatic code-review dispatch without disabling explicit review requests.",
    )
    verify_window_headroom: float = _documented(
        0.8,
        "Limits each plan-review verification request to this fraction of the model window.",
    )
    remediation_window_minutes: int = _documented(
        60,
        "Sets how long an edited plan remains eligible for remediation-mode review.",
    )
    novelty_drop_threshold: float = _documented(
        0.7,
        "Sets the novelty score required before remediation review may drop a finding.",
    )
    novelty_priority_floor: float = _documented(
        0.4,
        "Keeps remediation findings at or above this priority score even when they are novel.",
    )
    completion_priority_floor: float = _documented(
        0.4,
        "Keeps completion findings at or above this priority when the floor is active.",
    )
    completion_preserve_criteria: tuple[str, ...] = _documented(
        ("T5c", "T10"),
        "Names criteria that completion review never drops through the completion floor.",
    )
    completion_floor_active: bool = _documented(
        False,
        "Enables the calibrated completion floor, which may drop eligible low-priority findings.",
    )
    completion_recovery_pool_multiplier: float = _documented(
        1.5,
        "Scales the completion-verifier recovery step pool by the number of reviewed criteria.",
    )
    completion_verify_steps_per_criterion: int = _documented(
        24,
        "Adds this many completion-verifier steps for each reviewed criterion.",
    )
    completion_verify_step_floor_min: int = _documented(
        160,
        "Sets the minimum step budget for a completion-verification run.",
    )
    completion_verify_child_traversal_steps: int = _documented(
        16,
        "Adds this many verifier steps for each child ticket traversed during completion review.",
    )
    completion_verify_fixed_overhead_steps: int = _documented(
        16,
        "Adds a fixed step allowance to every completion-verification run.",
    )
    auto_resume_max: int = _documented(
        2,
        "Limits automatic completion-verification retries after evidence-search exhaustion.",
    )
    # read-via: llm/plan_review/xcheck.py getattr
    contradiction_xcheck_active: bool = _documented(
        False,
        "Enables a cross-check that drops a weaker finding contradicted by another finding.",
    )
    # read-via: llm/plan_review/xcheck.py getattr
    comment_trail_xcheck_active: bool = _documented(
        False,
        "Enables a cross-check that drops findings which repeat matters resolved in comments.",
    )
    require_environment: str | None = _documented(
        None,
        "Restricts operation certificates to the trusted environment with this identifier.",
    )
    opcert_enforce_since: str | None = _documented(
        None,
        "Limits operation-certificate checks to closure commits at this ref or descendants.",
    )
    opcert_remote_url: str | None = _documented(
        None,
        "Routes remote certificate requests to the trusted gate service at this base URL.",
    )


@dataclass
class IdentityConfig:
    # Opt-in authenticated-authorship enforcement (epic gnu-whale-ichor). When true,
    # `rebar verify-authorship` (the CI merge-gate) FAILS if any in-scope mutating event
    # is not a `verified` authored signature, and the UX write-gate refuses a write that
    # cannot be signed (no resolvable identity / no signing key) for the gate-exempt types.
    # Default off ⇒ authorship is advisory (signed best-effort, never enforced). Turning it
    # off is the rollback. Env override: REBAR_IDENTITY_REQUIRE_AUTHENTICATED. Mirrors the
    # verify.require_ticket_for_commit opt-in gate pattern. See docs/llm-framework.md /
    # the identity epic.
    require_authenticated: bool = _documented(
        False,
        "Requires verified authorship for governed ticket mutations and refuses unsigned writes.",
    )
    # Path to the OpenSSH PRIVATE key used to sign event authorship at write time. When set
    # (and a current identity resolves), each event's canonical bytes are signed and the DSSE
    # envelope stored as `author_sig` on the event. Unset ⇒ events are written unsigned (the
    # merge-gate then flags them when require_authenticated is on). Env override:
    # REBAR_IDENTITY_SIGNING_KEY.
    signing_key: str | None = _documented(
        None,
        "Names the OpenSSH private key used to sign ticket mutation events.",
    )
    # Grandfathering boundary for the authorship merge-gate (epic gnu-whale-ichor, AC7). A git
    # ref (commit/tag/branch) on the tracker branch: only events whose introducing commit is
    # `<ref>` or a descendant of it are ENFORCED; pre-existing (ancestor) events are reported
    # but never fail the gate. Unset ⇒ every in-scope event is enforced (no grandfathering).
    # Overridable per-run by `rebar verify-identity --since <ref>`. Env override:
    # REBAR_IDENTITY_ENFORCE_SINCE.
    enforce_since: str | None = _documented(
        None,
        "Limits authorship enforcement to event commits at this tracker ref or its descendants.",
    )


@dataclass
class TicketConfig:
    display_mode: str = _documented(
        "auto",
        "Selects automatic, canonical, alias, or short identifiers for ticket references.",
    )
    # The assignee `claim` falls back to when none is given (story c36c). A LOCAL
    # default written into the claim's EDIT event; the reconciler resolves it to a
    # Jira accountId at sync time, so it should be a Jira-resolvable identity (email
    # / accountId) to survive — a bare ambiguous handle is left unassigned (bug 544e).
    default_assignee: str = _documented(
        "",
        "Supplies the Jira-resolvable assignee used by claim when no assignee is provided.",
    )


@dataclass
class TicketClarityConfig:
    threshold: int = _documented(
        5,
        "Sets the minimum score required for a ticket to pass the clarity check.",
    )  # clarity-check pass threshold (section name matches the
    # legacy flat key `ticket_clarity.threshold`, so it reads with no alias)


@dataclass
class CompactConfig:
    threshold: int = _documented(
        10,
        "Sets the minimum eligible event count that triggers ticket compaction.",
    )
    # RC2b Option 3 (conservative horizon): compaction only folds an event once it is
    # older than this many HLC nanoseconds (``hlc.physical_now() - event_ts >=``). The
    # SNAPSHOT is timestamped at the fold boundary, so younger "hot-edge" events stay
    # live ``*.json`` and sort AFTER the snapshot — a concurrently-appended sub-horizon
    # event that merges in later replays on top instead of being silently dropped by the
    # snapshot's positional skip. Default 1800 s (30 min) in ns.
    COMPACTION_HORIZON_NS: int = _documented(
        1_800_000_000_000,
        "Keeps events newer than this nanosecond horizon outside compaction snapshots.",
    )
    # Legacy signature-mirror retirement (epic dark-acme-lumen, tasks 352b/7ed9). The
    # legacy single-slot ``state['signature']`` mirror was a back-compat projection of the
    # most-recent attestation; the kind-keyed ``state['attestations']`` map is now
    # authoritative and every in-tree consumer reads it. New SNAPSHOTs UNCONDITIONALLY omit
    # the legacy ``signature`` mirror (hardcoded never-emit) — the former CONTRACT-phase
    # rollback toggle ``emit_legacy_signature_mirror`` has been REMOVED. The mirror is still
    # re-derived IN MEMORY on every replay (reducer ``process_signature``), so signature
    # verification keeps working on a compacted ticket; only persistence into new snapshots
    # is gone. See docs/migrations.md "Legacy signature-mirror retirement".
    # The OPERATION-LINKED compaction trigger: the compaction floor for stores with no CI and
    # no cron. async detaches a worker after a close, always folds inline (tests/CI), off
    # disables it. trigger_interval_s bounds the last-sweep staleness arm (6 h, matching the
    # scheduled sweep). Rationale: rebar._commands.compact_trigger; keys: docs/config.md.
    trigger: str = _documented(
        "async",
        "Selects asynchronous, inline, or disabled compaction after qualifying write operations.",
    )  # async | always | off
    trigger_interval_s: int = _documented(
        21_600,
        "Starts a compaction sweep when the prior sweep is older than this many seconds.",
    )


@dataclass
class ReclaimConfig:
    # read-via: config.py reclaim_horizon_days()
    horizon_days: int = _documented(
        30,
        "Sets the remote-anchored reclamation horizon in days.",
    )


@dataclass
class FixtureHealConfig:
    # mechanism-ok: config_key fixture_heal.interval_days — 1cef fixture-heal interval
    # read-via: config.py fixture_heal_interval_days()
    interval_days: int = _documented(
        30,
        "Sets the scheduled fixture-mining heal loop interval in days.",
    )


@dataclass
class SyncConfig:
    # read-via: config.py resolve_push_mode()
    push: str = _documented(
        "always",
        "Selects synchronous, asynchronous, or disabled ticket-store pushes after writes.",
    )  # always | async | off
    pull: str = _documented(
        "on",
        "Controls whether ticket-store reads fetch and integrate remote updates.",
    )  # on | off
    remote: str = _documented(
        "origin",
        "Names the Git remote used to push, fetch, reconcile, and verify the tickets branch.",
    )  # git remote the tickets branch syncs to (push/fetch/fsck)


@dataclass
class McpConfig:
    # read-via: config.py mcp_readonly()
    readonly: bool = _documented(
        False,
        "Prevents MCP tools from mutating tickets while retaining read-only tools.",
    )
    # read-via: mcp_server.py _mcp_gate string key
    allow_llm: bool = _documented(
        False,
        "Allows MCP callers to invoke model-backed review and analysis tools.",
    )
    # read-via: mcp_server.py _mcp_gate string key
    allow_jira_sync: bool = _documented(
        False,
        "Allows MCP callers to apply Jira synchronization writes.",
    )
    # Streamable-HTTP transport (S1): stdio remains the default; "http" selects the
    # optional SDK Streamable-HTTP transport with DNS-rebinding protection + fail-closed
    # startup gates. The http_* keys tune the bind + allowlists; each auto-derives a
    # REBAR_MCP_<KEY_UPPER> env var.
    transport: str = _documented(
        "stdio",
        "Selects stdio or Streamable HTTP for the MCP server transport.",
    )
    http_host: str = _documented(
        "127.0.0.1",
        "Sets the network interface address bound by the MCP HTTP transport.",
    )
    http_port: int = _documented(
        8000,
        "Sets the port bound by the MCP HTTP transport.",
    )
    http_path: str = _documented(
        "/mcp",
        "Sets the request path served by the MCP HTTP transport.",
    )
    http_allowed_hosts: tuple[str, ...] = _documented(
        (),
        "Lists accepted HTTP Host header values for DNS rebinding protection.",
    )
    http_allowed_origins: tuple[str, ...] = _documented(
        (),
        "Lists accepted HTTP Origin header values for cross-origin request protection.",
    )
    http_tls_at_edge: bool = _documented(
        False,
        "Acknowledges that a fronting proxy provides TLS when HTTP binds beyond loopback.",
    )
    allow_unauthenticated_http: bool = _documented(
        False,
        "Permits HTTP startup without token authentication when explicitly enabled.",
    )
    # Authentication seam (S2): OFF by default. When auth_enabled, build_server wires a
    # composite token verifier (the SINGLE audience/fail-closed choke point) to the SDK's
    # Resource-Server support. auth_strategies is the ORDERED, closed vocabulary of verifiers
    # to compose ({static, jwt, introspection, proxy, custom}); S2 ships only `static`. The
    # remaining keys tune the Resource-Server identity + the static-bearer secrets file. Each
    # auto-derives a REBAR_MCP_<KEY_UPPER> env var.
    auth_enabled: bool = _documented(
        False,
        "Requires HTTP credentials accepted by a configured authentication strategy.",
    )
    auth_strategies: tuple[str, ...] = _documented(
        (),
        "Selects the ordered token verification strategies composed by the MCP server.",
    )
    auth_issuer_url: str = _documented(
        "",
        "Advertises the authorization server issuer in protected-resource metadata.",
    )
    auth_resource_server_url: str = _documented(
        "",
        "Sets the audience and protected resource identifier required on accepted tokens.",
    )
    auth_required_scopes: tuple[str, ...] = _documented(
        (),
        "Requires accepted principals to hold every listed scope.",
    )
    auth_static_tokens_file: str = _documented(
        "",
        "Names the JSON file containing static token digests or environment references.",
    )
    # JWKS/JWT verifier (S3): the `jwt` strategy's flat keys. Each auto-derives a
    # REBAR_MCP_<KEY_UPPER> env var. algorithms is asymmetric-only on a JWKS source.
    auth_jwt_jwks_uri: str = _documented(
        "",
        "Sets the HTTPS JWKS endpoint used to verify JWT signatures.",
    )
    auth_jwt_issuer: str = _documented(
        "",
        "Requires accepted JWTs to carry this issuer.",
    )
    auth_jwt_algorithms: tuple[str, ...] = _documented(
        ("RS256", "ES256"),
        "Limits JWT signature verification to these asymmetric algorithms.",
    )
    auth_jwt_leeway: int = _documented(
        60,
        "Allows this many seconds of clock skew when validating JWT timestamps.",
    )
    auth_jwt_jwks_refetch_cooldown: int = _documented(
        30,
        "Sets the minimum seconds between JWKS fetch attempts for unknown key identifiers.",
    )
    auth_jwt_jwks_timeout: int = _documented(
        10,
        "Limits each JWKS request to this many seconds.",
    )
    auth_jwt_expected_typ: str = _documented(
        "",
        "Requires the JWT type header to match this value when configured.",
    )
    auth_jwt_allow_private_jwks_host: bool = _documented(
        False,
        "Allows JWKS retrieval from private, link-local, or loopback addresses.",
    )
    # Introspection verifier (S4): the `introspection` strategy's flat keys (RFC 7662).
    # Each auto-derives a REBAR_MCP_<KEY_UPPER> env var. The client secret is NEVER stored
    # in config — auth_introspection_client_secret_env NAMES the env var holding it.
    auth_introspection_endpoint: str = _documented(
        "",
        "Sets the HTTPS RFC 7662 endpoint used to validate opaque tokens.",
    )
    auth_introspection_client_id: str = _documented(
        "",
        "Identifies the MCP resource server to the token introspection endpoint.",
    )
    auth_introspection_client_secret_env: str = _documented(
        "",
        "Names the environment variable containing the introspection client secret.",
    )
    auth_introspection_allow_private_host: bool = _documented(
        False,
        "Allows token introspection through a private, link-local, or loopback address.",
    )
    auth_introspection_allow_missing_aud: bool = _documented(
        False,
        "Accepts active introspection responses that omit an audience claim.",
    )
    # Trusted-proxy verifier (S5): the `proxy` strategy's flat keys. A fronting proxy
    # (oauth2-proxy / gateway / ALB) authenticates the caller and forwards the identity
    # on a header; rebar trusts it ONLY when a shared-secret header matches. The secret is
    # NEVER stored in config — auth_proxy_secret_env NAMES the env var holding it. Each key
    # auto-derives a REBAR_MCP_<KEY_UPPER> env var.
    auth_proxy_secret_env: str = _documented(
        "",
        "Names the environment variable containing the trusted proxy shared secret.",
    )
    auth_proxy_secret_header: str = _documented(
        "x-proxy-auth",
        "Names the request header carrying the trusted proxy shared secret.",
    )
    auth_proxy_identity_header: str = _documented(
        "x-forwarded-user",
        "Names the request header carrying the identity asserted by the trusted proxy.",
    )
    auth_proxy_scopes: tuple[str, ...] = _documented(
        (),
        "Grants these scopes to principals accepted through the trusted proxy.",
    )
    # Pluggable custom verifier (S6): the `custom` strategy's flat key. A `module:factory`
    # import string resolving to a factory returning a TokenVerifier-shaped object. This is
    # a TRUSTED operator config value that executes code at load — never read from a request.
    # Auto-derives REBAR_MCP_AUTH_CUSTOM_IMPORT.
    auth_custom_import: str = _documented(
        "",
        "Loads a trusted module and factory that supplies a custom token verifier.",
    )


@dataclass
class UiConfig:
    # Gates the optional, read-only audit web UI (`rebar audit serve`, story a3d7).
    # Default OFF: when false, `rebar audit serve` refuses to start and no web
    # dependency is imported. Requires the `nava-rebar[ui]` extra when enabled.
    enabled: bool = _documented(
        False,
        "Allows `rebar audit serve` to start the read-only audit web interface.",
    )


@dataclass
class WarningsConfig:
    cross_session: bool = _documented(
        True,
        "Warns when a mutation targets a ticket whose live claim is held by a DIFFERENT session.",
    )


@dataclass
class ReconcilerConfig:
    jira_cli_timeout: int = _documented(
        0,
        "Limits each Jira CLI call to this many seconds. Zero uses the 120-second process default.",
    )
    # Rich-text cutover flag (story 3388, epic 708d). Selects which client sends
    # RICH rich-text instead of today's plain wire: "off" (default), "cloud", "dc",
    # or "both". Ships defaulting OFF — this is an opt-in per-client cutover, not a
    # 100%-traffic flip — and setting it back to "off" IS the rollback: the codecs
    # return to the plain/identity wire with no capability revert or redeploy.
    rich_text_cutover: str = _documented(
        "off",
        "Selects which Jira clients receive rich-text payloads instead of plain text.",
    )
    # Wall-clock ceiling, in seconds, on ONE pandoc invocation in the Data Center
    # wiki renderer (story 5c0e). One corpus body span pandoc's jira reader for
    # 13.5 minutes at 95.8% CPU, and pypandoc's high-level API sets no timeout at
    # all, so without this a single field can stall a reconcile indefinitely. The
    # 10s default is >30x the observed ~0.3s per-field render and ~80x below that
    # hang, so it cannot fire on healthy input. On expiry the unit degrades to its
    # original Markdown — echo-safe, and no other unit is affected.
    dc_pandoc_timeout_s: float = _documented(
        10.0,
        "Limits one Data Center Pandoc conversion to this many seconds before Markdown fallback.",
    )
    # Which vendor backend the reconciler drives (ADR 0035 §(d) vendor-adapter seam,
    # epic bbf1). Selects the adapter via the in-tree backend registry
    # (rebar_reconciler._backend_registry.select_backend). Only "jira" exists today;
    # a second backend widens the choice-set here when it lands (epic be74). The
    # REBAR_RECONCILER_BACKEND env override is auto-derived from this field.
    backend: str = _documented(
        "jira",
        "Selects the Jira Cloud or Jira Data Center reconciliation adapter.",
    )
    # Lease (seconds) the ref-backend pass-lock holds; the heartbeat renews at
    # max(1, lease // 3). Consumed by the refs/reconciler/* CAS lock (epic
    # dust-troth-naval / ADR 0031), the only pass-lock backend.
    # read-via: _engine/rebar_reconciler/_advisory_lock.py getattr
    lock_lease_secs: int = _documented(
        120,
        "Sets how long the reconciler pass lock remains valid before renewal or takeover.",
    )
    deletion_probe_limit: int = _documented(
        20,
        "Limits the Jira GET probes used to confirm that a remote item was deleted.",
    )
    id_guard_bypass_unsafe: bool = _documented(
        False,
        "Bypasses the reconciler identifier write guard and can permit identifier corruption.",
    )
    # Convergence circuit breaker (epic 3006-e198): refuse a pass whose ACTING
    # decisions (terminal-transition / retire / adopt) exceed this fraction of the
    # binding population. 2026-07-03 census measured 1.14% acting — 8.8× headroom.
    max_acting_fraction: float = _documented(
        0.10,
        "Refuses a pass when acting decisions exceed this fraction of bound tickets.",
    )
    # Convergence rollout retired (story d6bd): the per-binding baseline is now
    # ALWAYS dual-written AND ALWAYS consumed as the outbound field differ's
    # arbitration ancestor (ADR 0026). The former rollout flags
    # (baseline_dual_write / baseline_consumer_swap) ran clean in prod and were
    # removed — the always-on behavior is hardcoded, no config surface remains.

    # --- Data Center connection settings (story J6, epic e369) ---
    # Vendor-neutral (not Cloud's ACLI-driven ``[tool.rebar.jira]``): a future
    # non-Jira backend could reuse the same shape. ``base_url`` is TLS-validated
    # at construction time (``_validate_reconciler_tls``, below) — a non-https
    # scheme raises ``ConfigError`` unless ``allow_insecure`` is set.
    base_url: str = _documented(
        "",
        "Sets the Jira Data Center base URL used by the reconciler adapter.",
    )
    # Overrides ONLY the base_url scheme check above; never relaxes certificate
    # verification (that is ca_bundle / the transport's options["verify"]).
    allow_insecure: bool = _documented(
        False,
        "Allows a cleartext Data Center URL without disabling certificate verification.",
    )
    # Path to an internal/self-signed CA bundle, passed as the DC transport's
    # options["verify"] value (never a bare False — see transport.py's
    # build_client_from_settings). Empty means "use the library's TLS default".
    ca_bundle: str = _documented(
        "",
        "Names the CA bundle used to verify Jira Data Center TLS certificates.",
    )
    # Ceiling (characters) the DC comment sanitizer truncates a comment body to —
    # this instance's `jira.text.field.character.limit`, which is ADMIN-SETTABLE on
    # Data Center (documented range 0-2147483647, where 0 means UNLIMITED). The
    # default is Jira's own default for that property, so a stock instance needs no
    # configuration; raise it here to match an instance whose administrator raised it,
    # or rebar truncates comments Jira would have accepted in full (bug 049e). Env
    # override REBAR_RECONCILER_COMMENT_MAX_CHARS is auto-derived from this field.
    comment_max_chars: int = _documented(
        32767,
        "Truncates Jira Data Center comments to this character limit. Zero disables truncation.",
    )

    def __post_init__(self) -> None:
        _validate_reconciler_tls(self.base_url, self.allow_insecure)


@dataclass
class JiraConfig:
    url: str = _documented(
        "",
        "Sets the Jira Cloud base URL used for API access.",
    )
    user: str = _documented(
        "",
        "Sets the Jira user used for Cloud API access.",
    )
    project: str = _documented(
        "",
        "Sets the legacy default Jira project for tickets without a recorded bridge project.",
    )
    # Overrides ONLY the url scheme check below (parity with reconciler.allow_insecure);
    # never relaxes certificate verification. Env override auto-derives to
    # REBAR_JIRA_ALLOW_INSECURE. Intended for a loopback/trusted test instance (bug bdb8).
    # read-via: JiraConfig.__post_init__ url-scheme check
    allow_insecure: bool = _documented(
        False,
        "Allows a cleartext Jira Cloud URL. It does not disable certificate verification.",
    )

    def __post_init__(self) -> None:
        # Parity with the DC ReconcilerConfig: a cleartext jira.url risks exposing the
        # API token / basic-auth credentials in transit, so reject it unless the operator
        # opts in via jira.allow_insecure (bug bdb8).
        _validate_https_url(
            self.url,
            self.allow_insecure,
            url_label="jira.url",
            override_label="jira.allow_insecure",
        )


@dataclass
class ScratchConfig:
    base_dir: str = _documented(
        "",
        "Sets the scratch workspace base directory. Empty uses `<repo>/.rebar/scratch`.",
    )


@dataclass
class EnsureConfig:
    # Write-path pending-hint (epic odd-vortex-elbow / WS2). When an existing store is
    # behind the idempotent ensure-registry, a covered write emits a best-effort,
    # rate-limited WARNING nudging `rebar fsck --repair`. These tune it; both are
    # auto-derived env vars (REBAR_ENSURE_HINT_INTERVAL_SECS / REBAR_ENSURE_HINT_ENABLED).
    hint_interval_secs: int = _documented(
        86400,
        "Sets the minimum seconds between pending-migration repair hints on writes.",
    )  # min seconds between hints (rate-limit; 24h)
    hint_enabled: bool = _documented(
        True,
        "Controls whether writes warn when the store is behind the ensure registry.",
    )  # kill-switch: false silences the nudge entirely


@dataclass
class TrackerConfig:
    # The ticket event-store worktree/symlink dir (repo-root-relative name by default;
    # an absolute path relocates the store — EV-3b) and the orphan branch the event log
    # lives on. Both default to today's values, so every existing repo is unaffected.
    dir: str = _documented(
        ".tickets-tracker",
        "Sets the worktree or symlink directory containing the ticket event store.",
    )
    # Consumers call config.tickets_branch() instead of reading this field: _commands/fsck.py,
    # _commands/init.py, _engine/rebar_reconciler/_concurrency.py, opcert_service/workspace.py.
    # read-via: config.py tickets_branch()
    branch: str = _documented(
        "tickets",
        "Names the orphan Git branch that stores ticket event history.",
    )


@dataclass
class CodeHealthConfig:
    """Scan roots and module-size policy for the code-health metrics."""

    scan_roots: list[str] = field(
        default_factory=list,
        metadata={
            "public_description": "Lists the directories inspected by code-health metric analyzers."
        },
    )
    # Empty means "every file scc recognises" — the polyglot default. Narrowing it scopes the
    # module-size metric to the file types a project's own size policy governs.
    # read-via: _commands/metrics.py ctx.include_extensions -> metrics/analyzers/scc_loc.py
    include_extensions: list[str] = field(
        default_factory=list,
        metadata={
            "public_description": (
                "Limits module-size metrics to listed extensions. "
                "Empty includes every scc file type."
            )
        },
    )
    size_cap: int | None = _documented(
        None,
        "Sets the line-count cap used for module-size distribution and oversized-module metrics.",
    )
    size_near_fraction: float = _documented(
        0.1,
        "Marks files within this fraction below the size cap as near the limit.",
    )


@dataclass
class Config:
    """The typed core configuration — defaults baked in; build with
    :meth:`from_mapping`. Secrets are NOT here (env/.env only)."""

    verify: VerifyConfig = field(default_factory=VerifyConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    ticket_clarity: TicketClarityConfig = field(default_factory=TicketClarityConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    # read-via: config.py reclaim_horizon_days()
    reclaim: ReclaimConfig = field(default_factory=ReclaimConfig)
    # read-via: config.py fixture_heal_interval_days()
    fixture_heal: FixtureHealConfig = field(default_factory=FixtureHealConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    warnings: WarningsConfig = field(default_factory=WarningsConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    scratch: ScratchConfig = field(default_factory=ScratchConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    ensure: EnsureConfig = field(default_factory=EnsureConfig)
    code_health: CodeHealthConfig = field(default_factory=CodeHealthConfig)

    @classmethod
    def from_mapping(cls, raw: dict | None, *, source: str = "", strict: bool = False) -> Config:
        """Build a Config from a nested mapping (TOML ``[tool.rebar]`` shape): coerce
        + validate present values, apply defaults for the rest, honor legacy
        aliases, and WARN (never silently drop) on unknown sections/keys — or, with
        ``strict=True``, hard-error on them (the post-deprecation cutover). Raises
        :class:`ConfigError` on an invalid value (fail-closed at load)."""
        sparse = coerce_sparse(raw, source=source, strict=strict)
        return cls(**{sect: _SECTION_CLASSES[sect](**vals) for sect, vals in sparse.items()})


# ── schema: the single source of coercion truth (sparse parse + defaults) ─────
_SECTION_CLASSES: dict[str, type] = {
    "verify": VerifyConfig,
    "identity": IdentityConfig,
    "ticket": TicketConfig,
    "ticket_clarity": TicketClarityConfig,
    "compact": CompactConfig,
    "reclaim": ReclaimConfig,
    "fixture_heal": FixtureHealConfig,
    "sync": SyncConfig,
    "mcp": McpConfig,
    "ui": UiConfig,
    "warnings": WarningsConfig,
    "reconciler": ReconcilerConfig,
    "jira": JiraConfig,
    "scratch": ScratchConfig,
    "tracker": TrackerConfig,
    "ensure": EnsureConfig,
    "code_health": CodeHealthConfig,
}
