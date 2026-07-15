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
from dataclasses import dataclass, field
from typing import Any

from rebar._deprecations import raise_or_warn_cfg_key, warn_deprecated

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


class ConfigError(ValueError):
    """A config value is invalid. Raised at load time so problems fail fast at one
    site rather than surfacing deep in unrelated logic."""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _src(source: str) -> str:
    return f" ({source})" if source else ""


def _as_bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(f"{key}: expected a boolean, got {v!r}")


def _as_int(v: Any, key: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(v, bool):  # bool is an int subclass — reject to catch e.g. true→1
        raise ConfigError(f"{key}: expected an integer, got boolean {v!r}")
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected an integer, got {v!r}") from None
    if minimum is not None and i < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {i}")
    if maximum is not None and i > maximum:
        raise ConfigError(f"{key}: must be <= {maximum}, got {i}")
    return i


def _as_float(
    v: Any, key: str, *, minimum: float | None = None, maximum: float | None = None
) -> float:
    if isinstance(v, bool):
        raise ConfigError(f"{key}: expected a number, got boolean {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected a number, got {v!r}") from None
    if minimum is not None and f < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {f}")
    if maximum is not None and f > maximum:
        raise ConfigError(f"{key}: must be <= {maximum}, got {f}")
    return f


def _as_str(v: Any, key: str) -> str:
    if isinstance(v, (dict, list)):
        raise ConfigError(f"{key}: expected a string, got {type(v).__name__}")
    return str(v)


def _as_str_tuple(v: Any, key: str) -> tuple[str, ...]:
    """A tuple of non-empty, trimmed strings from either a TOML array or a comma-separated
    string, so both ``key = ["T5c", "T10"]`` and ``key = "T5c, T10"`` parse. Empty entries are
    dropped; a non-list/non-str value is rejected. Used for config-backed id sets (e.g.
    ``verify.completion_preserve_criteria``)."""
    if isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v]
    elif isinstance(v, str):
        items = [p.strip() for p in v.split(",")]
    else:
        raise ConfigError(
            f"{key}: expected a list or comma-separated string, got {type(v).__name__}"
        )
    return tuple(x for x in items if x)


def _as_choice(v: Any, key: str, choices: set[str]) -> str:
    s = str(v).strip().lower()
    if s not in choices:
        raise ConfigError(f"{key}: expected one of {sorted(choices)}, got {v!r}")
    return s


# Characters git's check-ref-format forbids anywhere in a ref component.
_BAD_REF_CHARS = set(" ~^:?*[\\\x7f") | {chr(c) for c in range(0x20)}


def _as_git_ref(v: Any, key: str) -> str:
    """Validate a single-level git branch name against a `git check-ref-format`-style
    rule set (the subset that matters for a branch): reject empty, whitespace, `..`,
    a leading `-` or `.`, any of ``~^:?*[\\`` / control / DEL chars, an ``@{`` sequence,
    a bare ``@``, a trailing ``/`` / ``.lock`` / ``.``, a leading/trailing/double slash,
    and a component beginning with ``.``. Keeps the tracker branch a valid, pushable ref."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: branch name must not be empty")
    if s == "@" or "@{" in s or ".." in s:
        raise ConfigError(f"{key}: invalid branch name {s!r} (contains '@', '@{{', or '..')")
    if s.startswith("-") or s.startswith("/") or s.endswith("/") or "//" in s:
        raise ConfigError(f"{key}: invalid branch name {s!r} (bad slash placement or leading '-')")
    if s.endswith("."):  # per-component '.lock' is caught by the loop below
        raise ConfigError(f"{key}: invalid branch name {s!r} (ends with '.')")
    bad = sorted(_BAD_REF_CHARS & set(s))
    if bad:
        raise ConfigError(f"{key}: invalid branch name {s!r} (forbidden char(s) {bad})")
    for comp in s.split("/"):
        if not comp or comp.startswith(".") or comp.endswith(".lock"):
            raise ConfigError(f"{key}: invalid branch name {s!r} (bad path component {comp!r})")
    return s


def _as_git_remote(v: Any, key: str) -> str:
    """Validate a git REMOTE NAME (e.g. ``origin``, ``gerrit``, ``github``). Distinct from
    :func:`_as_git_ref` (a branch name): a remote name is a single-level token that becomes
    a path component under ``refs/remotes/<name>/`` and is passed as a positional to
    ``git push``/``fetch``. Reject empty/whitespace, a leading ``-`` (would parse as a
    flag), any ``/`` (remote names are single-level), ``..``, and the
    check-ref-format-forbidden chars (space, ``~^:?*[\\``, control, DEL). Dots and
    (non-leading) hyphens are allowed, so ``my-remote`` / ``gerrit.example`` pass."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: git remote name must not be empty")
    if s.startswith("-") or "/" in s or ".." in s:
        raise ConfigError(f"{key}: invalid git remote name {s!r} (leading '-', '/', or '..')")
    bad = sorted(_BAD_REF_CHARS & set(s))
    if bad:
        raise ConfigError(f"{key}: invalid git remote name {s!r} (forbidden char(s) {bad})")
    return s


def _as_tracker_dir(v: Any, key: str) -> str:
    """Validate the tracker store dir. Allows a bare relative name (the common case,
    e.g. ``.tickets-tracker`` — used as the repo-root symlink name + gitignore entry)
    AND an absolute path (the supported relocated/decoupled store, EV-3b, set via
    ``REBAR_TRACKER_DIR``). Rejects empty/whitespace, any ``..`` traversal component,
    and control chars — values that would break the symlink/exclude or escape the repo."""
    s = _as_str(v, key).strip()
    if not s:
        raise ConfigError(f"{key}: tracker dir must not be empty")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        raise ConfigError(f"{key}: tracker dir {s!r} contains control characters")
    parts = s.replace("\\", "/").split("/")
    if ".." in parts:
        raise ConfigError(f"{key}: tracker dir {s!r} must not contain a '..' traversal component")
    return s


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
    for k in leftover:
        logger.warning(
            "rebar config%s: unknown key '%s.%s' ignored (typo? see docs/config.md)",
            _src(source),
            section,
            k,
        )


@dataclass
class VerifyConfig:
    # Opt-in completion-verification close gate: when true, closing a work ticket runs the
    # LLM completion-verifier (rebar.llm.verify_completion) and blocks on FAIL / unavailable
    # LLM (fail-closed; --force-close bypasses without signing). On PASS the verdict is signed.
    # Default off.
    require_completion_verification_for_close: bool = False
    # Opt-in plan-review gate (epic 5fd2): when true, claiming a work ticket
    # (open→in_progress) requires a fresh, certified plan-review attestation (run
    # `rebar review-plan <id>` to earn one). Absent / stale (code-HEAD moved) /
    # material-edited signatures BLOCK the claim; `--force` bypasses with a logged
    # justification. A FAST local HMAC check only — no LLM on the claim path. Bugs
    # and session_logs are exempt. Default off ⇒ `claim` keeps today's behavior;
    # turning it off is the rollback (an ordinary preference, no kill-switch needed).
    require_plan_review_for_claim: bool = False
    # Opt-in store-wide cross-ticket overlap detection (epic only-crave-art). When true, the
    # plan-review invocation runs an ADVISORY store-wide overlap step (enrich → BM25F retrieve
    # → pairwise judge) that surfaces ≤3 candidate duplicate/supersede/dependency link
    # suggestions in a separate `overlap[]` verdict key. NEVER blocks claim, never affects the
    # claim-gate verdict. Default off; all tunables live on LLMConfig (`[tool.rebar.llm]`).
    overlap_enabled: bool = False
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


@dataclass
class SyncConfig:
    push: str = "always"  # always | async | off
    pull: str = "on"  # on | off
    remote: str = "origin"  # git remote the tickets branch syncs to (push/fetch/fsck)


@dataclass
class McpConfig:
    readonly: bool = False
    allow_llm: bool = False
    allow_jira_sync: bool = False
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
    # Lease (seconds) the ref-backend pass-lock holds; the heartbeat renews at
    # max(1, lease // 3). Consumed by the refs/reconciler/* CAS lock (epic
    # dust-troth-naval / ADR 0031), the only pass-lock backend.
    lock_lease_secs: int = 120
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


@dataclass
class JiraConfig:
    url: str = ""
    user: str = ""
    project: str = ""


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
    branch: str = "tickets"


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
}

# section -> {key -> coercer(value, dotted_key) -> coerced value (raises ConfigError)}
_SECTIONS: dict[str, dict] = {
    "verify": {
        "require_completion_verification_for_close": lambda v, k: _as_bool(v, k),
        "require_plan_review_for_claim": lambda v, k: _as_bool(v, k),
        "overlap_enabled": lambda v, k: _as_bool(v, k),
        "require_ticket_for_commit": lambda v, k: _as_bool(v, k),
        "enable_code_review": lambda v, k: _as_bool(v, k),
        "verify_window_headroom": lambda v, k: _as_float(v, k, minimum=0.1, maximum=1.0),
        "remediation_window_minutes": lambda v, k: _as_int(v, k, minimum=1),
        "novelty_drop_threshold": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "novelty_priority_floor": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "completion_priority_floor": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "completion_preserve_criteria": lambda v, k: _as_str_tuple(v, k),
        "completion_floor_active": lambda v, k: _as_bool(v, k),
        "require_environment": lambda v, k: _as_str(v, k),
        "opcert_enforce_since": lambda v, k: _as_str(v, k),
        "opcert_remote_url": lambda v, k: _as_str(v, k),
    },
    "identity": {
        "require_authenticated": lambda v, k: _as_bool(v, k),
        "signing_key": lambda v, k: _as_str(v, k),
        "enforce_since": lambda v, k: _as_str(v, k),
    },
    "ticket": {
        "display_mode": lambda v, k: _as_str(v, k) or "auto",
        "default_assignee": lambda v, k: _as_str(v, k),
    },
    "ticket_clarity": {"threshold": lambda v, k: _as_int(v, k, minimum=1)},
    "compact": {
        "threshold": lambda v, k: _as_int(v, k, minimum=1),
        "COMPACTION_HORIZON_NS": lambda v, k: _as_int(v, k, minimum=0),
    },
    "sync": {
        "push": lambda v, k: _as_choice(v, k, {"always", "async", "off"}),
        "pull": lambda v, k: _as_choice(v, k, {"on", "off"}),
        "remote": lambda v, k: _as_git_remote(v, k),
    },
    "mcp": {
        "readonly": lambda v, k: _as_bool(v, k),
        "allow_llm": lambda v, k: _as_bool(v, k),
        "allow_jira_sync": lambda v, k: _as_bool(v, k),
        "transport": lambda v, k: _as_choice(v, k, {"stdio", "http"}),
        "http_host": lambda v, k: _as_str(v, k),
        "http_port": lambda v, k: _as_int(v, k, minimum=1, maximum=65535),
        "http_path": lambda v, k: _as_str(v, k),
        "http_allowed_hosts": lambda v, k: _as_str_tuple(v, k),
        "http_allowed_origins": lambda v, k: _as_str_tuple(v, k),
        "http_tls_at_edge": lambda v, k: _as_bool(v, k),
        "allow_unauthenticated_http": lambda v, k: _as_bool(v, k),
        "auth_enabled": lambda v, k: _as_bool(v, k),
        "auth_strategies": lambda v, k: _as_str_tuple(v, k),
        "auth_issuer_url": lambda v, k: _as_str(v, k),
        "auth_resource_server_url": lambda v, k: _as_str(v, k),
        "auth_required_scopes": lambda v, k: _as_str_tuple(v, k),
        "auth_static_tokens_file": lambda v, k: _as_str(v, k),
        "auth_jwt_jwks_uri": lambda v, k: _as_str(v, k),
        "auth_jwt_issuer": lambda v, k: _as_str(v, k),
        "auth_jwt_algorithms": lambda v, k: _as_str_tuple(v, k),
        "auth_jwt_leeway": lambda v, k: _as_int(v, k, minimum=0),
        "auth_jwt_jwks_refetch_cooldown": lambda v, k: _as_int(v, k, minimum=0),
        "auth_jwt_jwks_timeout": lambda v, k: _as_int(v, k, minimum=1),
        "auth_jwt_expected_typ": lambda v, k: _as_str(v, k),
        "auth_jwt_allow_private_jwks_host": lambda v, k: _as_bool(v, k),
        "auth_introspection_endpoint": lambda v, k: _as_str(v, k),
        "auth_introspection_client_id": lambda v, k: _as_str(v, k),
        "auth_introspection_client_secret_env": lambda v, k: _as_str(v, k),
        "auth_introspection_allow_private_host": lambda v, k: _as_bool(v, k),
        "auth_introspection_allow_missing_aud": lambda v, k: _as_bool(v, k),
        "auth_proxy_secret_env": lambda v, k: _as_str(v, k),
        "auth_proxy_secret_header": lambda v, k: _as_str(v, k),
        "auth_proxy_identity_header": lambda v, k: _as_str(v, k),
        "auth_proxy_scopes": lambda v, k: _as_str_tuple(v, k),
        "auth_custom_import": lambda v, k: _as_str(v, k),
    },
    "ui": {
        "enabled": lambda v, k: _as_bool(v, k),
    },
    "reconciler": {
        "jira_cli_timeout": lambda v, k: _as_int(v, k, minimum=0),
        "lock_lease_secs": lambda v, k: _as_int(v, k, minimum=1),
        "deletion_probe_limit": lambda v, k: _as_int(v, k, minimum=1),
        "id_guard_bypass_unsafe": lambda v, k: _as_bool(v, k),
        "max_acting_fraction": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
    },
    "jira": {
        "url": lambda v, k: _as_str(v, k),
        "user": lambda v, k: _as_str(v, k),
        "project": lambda v, k: _as_str(v, k),
    },
    "scratch": {"base_dir": lambda v, k: _as_str(v, k)},
    "tracker": {
        "dir": lambda v, k: _as_tracker_dir(v, k),
        "branch": lambda v, k: _as_git_ref(v, k),
    },
    "ensure": {
        "hint_interval_secs": lambda v, k: _as_int(v, k, minimum=0),
        "hint_enabled": lambda v, k: _as_bool(v, k),
    },
}

# section -> {deprecated_key -> canonical_key}. Empty since the pre-1.0 breaking
# removal (DE7) dropped verify.require_verdict_for_close; kept as the extension point
# for any future config-key rename window (the coerce_sparse loop below consumes it).
_ALIASES: dict[str, dict[str, str]] = {}

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
