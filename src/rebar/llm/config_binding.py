"""Bind one authoritative ``LLMConfig`` for each public LLM operation.

Composition occurs once, and nested calls or workflow steps reuse the bound instance. An
explicit config remains authoritative. Unlike the general operation snapshot, composition
errors propagate before an external model call instead of falling back to ambient provider or
credential selection.

The redacted fingerprint projection uses an explicit allowlist of non-secret fields and the
validated ``OperationSnapshot`` constructor. New config fields remain excluded until reviewed.
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
