"""Typed core config schema — the dataclasses + coercion + section tables.

Extracted from :mod:`rebar.config` (a pure structural split; no behavior change).
This module holds the in-memory config SCHEMA: the per-section dataclasses, the
value coercers, :class:`Config` + its :meth:`Config.from_mapping`, and the
``_SECTIONS`` / ``_SECTION_CLASSES`` coercion tables. The loader/discovery/
override/cache machinery and the public ``load_config`` / ``mcp_readonly`` /
``tracker_dir`` … surface stay in :mod:`rebar.config`, which re-exports every
name here so the public API is unchanged (``from rebar.config import X`` still
works for every moved name). Imports only stdlib — a low-level leaf with no
``rebar.*`` deps, so :mod:`rebar.config` can import it with no cycle.

The logger is deliberately named ``"rebar.config"`` (not this module) so the
coercion/unknown-key warnings are byte-identical to before the split.
"""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field

from rebar._config_coercion import (
    ConfigError,
    _src,
    unknown_key_hint,
)
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
from rebar._config_sections import _SECTIONS
from rebar._deprecations import raise_or_warn_cfg_key, warn_deprecated, warn_deprecated_cfg_keys

logger = logging.getLogger("rebar.config")


# ── Typed config (the single source of truth for non-secret settings) ─────────
#
# This is the in-memory schema the config-refinement work (epic a621) builds on:
# a stdlib dataclass (no pydantic-settings — core stays dependency-free) holding
# the CORE config keys. ``from_mapping`` parses a nested mapping (TOML
# ``[tool.rebar]`` shape) into the typed object — coercing types, applying
# defaults, honoring legacy aliases, and WARNING (never silently dropping) on
# unknown keys. The TOML loader + discovery + layering (CLI > env > project >
# user > defaults) and routing the existing reads through this are subsequent
# tasks; ``llm.*`` keys live in the optional ``rebar.llm`` layer (not here) so the
# stdlib core never depends on the agents extra. See docs/config.md.


def _warn_unknown(section: str, leftover: dict, source: str, *, strict: bool = False) -> None:
    """Handle keys left over after coercion (unknown to the schema). During the
    deprecation window (``strict=False``, the default) WARN and ignore them — a typo
    guard that never breaks a working install. Past the cutover (``strict=True``, via
    ``REBAR_CONFIG_UNKNOWN_KEYS=error``) raise so the unknown key is a hard error."""
    if not leftover:
        return
    if strict:
        keys = ", ".join(f"{section}.{k}" for k in leftover)
        raise ConfigError(
            f"rebar config{_src(source)}: unknown key(s) {keys} "
            "(REBAR_CONFIG_UNKNOWN_KEYS=error — remove them or fix the typo)"
        )
    hint = unknown_key_hint()
    for k in leftover:
        logger.warning(
            "rebar config%s: unknown key '%s.%s' ignored (%s)", _src(source), section, k, hint
        )


def _validate_reconciler_tls(base_url: str, allow_insecure: bool) -> None:
    """Reject a non-``https`` ``reconciler.base_url`` unless ``allow_insecure`` is
    set (story J6, epic e369 — the Data Center transport's connection settings).

    Thin wrapper over :func:`_validate_https_url` with the DC field labels; kept as a
    named entry point (referenced by the DC config tests) so the DC behaviour and
    messages are byte-unchanged.

    Applies uniformly to ANY non-``https`` scheme, including ``http://localhost``
    — there is no loopback special case, because the J5 live-harness tests are
    REQUIRED to set ``allow_insecure = true`` explicitly precisely so their
    loopback DC instance exercises this override path rather than silently
    bypassing the validator (see ``tests/external/live_jira_dc/``). An empty
    ``base_url`` (the unset default) is not validated — nothing to check yet.

    ``allow_insecure`` affects ONLY this URL-scheme check; it never relaxes TLS
    CERTIFICATE verification (that is ``reconciler.ca_bundle`` / the transport's
    ``options["verify"]``, entirely independent of this flag).
    """
    _validate_https_url(
        base_url,
        allow_insecure,
        url_label="reconciler.base_url",
        override_label="reconciler.allow_insecure",
    )


class InsecureUrlError(ConfigError):
    """A configured URL uses a cleartext (non-``https``) scheme without an explicit
    ``allow_insecure`` override.

    A SUBCLASS of :class:`ConfigError` so existing ``except ConfigError`` handlers still
    catch it, but distinct so a caller can tell a deliberate security-policy rejection
    apart from a malformed-config parse error — e.g. Cloud's ``resolve_jira_settings``
    lets this PROPAGATE (fail-loud, parity with the DC resolver) while still degrading to
    env on a genuinely malformed config (bug bdb8).
    """


def _validate_https_url(
    url: str, allow_insecure: bool, *, url_label: str, override_label: str
) -> None:
    """Reject a non-``https`` ``url`` unless ``allow_insecure`` is set — the shared TLS
    scheme guard behind both ``reconciler.base_url`` (DC) and ``jira.url`` (Cloud).

    ``url_label`` / ``override_label`` name the offending config key and its override in
    the error/warning so the message is actionable for whichever section called in. An
    empty ``url`` (the unset default) is not validated. ``allow_insecure`` governs the
    URL SCHEME only — it never relaxes TLS certificate verification.
    """
    if not url:
        return
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme == "https":
        return
    if not allow_insecure:
        raise InsecureUrlError(
            f"{url_label}: {url!r} uses scheme {scheme!r}, not 'https' — "
            "a cleartext connection risks exposing credentials (e.g. a Jira PAT or "
            f"API token) in transit. Set {override_label} = true to override (only for a "
            "trusted network, e.g. a loopback test harness)."
        )
    logger.warning(
        "%s %r uses a non-https scheme; %s=true overrides the TLS requirement — this "
        "connection is NOT encrypted and is vulnerable to interception. This does not "
        "relax certificate verification (see the section's ca_bundle).",
        url_label,
        url,
        override_label,
    )


@dataclass
class VerifyConfig:
    # Gate-wide admission limit; historical p99 is 7,500 chars, so 8,000 protects only the tail.
    max_ticket_description_chars: int = 8_000
    # Opt-in material-pin enforcement; off for pre-feature project/attestation compatibility.
    enforce_plan_material_pins: bool = False
    # Opt-in completion-verification close gate: when true, closing a work ticket runs the
    # LLM completion-verifier (rebar.llm.verify_completion) and blocks on FAIL / unavailable
    # LLM (fail-closed; --force bypasses without signing). On PASS the verdict is signed.
    # Default off.
    # read-via: _commands/gates.py gate_enabled string key
    require_completion_verification_for_close: bool = False
    # Opt-in local plan-review close gate. It verifies a separately attested review with the
    # CLOSE validity profile; it never launches an LLM review. Default off.
    # read-via: _commands/gates.py string key
    require_plan_review_for_close: bool = False
    # Opt-in plan-review gate (epic 5fd2): when true, claiming a work ticket
    # (open→in_progress) requires a fresh, certified plan-review attestation (run
    # `rebar review-plan <id>` to earn one). Absent / stale (code-HEAD moved) /
    # material-edited signatures BLOCK the claim; `--force` bypasses with a logged
    # justification. A FAST local HMAC check only — no LLM on the claim path. Bugs
    # and session_logs are exempt. Default off ⇒ `claim` keeps today's behavior;
    # turning it off is the rollback (an ordinary preference, no kill-switch needed).
    # read-via: _commands/gates.py string key
    require_plan_review_for_claim: bool = False
    # Opt-in store-wide duplicate-ticket suggestions (epic only-crave-art). ADVISORY: adds an
    # overlap step (enrich → BM25F retrieve → pairwise judge) surfacing ≤3 duplicate/supersede/
    # dependency link suggestions under an `overlap[]` verdict key; NEVER blocks claim. Cost
    # scales with TRACKER SIZE, not the ticket. Off by default; tunables on LLMConfig
    # (`[tool.rebar.llm] overlap_*`). Pre-rename spelling stays honored — see `_ALIASES`.
    suggest_duplicate_tickets: bool = False
    # Opt-in commit-ticket gate: when true, `rebar verify-commit-ticket` (run in CI, the
    # Gerrit Verified leg) requires every commit message to reference a rebar ticket that
    # RESOLVES in the store (alias/full/short/Jira). Default off; enabled per-project in
    # rebar.toml. Turning it off is the rollback. See docs/commit-ticket-trailer.md.
    require_ticket_for_commit: bool = False
    # Opt-in agentic code-review capability (epic b744): when true, the public
    # `review_code()` (CLI `rebar review-code` / MCP `review_code`) runs the four-pass
    # code-review GATE (`gates/code-review.yaml`) and `produce_code_review_verdict` is live.
    # Default OFF ⇒ INERT — `review_code()` returns a valid empty `review_result` (+ a
    # 'capability disabled' note), zero LLM calls. Source-separated + off-by-default so it has
    # no effect when disabled. Env override: REBAR_VERIFY_ENABLE_CODE_REVIEW.
    enable_code_review: bool = False
    # Progressive drift-refresh (Story 2, epic boil-golem-veto / ADR 0002): on a
    # drift-only-stale re-review, run a cheap E4+G1G2 probe and, if the plan still holds,
    # REFRESH the attestation instead of a full re-review. Always on (operator-authorized
    # 2026-07-12, epic a37b, on the measured token/latency saving); the off switch was
    # retired in story 4cdf.

    # Token-budget headroom for the Pass-2 verify chunker (epic solid-timer-unison WS3): the
    # fraction of the verifier model's context window a single verify request may use before
    # the findings are split into multiple calls. Default 0.8 leaves room for the system
    # prompt + the per-finding structured output. The common case (whole request fits) is one
    # aggregate call; this only triggers on a pathological huge-findings ticket.
    verify_window_headroom: float = 0.8

    # Convergent plan-edit re-review (epic 7d43, child ec89): a re-review of an EDITED plan whose
    # reviewed CODE is unchanged always runs in remediation mode — the full criteria set still
    # runs, but Pass-3 may drop only NOVEL, low-priority findings (the rising floor, child cc5b).
    # Always on (operator-authorized on field evidence, 2026-07-11); the off switch was retired
    # in story 4cdf.
    # The freshness window (minutes) for remediation mode: a re-review is eligible only when the
    # LAST review of any kind was within this many minutes, measured from that last review and
    # RESET on each review (so the loop persists across a series of edits and lapses to a normal
    # full review only after the agent goes idle). Default 60.
    remediation_window_minutes: int = 60

    # Pass-3 rising floor (epic 7d43, child cc5b). On an eligible remediation re-review, a finding
    # is DROPPED iff its novelty >= novelty_drop_threshold AND its priority (validity × impact) <
    # novelty_priority_floor. T_novel default 0.7 (house precision-first). The floor is a scalar at
    # the corpus p40 impact percentile (~0.4, the "below major" band; see
    # scripts/plan_review_impact_distribution.py). Both config-overridable.
    novelty_drop_threshold: float = 0.7
    novelty_priority_floor: float = 0.4
    # The rising floor is always active (shared with the code-review region-gated floor, ADR 0037;
    # operator-authorized on field evidence, 2026-07-11, in lieu of 150b's `discriminates_novelty`
    # eval). It still runs subject to remediation eligibility + per-review self-gates; the off
    # switch (`novelty_drop_active`) was retired in story 4cdf.

    # Pass-3 COMPLETION floor (epic 66ac / story 6533) — the container-completion analogue of the
    # novelty rising floor, for a re-fired epic/story-with-children review. A finding is DROPPED iff
    # its completion sub-answers say it is fully about DELIVERED, settled plan text (attribution = a
    # delivered-now child AND containment = limited-to-closed AND layer = plan-semantics) AND its
    # priority (validity × impact) < completion_priority_floor AND none of its criteria is in the
    # always-preserve set. Every ambiguous/fail-safe sub-answer fails toward KEEP. The floor default
    # (0.4) matches novelty_priority_floor (the corpus "below major" band).
    completion_priority_floor: float = 0.4
    # The always-preserve set: REGISTERED criterion ids a completion drop never touches, regardless
    # of the other axes. Default the security overlay (T5c) + the endpoint/interface-contract
    # criterion (T10) — so a delivered child's "endpoint has no auth" or "contract omits a field"
    # is always kept. Adding privacy/compliance ids is a config change, not code.
    completion_preserve_criteria: tuple[str, ...] = ("T5c", "T10")
    # The EVIDENCE GATE: the completion floor stays inert (gate runs un-floored) until this is
    # flipped true — manually by the operator only after the calibration gold-set (story 77cf) has
    # cleared its must-never-suppress bar. Default False, so the floor never drops a finding by
    # default (the total back-out).
    completion_floor_active: bool = False
    # Completion-recovery banking + criteria-scaled step floor (epic 10ae, story 2948) — FLAT
    # completion_* fields (see verify_step_floor/plan_recovery_pool + docs; pool = multiplier × N).
    completion_recovery_pool_multiplier: float = 1.5
    completion_verify_steps_per_criterion: int = 24
    completion_verify_step_floor_min: int = 160
    # Evidence-surface scaling terms (ticket 8d74): epic criteria traverse child tickets
    # (show_ticket + comments + repo reads each), and every run pays a fixed show_ticket+parse
    # overhead — both sized at 16 steps (mid-band of observed 5-10 requests/child at the
    # 2x steps→requests halving). Consumed by verify_step_floor's child-traversal + overhead
    # terms; 0 disables a term.
    completion_verify_child_traversal_steps: int = 16
    completion_verify_fixed_overhead_steps: int = 16
    # Bounded auto-resume for the completion-verification close gate (ticket b5f8): when a
    # close FAILs on pure evidence-search exhaustion (every unmet criterion carries the
    # framework-set per-criterion `evidence_sufficient: false` marker — nothing positively
    # refuted), the gate re-runs `verify_completion` itself instead of asking the operator to
    # retype the same close; the cross-run verdict cache seeds prior validated PASSes, so each
    # re-run concentrates its budget on the formerly-insufficient criteria. At most this many
    # resumptions per close invocation, and only while the prior attempt strictly increased
    # the cache-credited PASS count (zero progress stops early). 0 disables auto-resume.
    auto_resume_max: int = 2
    # Validation-assessment cross-checks (bug 5e40) — two per-verdict consistency drops that
    # converge a non-deterministic re-review. Each stays inert (the gate runs un-cross-checked, the
    # verdict byte-identical) until flipped true, mirroring completion_floor_active's evidence gate:
    # the mechanism ships off, an operator enables it only after calibration confirms it never
    # suppresses a real finding.
    #   - contradiction_xcheck_active: cross-check the verdict's findings for a MUTUAL contradiction
    #     and drop the contradicted/weaker one (5e40 A1: a false BLOCK refuted by a true advisory).
    #   - comment_trail_xcheck_active: consult the ticket's recorded comment trail and drop a
    #     finding that re-litigates a point the trail already RESOLVED (5e40 B3: rebase:chain).
    contradiction_xcheck_active: bool = False  # read-via: llm/plan_review/xcheck.py getattr
    comment_trail_xcheck_active: bool = False  # read-via: llm/plan_review/xcheck.py getattr
    # Opt-in per-gate required-signing-environment (story 42d1). When set to an env_id, a gate's
    # operation certificate must come from that pinned trusted environment
    # (`.rebar/trusted_environments.yaml`), verified against its out-of-band-pinned key. Default
    # None ⇒ no required environment (the low-security default).
    require_environment: str | None = None
    # Grandfathering boundary for the op-cert merge-gate (`rebar verify-opcert`, story 4214). A git
    # ref (commit/tag/branch) on the tracker branch: only tickets whose close-STATUS introducing
    # commit is `<ref>` or a descendant of it are ENFORCED; pre-existing (ancestor) closures are
    # reported but never fail the gate. Unset ⇒ every in-scope closed ticket is enforced (no
    # grandfathering). Overridable per-run by `rebar verify-opcert --since <ref>`. Mirrors
    # identity.enforce_since for the authorship gate.
    opcert_enforce_since: str | None = None
    # Opt-in trusted op-cert gate service base URL (story ee0b). When set, `rebar remote-cert`
    # routes a gate run to the trusted environment at this URL (which fetches authoritative
    # state itself, runs the gate, and returns a signed op-cert). Unset ⇒ the remote path is
    # simply unavailable and `rebar remote-cert` errors with a clear message; it is NEVER
    # required for any LOCAL op-cert sign/verify path (those stay fully offline). Default None.
    opcert_remote_url: str | None = None


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
    require_authenticated: bool = False
    # Path to the OpenSSH PRIVATE key used to sign event authorship at write time. When set
    # (and a current identity resolves), each event's canonical bytes are signed and the DSSE
    # envelope stored as `author_sig` on the event. Unset ⇒ events are written unsigned (the
    # merge-gate then flags them when require_authenticated is on). Env override:
    # REBAR_IDENTITY_SIGNING_KEY.
    signing_key: str | None = None
    # Grandfathering boundary for the authorship merge-gate (epic gnu-whale-ichor, AC7). A git
    # ref (commit/tag/branch) on the tracker branch: only events whose introducing commit is
    # `<ref>` or a descendant of it are ENFORCED; pre-existing (ancestor) events are reported
    # but never fail the gate. Unset ⇒ every in-scope event is enforced (no grandfathering).
    # Overridable per-run by `rebar verify-identity --since <ref>`. Env override:
    # REBAR_IDENTITY_ENFORCE_SINCE.
    enforce_since: str | None = None


@dataclass
class TicketConfig:
    display_mode: str = "auto"
    # The assignee `claim` falls back to when none is given (story c36c). A LOCAL
    # default written into the claim's EDIT event; the reconciler resolves it to a
    # Jira accountId at sync time, so it should be a Jira-resolvable identity (email
    # / accountId) to survive — a bare ambiguous handle is left unassigned (bug 544e).
    default_assignee: str = ""


@dataclass
class TicketClarityConfig:
    threshold: int = 5  # clarity-check pass threshold (section name matches the
    # legacy flat key `ticket_clarity.threshold`, so it reads with no alias)


@dataclass
class CompactConfig:
    threshold: int = 10
    # RC2b Option 3 (conservative horizon): compaction only folds an event once it is
    # older than this many HLC nanoseconds (``hlc.physical_now() - event_ts >=``). The
    # SNAPSHOT is timestamped at the fold boundary, so younger "hot-edge" events stay
    # live ``*.json`` and sort AFTER the snapshot — a concurrently-appended sub-horizon
    # event that merges in later replays on top instead of being silently dropped by the
    # snapshot's positional skip. Default 1800 s (30 min) in ns.
    COMPACTION_HORIZON_NS: int = 1_800_000_000_000
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
    trigger: str = "async"  # async | always | off
    trigger_interval_s: int = 21_600


@dataclass
class SyncConfig:
    push: str = "always"  # always | async | off  # read-via: config.py resolve_push_mode()
    pull: str = "on"  # on | off
    remote: str = "origin"  # git remote the tickets branch syncs to (push/fetch/fsck)


@dataclass
class McpConfig:
    readonly: bool = False  # read-via: config.py mcp_readonly()
    allow_llm: bool = False  # read-via: mcp_server.py _mcp_gate string key
    allow_jira_sync: bool = False  # read-via: mcp_server.py _mcp_gate string key
    # Streamable-HTTP transport (S1): stdio remains the default; "http" selects the
    # optional SDK Streamable-HTTP transport with DNS-rebinding protection + fail-closed
    # startup gates. The http_* keys tune the bind + allowlists; each auto-derives a
    # REBAR_MCP_<KEY_UPPER> env var.
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    http_path: str = "/mcp"
    http_allowed_hosts: tuple[str, ...] = ()
    http_allowed_origins: tuple[str, ...] = ()
    http_tls_at_edge: bool = False
    allow_unauthenticated_http: bool = False
    # Authentication seam (S2): OFF by default. When auth_enabled, build_server wires a
    # composite token verifier (the SINGLE audience/fail-closed choke point) to the SDK's
    # Resource-Server support. auth_strategies is the ORDERED, closed vocabulary of verifiers
    # to compose ({static, jwt, introspection, proxy, custom}); S2 ships only `static`. The
    # remaining keys tune the Resource-Server identity + the static-bearer secrets file. Each
    # auto-derives a REBAR_MCP_<KEY_UPPER> env var.
    auth_enabled: bool = False
    auth_strategies: tuple[str, ...] = ()
    auth_issuer_url: str = ""
    auth_resource_server_url: str = ""
    auth_required_scopes: tuple[str, ...] = ()
    auth_static_tokens_file: str = ""
    # JWKS/JWT verifier (S3): the `jwt` strategy's flat keys. Each auto-derives a
    # REBAR_MCP_<KEY_UPPER> env var. algorithms is asymmetric-only on a JWKS source.
    auth_jwt_jwks_uri: str = ""
    auth_jwt_issuer: str = ""
    auth_jwt_algorithms: tuple[str, ...] = ("RS256", "ES256")
    auth_jwt_leeway: int = 60
    auth_jwt_jwks_refetch_cooldown: int = 30
    auth_jwt_jwks_timeout: int = 10
    auth_jwt_expected_typ: str = ""
    auth_jwt_allow_private_jwks_host: bool = False
    # Introspection verifier (S4): the `introspection` strategy's flat keys (RFC 7662).
    # Each auto-derives a REBAR_MCP_<KEY_UPPER> env var. The client secret is NEVER stored
    # in config — auth_introspection_client_secret_env NAMES the env var holding it.
    auth_introspection_endpoint: str = ""
    auth_introspection_client_id: str = ""
    auth_introspection_client_secret_env: str = ""
    auth_introspection_allow_private_host: bool = False
    auth_introspection_allow_missing_aud: bool = False
    # Trusted-proxy verifier (S5): the `proxy` strategy's flat keys. A fronting proxy
    # (oauth2-proxy / gateway / ALB) authenticates the caller and forwards the identity
    # on a header; rebar trusts it ONLY when a shared-secret header matches. The secret is
    # NEVER stored in config — auth_proxy_secret_env NAMES the env var holding it. Each key
    # auto-derives a REBAR_MCP_<KEY_UPPER> env var.
    auth_proxy_secret_env: str = ""
    auth_proxy_secret_header: str = "x-proxy-auth"
    auth_proxy_identity_header: str = "x-forwarded-user"
    auth_proxy_scopes: tuple[str, ...] = ()
    # Pluggable custom verifier (S6): the `custom` strategy's flat key. A `module:factory`
    # import string resolving to a factory returning a TokenVerifier-shaped object. This is
    # a TRUSTED operator config value that executes code at load — never read from a request.
    # Auto-derives REBAR_MCP_AUTH_CUSTOM_IMPORT.
    auth_custom_import: str = ""


@dataclass
class UiConfig:
    # Gates the optional, read-only audit web UI (`rebar audit serve`, story a3d7).
    # Default OFF: when false, `rebar audit serve` refuses to start and no web
    # dependency is imported. Requires the `nava-rebar[ui]` extra when enabled.
    enabled: bool = False


@dataclass
class ReconcilerConfig:
    jira_cli_timeout: int = 0
    # Rich-text cutover flag (story 3388, epic 708d). Selects which client sends
    # RICH rich-text instead of today's plain wire: "off" (default), "cloud", "dc",
    # or "both". Ships defaulting OFF — this is an opt-in per-client cutover, not a
    # 100%-traffic flip — and setting it back to "off" IS the rollback: the codecs
    # return to the plain/identity wire with no capability revert or redeploy.
    rich_text_cutover: str = "off"
    # Wall-clock ceiling, in seconds, on ONE pandoc invocation in the Data Center
    # wiki renderer (story 5c0e). One corpus body span pandoc's jira reader for
    # 13.5 minutes at 95.8% CPU, and pypandoc's high-level API sets no timeout at
    # all, so without this a single field can stall a reconcile indefinitely. The
    # 10s default is >30x the observed ~0.3s per-field render and ~80x below that
    # hang, so it cannot fire on healthy input. On expiry the unit degrades to its
    # original Markdown — echo-safe, and no other unit is affected.
    dc_pandoc_timeout_s: float = 10.0
    # Which vendor backend the reconciler drives (ADR 0035 §(d) vendor-adapter seam,
    # epic bbf1). Selects the adapter via the in-tree backend registry
    # (rebar_reconciler._backend_registry.select_backend). Only "jira" exists today;
    # a second backend widens the choice-set here when it lands (epic be74). The
    # REBAR_RECONCILER_BACKEND env override is auto-derived from this field.
    backend: str = "jira"
    # Lease (seconds) the ref-backend pass-lock holds; the heartbeat renews at
    # max(1, lease // 3). Consumed by the refs/reconciler/* CAS lock (epic
    # dust-troth-naval / ADR 0031), the only pass-lock backend.
    lock_lease_secs: int = 120  # read-via: _engine/rebar_reconciler/_advisory_lock.py getattr
    deletion_probe_limit: int = 20
    id_guard_bypass_unsafe: bool = False
    # Convergence circuit breaker (epic 3006-e198): refuse a pass whose ACTING
    # decisions (terminal-transition / retire / adopt) exceed this fraction of the
    # binding population. 2026-07-03 census measured 1.14% acting — 8.8× headroom.
    max_acting_fraction: float = 0.10
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
    base_url: str = ""
    # Overrides ONLY the base_url scheme check above; never relaxes certificate
    # verification (that is ca_bundle / the transport's options["verify"]).
    allow_insecure: bool = False
    # Path to an internal/self-signed CA bundle, passed as the DC transport's
    # options["verify"] value (never a bare False — see transport.py's
    # build_client_from_settings). Empty means "use the library's TLS default".
    ca_bundle: str = ""
    # Ceiling (characters) the DC comment sanitizer truncates a comment body to —
    # this instance's `jira.text.field.character.limit`, which is ADMIN-SETTABLE on
    # Data Center (documented range 0-2147483647, where 0 means UNLIMITED). The
    # default is Jira's own default for that property, so a stock instance needs no
    # configuration; raise it here to match an instance whose administrator raised it,
    # or rebar truncates comments Jira would have accepted in full (bug 049e). Env
    # override REBAR_RECONCILER_COMMENT_MAX_CHARS is auto-derived from this field.
    comment_max_chars: int = 32767

    def __post_init__(self) -> None:
        _validate_reconciler_tls(self.base_url, self.allow_insecure)


@dataclass
class JiraConfig:
    url: str = ""
    user: str = ""
    project: str = ""
    # Overrides ONLY the url scheme check below (parity with reconciler.allow_insecure);
    # never relaxes certificate verification. Env override auto-derives to
    # REBAR_JIRA_ALLOW_INSECURE. Intended for a loopback/trusted test instance (bug bdb8).
    allow_insecure: bool = False  # read-via: JiraConfig.__post_init__ url-scheme check

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
    base_dir: str = ""


@dataclass
class EnsureConfig:
    # Write-path pending-hint (epic odd-vortex-elbow / WS2). When an existing store is
    # behind the idempotent ensure-registry, a covered write emits a best-effort,
    # rate-limited WARNING nudging `rebar fsck --repair`. These tune it; both are
    # auto-derived env vars (REBAR_ENSURE_HINT_INTERVAL_SECS / REBAR_ENSURE_HINT_ENABLED).
    hint_interval_secs: int = 86400  # min seconds between hints (rate-limit; 24h)
    hint_enabled: bool = True  # kill-switch: false silences the nudge entirely


@dataclass
class TrackerConfig:
    # The ticket event-store worktree/symlink dir (repo-root-relative name by default;
    # an absolute path relocates the store — EV-3b) and the orphan branch the event log
    # lives on. Both default to today's values, so every existing repo is unaffected.
    dir: str = ".tickets-tracker"
    # Consumers call config.tickets_branch() instead of reading this field: _commands/fsck.py,
    # _commands/init.py, _engine/rebar_reconciler/_concurrency.py, opcert_service/workspace.py.
    # read-via: config.py tickets_branch()
    branch: str = "tickets"


@dataclass
class CodeHealthConfig:
    """Scan roots and module-size policy for the code-health metrics."""

    scan_roots: list[str] = field(default_factory=list)
    # Empty means "every file scc recognises" — the polyglot default. Narrowing it scopes the
    # module-size metric to the file types a project's own size policy governs.
    # read-via: _commands/metrics.py ctx.include_extensions -> metrics/analyzers/scc_loc.py
    include_extensions: list[str] = field(default_factory=list)
    size_cap: int | None = None
    size_near_fraction: float = 0.1


@dataclass
class Config:
    """The typed core configuration — defaults baked in; build with
    :meth:`from_mapping`. Secrets are NOT here (env/.env only)."""

    verify: VerifyConfig = field(default_factory=VerifyConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    ticket_clarity: TicketClarityConfig = field(default_factory=TicketClarityConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    ui: UiConfig = field(default_factory=UiConfig)
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
    "sync": SyncConfig,
    "mcp": McpConfig,
    "ui": UiConfig,
    "reconciler": ReconcilerConfig,
    "jira": JiraConfig,
    "scratch": ScratchConfig,
    "tracker": TrackerConfig,
    "ensure": EnsureConfig,
    "code_health": CodeHealthConfig,
}

# section -> {deprecated_key -> canonical_key}, consumed by the coerce_sparse loop below. Every
# entry needs a matching `cfg:<section>.<old>` row in rebar._deprecations, else warn_deprecated
# raises. The 9416 entry is a PERMANENT rename: same boolean, untouched configs keep working.
_ALIASES: dict[str, dict[str, str]] = {"verify": {"overlap_enabled": "suggest_duplicate_tickets"}}

# Config sections owned by an OPTIONAL layer rather than the stdlib core typed
# Config — currently ``llm`` (the ``nava-rebar[agents]`` extra; resolved by
# ``rebar.llm.LLMConfig.from_env`` so the stdlib core never imports the agents
# stack). They are RECOGNISED by the core parser — neither warned as unknown nor
# coerced into :class:`Config` — and read raw via :func:`read_reserved_section`.
# ``snapshot`` is the repo-snapshot-isolation gate cache/janitor tunables layer
# (``rebar._snapshot``), resolved env-first by :class:`rebar._snapshot.JanitorConfig`.
_RESERVED_SECTIONS: frozenset[str] = frozenset({"llm", "snapshot"})


def coerce_sparse(raw: dict | None, *, source: str = "", strict: bool = False) -> dict:
    """Coerce+validate a nested mapping into a SPARSE nested dict of ONLY the keys
    actually present (NO defaults applied) — the per-layer building block for
    precedence merging. Resolves legacy aliases (the legacy key is accepted, with a
    deprecation warning, regardless of ``strict``); raises :class:`ConfigError` on an
    invalid value. Unknown sections/keys WARN by default and, with ``strict=True``,
    hard-error (the deprecation cutover). Defaults are applied ONCE, at the end, by
    :meth:`Config.from_mapping` — so a lower-priority layer's default can never
    override a higher layer's explicit value."""
    raw = dict(raw or {})
    out: dict[str, dict] = {}
    for sect, val in raw.items():
        if sect in _RESERVED_SECTIONS:
            continue  # owned by an optional layer (e.g. llm → rebar.llm); not a core key
        if sect not in _SECTIONS:
            if strict:
                raise ConfigError(
                    f"rebar config{_src(source)}: unknown section [{sect}] "
                    "(REBAR_CONFIG_UNKNOWN_KEYS=error)"
                )
            logger.warning("rebar config%s: unknown section [%s] ignored", _src(source), sect)
            continue
        if not isinstance(val, dict):
            raise ConfigError(f"[{sect}]: expected a table/section, got {type(val).__name__}")
        d = dict(val)
        # Tombstoned (REMOVED) TOML keys: route to a targeted RemovedInputError (error)
        # or WARN (warn), BEFORE the generic unknown-key path — a retired lifecycle/gate
        # key must fail loud, not be swallowed as a forward-compat "unknown key". This is
        # separate from the genuinely-unknown-key policy in _warn_unknown.
        for tkey in list(d):
            if raise_or_warn_cfg_key(sect, tkey) is not None:
                d.pop(tkey)  # warn-class: consumed here so _warn_unknown does not re-flag it
        warn_deprecated_cfg_keys(sect, d, renames=_ALIASES.get(sect, {}), logger=logger)
        for old, new in _ALIASES.get(sect, {}).items():
            if old in d:
                if new not in d:
                    warn_deprecated(f"cfg:{sect}.{old}", logger=logger)
                    d[new] = d.pop(old)
                else:
                    d.pop(old)  # canonical key wins
        coerced: dict = {}
        for key, coercer in _SECTIONS[sect].items():
            if key in d:
                coerced[key] = coercer(d.pop(key), f"{sect}.{key}")
        _warn_unknown(sect, d, source, strict=strict)
        if coerced:
            out[sect] = coerced
    return out


def merge_sparse(*layers: dict | None) -> dict:
    """Deep-merge sparse config layers in precedence order — LATER layers win,
    per key. Each layer is a sparse nested dict from :func:`coerce_sparse`."""
    out: dict[str, dict] = {}
    for layer in layers:
        for sect, vals in (layer or {}).items():
            out.setdefault(sect, {}).update(vals)
    return out
