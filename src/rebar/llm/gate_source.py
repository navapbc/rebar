"""Resolve a code-reading gate's read-root from a client ``(ref, source)`` pair (S3).

Every code-reading gate (`review_plan`, `verify_completion`, `review_ticket`,
`review_code`, `scan_spec`) takes ONE ``ref`` (branch | tag | SHA, default ``origin/main``)
and a ``source`` mode (``attested`` default | ``local``) and reads a snapshot materialized
at the pinned SHA instead of the server's mutable checkout:

* **attested** — materialize (via the content-addressed cache) a faithful snapshot at the
  pinned SHA and re-root the gate's file tools onto it (``cfg.repo_path`` + the context-
  local code root, so even configs rebuilt deep in the workflow read the snapshot). The
  run is signable; ``verified_at_sha`` is recorded on the result.
* **local** — read the server's in-place checkout directly (no materialization, dirty
  allowed); ``repo_root`` IS the read root and the run is flagged UNSIGNED (S4 bars
  signing). This is the documented back-out to the prior in-place behavior.

Defaults resolve through the standard precedence (``REBAR_GATE_*`` env > ``[snapshot]``
config table > documented default), so a deployment can override them without code.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from collections.abc import Iterator
from dataclasses import replace

from rebar._snapshot import (
    SOURCE_ATTESTED,
    SOURCE_LOCAL,
    SnapshotHandle,
    acquire,
)
from rebar._snapshot.repo_snapshot import DEFAULT_REF, materialize_tickets
from rebar.llm.config import LLMConfig, gate_session, use_code_root, use_tickets_root

__all__ = [
    "SOURCE_ATTESTED",
    "SOURCE_LOCAL",
    "annotate_result",
    "apply_handle",
    "default_ref",
    "default_source",
    "gate_read_root",
    "resolve_gate_handle",
]


def _snapshot_table(repo_root: str | None) -> dict:
    try:
        from rebar import config as _root_config

        return _root_config.read_reserved_section("snapshot", repo_root)
    except Exception:  # noqa: BLE001 - degrade to env/defaults on any config error
        return {}


def _str_pref(env_name: str, file_key: str, default: str, repo_root: str | None) -> str:
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    fv = _snapshot_table(repo_root).get(file_key)
    if isinstance(fv, str) and fv.strip():
        return fv.strip()
    return default


def default_ref(repo_root: str | None = None) -> str:
    """The default ``ref`` (``REBAR_GATE_REF`` > ``[snapshot].ref`` > ``origin/main``)."""
    return _str_pref("REBAR_GATE_REF", "ref", DEFAULT_REF, repo_root)


def default_source(repo_root: str | None = None) -> str:
    """The default ``source`` mode (``REBAR_GATE_SOURCE`` > ``[snapshot].source`` >
    ``attested``); an invalid configured value falls back to ``attested``."""
    val = _str_pref("REBAR_GATE_SOURCE", "source", SOURCE_ATTESTED, repo_root)
    return val if val in (SOURCE_ATTESTED, SOURCE_LOCAL) else SOURCE_ATTESTED


def resolve_gate_handle(
    ref: str | None,
    source: str | None,
    repo_root: str | None,
    *,
    fetch: bool = True,
) -> SnapshotHandle:
    """Resolve ``(ref, source)`` (applying the configured defaults for ``None``) to a
    :class:`SnapshotHandle`. Attested materializes/serves the pinned snapshot; local hands
    back the in-place checkout. Fail-closed errors (bad ref / missing credentials) propagate
    so an attested gate never silently reads the wrong tree."""
    resolved_ref = ref or default_ref(repo_root)
    resolved_source = source or default_source(repo_root)
    handle = acquire(resolved_ref, source_mode=resolved_source, repo_root=repo_root, fetch=fetch)
    if handle.source == SOURCE_ATTESTED:
        # The ticket store lives on the orphan `tickets` branch, so it is ABSENT from the
        # code snapshot — materialize a separate pinned copy and attach it so the agent's
        # rebar ticket tools read it (instead of erroring on the missing `.tickets-tracker`
        # in the code snapshot). Fail-closed errors propagate, like the code snapshot.
        tickets_root = materialize_tickets(repo_root=repo_root, fetch=fetch)
        handle = dataclasses.replace(handle, tickets_path=tickets_root)
    return handle


@contextlib.contextmanager
def gate_read_root(handle: SnapshotHandle) -> Iterator[None]:
    """Run the block inside the gate's snapshot session (the safeguard marker, set for BOTH
    modes) and, in attested mode, activate the snapshot as the context-local code root.
    Local mode leaves the code root unset → configs read the in-place checkout, but the run
    is still marked as a deliberate gate session so :func:`config.assert_gated` passes."""
    with gate_session():
        if handle.source == SOURCE_ATTESTED:
            # Activate BOTH the code root (file tools) and the pinned ticket-store root
            # (rebar ticket tools), so every config rebuilt deep in the gate reads each.
            with use_code_root(str(handle.path)), use_tickets_root(handle.tickets_path):
                yield
        else:
            yield


def apply_handle(cfg: LLMConfig, handle: SnapshotHandle) -> LLMConfig:
    """Re-root an explicit config's ``repo_path`` (code) and ``tickets_path`` (the pinned
    ticket store) onto an attested snapshot (no-op for local, which already reads the
    checkout)."""
    if handle.source == SOURCE_ATTESTED:
        return replace(cfg, repo_path=str(handle.path), tickets_path=handle.tickets_path)
    return cfg


def annotate_result(result: dict, handle: SnapshotHandle) -> dict:
    """Stamp the source provenance on a gate result: the ``source`` mode, the pinned
    ``verified_at_sha`` (``None`` in local mode), and whether the run is ``signable``
    (attested + pinned SHA). S4 reads ``signable``/``verified_at_sha`` to bind the SHA."""
    if isinstance(result, dict):
        result["source"] = handle.source
        result["verified_at_sha"] = handle.sha
        result["signable"] = handle.signable
    return result
