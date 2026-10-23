"""Gate execution context for pinned code and ticket read roots.

ContextVars, scoped binders, readers, and ``assert_gated`` live here;
``LLMConfig`` consumes them through ``rebar.llm.config``. That module re-exports
these names, and consumers should keep importing there so existing monkeypatch
targets remain effective. ``config`` imports this module; a reverse import
would create a cycle. Literal environment reads in ``LLMConfig.from_env`` must
remain visible to the env-registry scanner; a field table would hide them.
See ticket b300."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from collections.abc import Iterator

from rebar import config as _root_config
from rebar._engine_support.reads import (
    current_ticket_view as current_ticket_view,
)
from rebar._engine_support.reads import (
    use_ticket_view as use_ticket_view,
)

# The active code read-root for the running gate (epic raze-vet-ditch S3). When a gate
# runs in `attested` mode it materializes a snapshot at the client-pinned SHA and sets
# this for the duration of the run; `LLMConfig.from_env` then resolves `repo_path` to the
# snapshot, so EVERY config built deep in the gate (citation resolution, reconcile, the
# agent itself) reads the pinned snapshot rather than the server's mutable checkout. A
# ContextVar is thread- and asyncio-task-safe (no global env mutation across concurrent
# gates). Unset (the default) preserves the prior in-place behavior — exactly `local` mode.
_active_code_root: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_llm_code_root", default=None
)


def current_code_root() -> str | None:
    """The active gate's code read-root (an attested snapshot dir), or ``None``."""
    return _active_code_root.get()


def resolve_code_root(
    repo_root: str | os.PathLike[str] | None = None,
    *,
    cfg_repo_path: str | None = None,
    allow_checkout_fallback: bool = True,
    require: bool = False,
) -> str | None:
    """Resolve the authoritative code read root for an LLM gate.

    Choose the first available root: explicit ``repo_root``, configured
    ``cfg_repo_path``, active pinned snapshot, then live checkout. With
    ``allow_checkout_fallback=False``, return None instead of choosing checkout;
    ``require=True`` raises ``LLMConfigError`` when no root remains. The default
    always yields a root. This keeps callers that omit a threaded root on the
    attested snapshot rather than silently reading mutable code (ADR 0006)."""
    if repo_root:
        return str(repo_root)
    if cfg_repo_path:
        return cfg_repo_path
    snapshot = current_code_root()
    if snapshot:
        return snapshot
    resolved = str(_root_config.repo_root()) if allow_checkout_fallback else None
    if resolved is None and require:
        from rebar.llm.errors import LLMConfigError

        raise LLMConfigError(
            "resolve_code_root: a code read-root is REQUIRED but none could be resolved "
            "(no explicit repo_root, no cfg.repo_path, no active attested snapshot, and "
            "allow_checkout_fallback=False). A gate stage must not run blind against a None "
            "root — thread a root, activate a snapshot, or allow the checkout fallback."
        )
    return resolved


# The active TICKET-store read-root for the running gate. The agent's rebar ticket tools
# resolve the store under `cfg.repo_path` (the code snapshot) — but the ticket store lives
# on the orphan `tickets` branch (gitignored `.tickets-tracker/`) and is ABSENT from the
# code snapshot, so a gate sets this to a separately materialized, pinned copy of the store
# (see `rebar._snapshot.materialize_tickets`) and `LLMConfig.from_env` resolves
# `tickets_path` to it. Mirrors `_active_code_root`; unset (local mode) reads the live
# checkout's store (which already has `.tickets-tracker/`).
_active_tickets_root: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rebar_llm_tickets_root", default=None
)


def current_tickets_root() -> str | None:
    """The active gate's ticket-store read-root (a pinned snapshot of the store), or ``None``."""
    return _active_tickets_root.get()


def current_code_sha() -> str | None:
    """The pinned SHA of the active attested snapshot, or ``None`` (local / no gate).

    Derived from the content-addressed snapshot layout: an attested code root is
    ``<store>/<sha>`` (``rebar._snapshot`` keys entries by full commit SHA), so the dir
    name IS the SHA. A local read root (the checkout) is not SHA-named → ``None``."""
    root = current_code_root()
    if not root:
        return None
    name = os.path.basename(root.rstrip(os.sep))
    if len(name) == 40 and all(c in "0123456789abcdef" for c in name):
        return name
    return None


# Whether we are inside a code-reading gate's snapshot session (epic raze-vet-ditch S-RETRO
# safeguard). Set by `gate_source.gate_read_root` for BOTH attested AND local runs — so it
# marks "a gate deliberately chose this read root", distinct from `current_code_root` (which
# is only set for attested). The runtime guard `assert_gated` uses it to FAIL CLOSED when a
# tool-using agent's file tools are built outside any gate session — catching a new agentic
# op (e.g. a generic run_workflow agent step) added without following the snapshot process.
_in_gate_session: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "rebar_llm_in_gate_session", default=False
)


def in_gate_session() -> bool:
    """True iff execution is inside a code-reading gate's snapshot session."""
    return _in_gate_session.get()


@contextlib.contextmanager
def gate_session() -> Iterator[None]:
    """Mark the block as running inside a gate's snapshot session (attested OR local)."""
    token = _in_gate_session.set(True)
    try:
        yield
    finally:
        _in_gate_session.reset(token)


def assert_gated(context: str = "agentic file access") -> None:
    """Fail closed when a tool-using agent reads files OUTSIDE the snapshot gate process.

    The safeguard (epic raze-vet-ditch) against a NEW agentic operation being added without
    routing through ``rebar.llm.gate_source`` (which pins an attested snapshot or an explicit
    local read). Any agent that wires read-only file tools MUST run inside ``gate_read_root``;
    otherwise it would silently read the server's mutable checkout — the exact class of bug
    this epic exists to prevent. ``REBAR_GATE_ALLOW_UNGATED=1`` is a logged escape hatch for a
    deliberate, audited exception."""
    if _in_gate_session.get():
        return
    allow = os.environ.get("REBAR_GATE_ALLOW_UNGATED", "")  # read-via: subsystem-kill-switch
    if allow.strip().lower() in ("1", "true", "yes"):
        # `__name__`, not the former hardcoded "rebar.llm.config" (ticket b300): the warning
        # calls the override "audited", so attributing it to a module that no longer holds
        # this code sends whoever greps the logs to the wrong file.
        logging.getLogger(__name__).warning(
            "%s ran OUTSIDE a snapshot gate session (REBAR_GATE_ALLOW_UNGATED override)", context
        )
        return
    raise RuntimeError(
        f"{context} was attempted OUTSIDE the repo-snapshot gate process (epic "
        "raze-vet-ditch): a tool-using agent must run inside rebar.llm.gate_source."
        "gate_read_root (attested snapshot or explicit local), never against the server's "
        "mutable checkout. Route the operation through gate_source, or set "
        "REBAR_GATE_ALLOW_UNGATED=1 to override (audited)."
    )


@contextlib.contextmanager
def use_code_root(path: str | None) -> Iterator[None]:
    """Bind the gate's code read-root for the duration of the block (``None`` = no override,
    i.e. read the in-place checkout — local mode).

    Caveat: a ``ContextVar`` is inherited by asyncio tasks but NOT by raw threads — code that
    rebuilds an :class:`~rebar.llm.config.LLMConfig` on a worker thread (e.g. a future ``map``
    workflow step's fan-out) must propagate context via ``contextvars.copy_context().run`` or
    it will fall through to the checkout. The current gate workflows rebuild config only on the
    calling thread, so the snapshot is honored everywhere they read it."""
    token = _active_code_root.set(path)
    try:
        yield
    finally:
        _active_code_root.reset(token)


@contextlib.contextmanager
def use_tickets_root(path: str | None) -> Iterator[None]:
    """Bind the gate's ticket-store read-root for the duration of the block (``None`` = no
    override, i.e. read the in-place checkout's store — local mode). Mirrors
    :func:`use_code_root`; the same raw-thread ContextVar caveat applies."""
    token = _active_tickets_root.set(path)
    try:
        yield
    finally:
        _active_tickets_root.reset(token)
