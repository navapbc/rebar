"""Resolve a code-reading gate's read-root from a client ``(ref, source)`` pair (S3).

Every code-reading gate (`review_plan`, `verify_completion`,
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
from collections.abc import Iterator
from dataclasses import replace
from time import monotonic_ns

from rebar._config_resolvers import snapshot_repo_root
from rebar._snapshot import (
    SOURCE_ATTESTED,
    SOURCE_LOCAL,
    SnapshotHandle,
    acquire,
    gc_trigger,
)
from rebar._snapshot.repo_snapshot import DEFAULT_REF, materialize_tickets
from rebar.llm import build_drift
from rebar.llm.config import LLMConfig
from rebar.llm.gate_context import (
    gate_session,
    use_code_root,
    use_ticket_view,
    use_tickets_root,
)

__all__ = [
    "PROVENANCE_KEYS",
    "SOURCE_ATTESTED",
    "SOURCE_LOCAL",
    "annotate_result",
    "apply_handle",
    "attach_materialized_tickets",
    "copy_provenance",
    "default_ref",
    "default_source",
    "gate_read_root",
    "resolve_gate_handle",
]

#: The provenance keys :func:`annotate_result` stamps on a gate payload — the single
#: source of truth shared with :func:`copy_provenance`.
PROVENANCE_KEYS = ("source", "verified_at_sha", "signable")

# What an UNPINNED payload gets: one that never resolved a handle at all (an inert or
# preflight-degraded verdict). Honest and fail-closed — no source, no SHA, never signable.
_UNPINNED = {"source": None, "verified_at_sha": None, "signable": False}


def _record_phase(metrics: dict[str, int], key: str, started_ns: int) -> None:
    metrics[key] = metrics.get(key, 0) + (monotonic_ns() - started_ns) // 1_000_000


def default_ref(repo_root: str | None = None) -> str:
    """The default ``ref`` (``REBAR_GATE_REF`` > ``[snapshot].ref`` > ``origin/main``)."""
    from rebar import config as _root_config

    return _root_config.resolve_gate_ref(DEFAULT_REF, repo_root)


def default_source(repo_root: str | None = None) -> str:
    """The default ``source`` mode (``REBAR_GATE_SOURCE`` > ``[snapshot].source`` >
    ``attested``); an invalid configured value falls back to ``attested``."""
    from rebar import config as _root_config

    val = _root_config.resolve_gate_source(SOURCE_ATTESTED, repo_root)
    return val if val in (SOURCE_ATTESTED, SOURCE_LOCAL) else SOURCE_ATTESTED


def current_head_sha(auth_manifest: list[str] | None, repo_root: str | None = None) -> str:
    """The sha the signed ``verified_at_sha`` must be compared against for the unscoped
    whole-HEAD plan-review freshness check (bug 1137). For an ATTESTED manifest this is the
    CURRENT gate-ref sha read from the LOCAL object DB (NO fetch) -- NOT ``git rev-parse HEAD`` of
    whatever working tree the evaluator sits in (a feature worktree or a foreign enclosing repo
    would read a stranger sha and report a spurious ``stale-head``). For a LEGACY manifest (no
    ``verified_at_sha``) or ``source=local`` it is the working-tree HEAD. Raises ``SnapshotError``
    when an attested gate ref cannot be resolved locally -- callers choose fail-open vs
    fail-closed. This is the SINGLE source of the unscoped current-head anchor so
    ``attest.compute_validity`` and ``drift_floor`` cannot drift apart (both consumers of the
    ticket's ``code_drifted`` axis read the same value)."""
    from rebar import config as _config
    from rebar import signing as _signing
    from rebar._snapshot.repo_snapshot import resolve_ref

    if not _signing.verified_at_sha_from_manifest(auth_manifest):
        return _signing.head_sha(_config.repo_root(repo_root))
    working = str(_config.repo_root(repo_root))
    if default_source(working) != SOURCE_ATTESTED:
        return _signing.head_sha(working)
    return resolve_ref(default_ref(working), working, fetch=False)


def resolve_gate_handle(
    ref: str | None,
    source: str | None,
    repo_root: str | None,
    *,
    fetch: bool = True,
    phase_metrics: dict[str, int] | None = None,
    materialize_ticket_store: bool = True,
) -> SnapshotHandle:
    """Resolve ``(ref, source)`` (applying the configured defaults for ``None``) to a
    :class:`SnapshotHandle`. Attested materializes/serves the pinned snapshot; local hands
    back the in-place checkout. Fail-closed errors (bad ref / missing credentials) propagate
    so an attested gate never silently reads the wrong tree."""
    phase_started_ns = monotonic_ns() if phase_metrics is not None else 0
    resolved_ref = ref or default_ref(repo_root)
    resolved_source = source or default_source(repo_root)
    # The THIRD configured default this function owes, alongside ref and source. Without it a
    # caller that passes no repo_root reaches the snapshot layer's bare-cwd fallback, and on
    # the deployed MCP server the cwd is /app -- a source copy whose .git is excluded by
    # .dockerignore -- while REBAR_ROOT names a healthy checkout. Every attested tool
    # (review_plan, scan_spec, verify_completion, review_code) calls through without one, so
    # all of them failed with `cannot resolve ref 'origin/main' to a commit in '.'`, including
    # for a full SHA the configured checkout demonstrably contained. Ticket 1eb6.
    repo_root = snapshot_repo_root(repo_root)
    if phase_metrics is not None:
        _record_phase(phase_metrics, "verifier_handle_defaults_ms", phase_started_ns)
        phase_started_ns = monotonic_ns()
    handle = acquire(resolved_ref, source_mode=resolved_source, repo_root=repo_root, fetch=fetch)
    if phase_metrics is not None:
        _record_phase(phase_metrics, "verifier_code_snapshot_ms", phase_started_ns)
        phase_started_ns = monotonic_ns()
    # A gate reads material at `handle.sha` with the code of whatever build is running. When
    # that build PREDATES the pinned sha it can silently mis-handle newer material (the
    # motivating incident: a renamed config key read as an unknown one). Advisory only — the
    # call cannot raise and cannot alter the handle, the verdict, or the provenance stamp.
    build_drift.warn_if_behind(handle.sha, repo_root)
    if phase_metrics is not None:
        _record_phase(phase_metrics, "verifier_build_drift_ms", phase_started_ns)
        phase_metrics.setdefault("verifier_ticket_snapshot_ms", 0)
        phase_metrics.setdefault("verifier_snapshot_gc_ms", 0)
    if handle.source == SOURCE_ATTESTED and materialize_ticket_store:
        handle = attach_materialized_tickets(
            handle, repo_root=repo_root, fetch=fetch, phase_metrics=phase_metrics
        )
    return handle


def attach_materialized_tickets(
    handle: SnapshotHandle,
    *,
    repo_root: str | None,
    fetch: bool,
    phase_metrics: dict[str, int] | None = None,
) -> SnapshotHandle:
    """Attach the legacy full ticket snapshot to an already-resolved code handle."""
    if handle.source == SOURCE_ATTESTED and handle.tickets_path is None:
        # The ticket store lives on the orphan `tickets` branch, so it is ABSENT from the
        # code snapshot — materialize a separate pinned copy and attach it so the agent's
        # rebar ticket tools read it (instead of erroring on the missing `.tickets-tracker`
        # in the code snapshot). Fail-closed errors propagate, like the code snapshot.
        phase_started_ns = monotonic_ns() if phase_metrics is not None else 0
        tickets_root = materialize_tickets(repo_root=repo_root, fetch=fetch)
        handle = dataclasses.replace(handle, tickets_path=tickets_root)
        if phase_metrics is not None:
            _record_phase(phase_metrics, "verifier_ticket_snapshot_ms", phase_started_ns)
        # The attested resolution above is what POPULATES the snapshot store, so its tail is
        # where the operation-linked GC trigger lives (bug undamaged-epidermic-kakarikis):
        # one stamp `stat`, no ticket-store lock, the pass itself in a detached child.
        # Never raises — housekeeping must not fail the gate that triggered it.
        phase_started_ns = monotonic_ns() if phase_metrics is not None else 0
        gc_trigger.maybe_gc(repo_root)
        if phase_metrics is not None:
            _record_phase(phase_metrics, "verifier_snapshot_gc_ms", phase_started_ns)
    return handle


@contextlib.contextmanager
def gate_read_root(
    handle: SnapshotHandle,
    *,
    phase_metrics: dict[str, int] | None = None,
    ticket_view=None,
) -> Iterator[None]:
    """Run the block inside the gate's snapshot session (the safeguard marker, set for BOTH
    modes) and, in attested mode, activate the snapshot as the context-local code root.
    Local mode leaves the code root unset → configs read the in-place checkout, but the run
    is still marked as a deliberate gate session so :func:`config.assert_gated` passes."""
    if phase_metrics is None:
        with gate_session():
            if handle.source == SOURCE_ATTESTED:
                # Activate BOTH the code root (file tools) and the pinned ticket-store root
                # (rebar ticket tools), so every config rebuilt deep in the gate reads each.
                with (
                    use_code_root(str(handle.path)),
                    use_tickets_root(handle.tickets_path),
                    use_ticket_view(ticket_view),
                ):
                    yield
            else:
                yield
        return

    phase_started_ns = monotonic_ns()
    exit_started_ns = 0
    try:
        with gate_session():
            if handle.source == SOURCE_ATTESTED:
                with (
                    use_code_root(str(handle.path)),
                    use_tickets_root(handle.tickets_path),
                    use_ticket_view(ticket_view),
                ):
                    _record_phase(phase_metrics, "verifier_snapshot_enter_ms", phase_started_ns)
                    try:
                        yield
                    finally:
                        exit_started_ns = monotonic_ns()
            else:
                _record_phase(phase_metrics, "verifier_snapshot_enter_ms", phase_started_ns)
                try:
                    yield
                finally:
                    exit_started_ns = monotonic_ns()
    finally:
        if exit_started_ns:
            _record_phase(phase_metrics, "verifier_snapshot_exit_ms", exit_started_ns)


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


def copy_provenance(src: dict | None, dst: dict) -> dict:
    """Propagate the :func:`annotate_result` stamp from one gate payload onto another.

    Used where the handle is resolved DEEPER than the surface that returns the result — the
    code-review gate resolves it inside the four-pass run, so the ``review_code`` shim must
    carry the stamp of the handle the review ACTUALLY ran under rather than re-resolving
    (which could pin a different SHA if the base ref moved in between). A ``src`` that never
    pinned a source (an inert or preflight-degraded verdict) yields the UNPINNED stamp, so
    the keys are always present and such a result is never ``signable``."""
    for key in PROVENANCE_KEYS:
        value = src.get(key, _UNPINNED[key]) if isinstance(src, dict) else _UNPINNED[key]
        dst[key] = value
    return dst
