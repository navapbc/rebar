"""Per-section configuration coercion registry."""

import logging

from rebar._config_coercion import (
    ConfigError,
    _as_bool,
    _as_choice,
    _as_float,
    _as_git_ref,
    _as_git_remote,
    _as_int,
    _as_str,
    _as_str_list,
    _as_str_tuple,
    _as_tracker_dir,
    _src,
    _warn_unknown,
)
from rebar._deprecations import (
    raise_or_warn_cfg_key,
    warn_deprecated,
    warn_deprecated_cfg_keys,
)

logger = logging.getLogger("rebar.config")

# section -> {key -> coercer(value, dotted_key) -> coerced value (raises ConfigError)}
_SECTIONS: dict[str, dict] = {
    "verify": {
        "max_ticket_description_chars": lambda v, k: _as_int(v, k, minimum=1),
        "enforce_plan_material_pins": lambda v, k: _as_bool(v, k),
        "require_completion_verification_for_close": lambda v, k: _as_bool(v, k),
        "completion_pinned_ticket_view": lambda v, k: _as_bool(v, k),
        "require_plan_review_for_close": lambda v, k: _as_bool(v, k),
        "require_plan_review_for_claim": lambda v, k: _as_bool(v, k),
        "suggest_duplicate_tickets": lambda v, k: _as_bool(v, k),
        "require_ticket_for_commit": lambda v, k: _as_bool(v, k),
        "enable_code_review": lambda v, k: _as_bool(v, k),
        "verify_window_headroom": lambda v, k: _as_float(v, k, minimum=0.1, maximum=1.0),
        "remediation_window_minutes": lambda v, k: _as_int(v, k, minimum=1),
        "novelty_drop_threshold": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "novelty_priority_floor": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "completion_priority_floor": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "completion_preserve_criteria": lambda v, k: _as_str_tuple(v, k),
        "completion_floor_active": lambda v, k: _as_bool(v, k),
        "completion_recovery_pool_multiplier": lambda v, k: _as_float(v, k, minimum=1.0),
        "completion_verify_steps_per_criterion": lambda v, k: _as_int(v, k, minimum=1),
        "completion_verify_step_floor_min": lambda v, k: _as_int(v, k, minimum=1),
        "completion_verify_child_traversal_steps": lambda v, k: _as_int(v, k, minimum=0),
        "completion_verify_fixed_overhead_steps": lambda v, k: _as_int(v, k, minimum=0),
        "auto_resume_max": lambda v, k: _as_int(v, k, minimum=0),
        "contradiction_xcheck_active": lambda v, k: _as_bool(v, k),
        "comment_trail_xcheck_active": lambda v, k: _as_bool(v, k),
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
        "trigger": lambda v, k: _as_choice(v, k, {"async", "always", "off"}),
        "trigger_interval_s": lambda v, k: _as_int(v, k, minimum=0),
    },
    "reclaim": {
        "horizon_days": lambda v, k: _as_int(v, k, minimum=1),
    },
    "fixture_heal": {
        "interval_days": lambda v, k: _as_int(v, k, minimum=1),
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
    "warnings": {
        "cross_session": lambda v, k: _as_bool(v, k),
    },
    "reconciler": {
        # Keep in step with the `@register(...)` keys in
        # `rebar_reconciler/adapters/`: a backend that is registered but absent here
        # is UNREACHABLE — the registry resolves it while config rejects it (the
        # state `jira-datacenter` was in between stories J6 and J7, epic e369).
        "backend": lambda v, k: _as_choice(v, k, {"jira", "jira-datacenter"}),
        "rich_text_cutover": lambda v, k: _as_choice(v, k, {"off", "cloud", "dc", "both"}),
        "jira_cli_timeout": lambda v, k: _as_int(v, k, minimum=0),
        "dc_pandoc_timeout_s": lambda v, k: _as_float(v, k, minimum=0.0),
        "lock_lease_secs": lambda v, k: _as_int(v, k, minimum=1),
        "deletion_probe_limit": lambda v, k: _as_int(v, k, minimum=1),
        "id_guard_bypass_unsafe": lambda v, k: _as_bool(v, k),
        "max_acting_fraction": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
        "base_url": lambda v, k: _as_str(v, k),
        "allow_insecure": lambda v, k: _as_bool(v, k),
        "ca_bundle": lambda v, k: _as_str(v, k),
        # `jira.text.field.character.limit`'s own documented range: 0 (= unlimited)
        # through 2147483647 (bug 049e). A negative value is a configuration error,
        # not a synonym for unlimited.
        "comment_max_chars": lambda v, k: _as_int(v, k, minimum=0, maximum=2147483647),
    },
    "jira": {
        "url": lambda v, k: _as_str(v, k),
        "user": lambda v, k: _as_str(v, k),
        "project": lambda v, k: _as_str(v, k),
        "allow_insecure": lambda v, k: _as_bool(v, k),
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
    "code_health": {
        "scan_roots": lambda v, k: _as_str_list(v, k),
        "include_extensions": lambda v, k: _as_str_list(v, k),
        "size_cap": lambda v, k: None if v is None else _as_int(v, k, minimum=0),
        "size_near_fraction": lambda v, k: _as_float(v, k, minimum=0.0, maximum=1.0),
    },
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
# ``mapping`` is the provider-neutral reconciler mapping-vocabulary layer
# (``rebar_reconciler.mapping_config``), resolved by :func:`load_mapping_config`.
_RESERVED_SECTIONS: frozenset[str] = frozenset({"llm", "snapshot", "mapping"})


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
