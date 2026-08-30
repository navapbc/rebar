"""The per-operation, authoritative :class:`~rebar.llm.config.LLMConfig` binding.

Ticket ec44 (ADR 0098, completing the ticket-3a08 CLI/command/store cutover for the
LLM surface): one behavior-bearing ``LLMConfig`` per public LLM/gate/workflow
operation, composed exactly once and bound for the whole operation so nested
subcalls and multi-step workflow runs observe the SAME resolved config instead of
re-reading the environment per call.

:func:`compose_and_bind_llm_config` is the LLM-surface counterpart to
:func:`rebar._operation_config.compose_and_bind_operation_snapshot` — same
reentrancy contract (an already-bound config is reused verbatim, never
recomposed), same "explicit input stays authoritative" contract — but it does NOT
share that function's fail-OPEN swallow: a malformed/missing LLM config decides
which provider/credentials/model a live, billable call uses, so composition
failure here propagates (AC3: fail before any external call, no anonymous/
cross-provider fallback) rather than degrading to unbound ambient resolution.

:func:`redacted_snapshot_values` / :func:`llm_config_fingerprint` give the LLM
config a non-secret, fingerprintable projection (AC4) by reusing
:class:`rebar._operation_config.OperationSnapshot`'s own validating constructor —
the same secret/live-object screen the general operation snapshot gets, applied
here to an EXPLICIT ALLOWLIST of known-safe ``LLMConfig`` fields.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator

from rebar.llm.config import LLMConfig, _active_gate_config, gate_config

logger = logging.getLogger(__name__)

# Explicit ALLOWLIST (never a denylist over dataclass fields) of LLMConfig fields
# that are safe to expose in a snapshot/fingerprint/log — a future field added to
# LLMConfig is excluded by default and must be deliberately added here.
_NON_SECRET_LLM_FIELDS: tuple[str, ...] = (
    "runner",
    "model",
    "model_provider",
    "base_url",
    "bedrock_region_name",
    "bedrock_region_source",
    "max_tokens",
    "max_iterations",
    "timeout_s",
    "temperature",
    "llm_retry_max_attempts",
    "llm_retry_max_wait_s",
    "llm_tool_timeout_s",
    "repo_path",
    "tickets_path",
    "overlap_propositions_min",
    "overlap_propositions_max",
    "overlap_k",
    "overlap_max_doc_freq",
    "overlap_min_should_match",
    "overlap_soak_min",
    "overlap_lease_ttl_min",
    "overlap_reenrich_debounce_min",
    "overlap_conf_threshold",
    "overlap_surface_cap",
    "overlap_drain",
    "overlap_drain_batch",
    "overlap_drain_gate_budget_ms",
    "trace_id",
    "ticket_id",
    "operation",
)


def redacted_snapshot_values(cfg: LLMConfig) -> dict[str, object]:
    """A non-secret, JSON-primitive-only projection of *cfg* (AC4).

    ``api_key`` (a bare secret string) and ``ticket_view`` (a live object) are never
    candidates — they are simply absent from :data:`_NON_SECRET_LLM_FIELDS`.
    ``headers``/``mcp_servers`` may carry resolved secret VALUES (``headers.py``'s
    ``${env:...}``/``${run:...}`` substitution grammar; MCP server auth), so only
    their KEY NAMES are exposed; ``langfuse`` exposes only its derived ``enabled``
    bool, never the credentials."""
    values: dict[str, object] = {name: getattr(cfg, name) for name in _NON_SECRET_LLM_FIELDS}
    values["header_names"] = sorted(cfg.headers)
    values["mcp_server_names"] = sorted(cfg.mcp_servers)
    values["langfuse_enabled"] = cfg.langfuse.enabled
    return values


def llm_config_fingerprint(cfg: LLMConfig, *, repo_root: str) -> str:
    """The stable content-hash fingerprint of :func:`redacted_snapshot_values`.

    Reuses :class:`rebar._operation_config.OperationSnapshot`'s validating
    constructor (:meth:`~rebar._operation_config.OperationSnapshot.build` rejects
    any leaf that is not a JSON primitive) rather than a second serializer."""
    from rebar._operation_config import ENVELOPE_VERSION, OperationSnapshot

    snapshot = OperationSnapshot.build(
        envelope_version=ENVELOPE_VERSION,
        repo_root=repo_root,
        values={"llm": redacted_snapshot_values(cfg)},
        sources={"llm": {}},
    )
    return snapshot.fingerprint()


def _log_llm_config_fingerprint(cfg: LLMConfig) -> None:
    """DEBUG-only diagnostic: the successor to the deleted ``emit_shadow_snapshot``'s
    diagnostic for this surface, folded into the authoritative composer instead of a
    separate shadow call. Guarded: any failure is caught and logged REDACTED
    (exception type name only); it never affects the bound config."""
    try:
        fingerprint = llm_config_fingerprint(cfg, repo_root=cfg.repo_path or "")
        logger.debug("llm operation config composed: fingerprint=%s…", fingerprint[:12])
    except Exception as exc:  # noqa: BLE001 — diagnostic must never break the operation
        logger.warning("llm operation config fingerprint skipped: %s", type(exc).__name__)


@contextlib.contextmanager
def compose_and_bind_llm_config(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    explicit: LLMConfig | None = None,
) -> Iterator[LLMConfig]:
    """Compose the ONE :class:`LLMConfig` for a public LLM operation and bind it
    active for the block (see module docstring).

    ``explicit`` is the caller-supplied ``config=`` a public op already accepts:
    when given, it is bound (and returned) UNCONDITIONALLY — always authoritative
    (AC3), and bound (not merely returned) so downstream subcalls observe the SAME
    instance rather than each independently defaulting.

    Reentrant: an already-bound config (an outer public op, or an outer
    :func:`~rebar.llm.config.gate_config` scope) is reused verbatim, never
    recomposed."""
    if explicit is not None:
        with gate_config(explicit):
            yield explicit
        return
    active = _active_gate_config.get()
    if active is not None:
        yield active
        return
    cfg = LLMConfig.from_env(repo_root=repo_root)
    _log_llm_config_fingerprint(cfg)
    with gate_config(cfg):
        yield cfg
