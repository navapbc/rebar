"""Gate read-root + snapshot-session context for the LLM agent-operations framework.

The three ContextVars a running gate binds — the attested code read-root, the pinned
ticket-store read-root, and the "am I inside a gate session" flag — plus their readers,
their ``with``-scoped binders, and the ``assert_gated`` fail-closed guard. This is the
gate EXECUTION context, a different concern from ``LLMConfig`` (which merely *consumes*
it: ``from_env`` calls :func:`current_code_root` / :func:`current_tickets_root` to resolve
``repo_path`` / ``tickets_path`` (``llm/config.py``)). Its natural consumer seam already
existed at ``llm/gate_source.py:35``, which imports the binders and nothing else.

WHY THIS FILE EXISTS — and what it does NOT fix (ticket b300). ``llm/config.py`` sat at
793 LOC against the absolute 800-line cap in ``.github/module-size-limit.txt`` and is the
most-imported near-cap module in the repo (fan-in 93), which made ticket d23e's
``REBAR_LLM_MODEL`` deprecation alias (measured at 10-16 lines) unlandable against the 7
lines available (that alias has since been removed, but the headroom problem
stands). This extraction RELOCATES INERT MASS: the code below has not grown since
2026-07-09, so moving it buys headroom without touching the actual absorber. That absorber
is ``LLMConfig.from_env``, which grows ~10-13 lines per new knob (a field, a resolution
line, a docstring row) and is untouched here — so this is roughly 15 knobs of runway, NOT
permanence. ``config.py`` will approach the cap again.

The durable follow-ups, either of which removes the growth term rather than deferring it:

  (a) teach ``scripts/gen_env_registry.py`` to consume an exported spec list, and ONLY
      THEN table-ify ``from_env``; or
  (b) domain-split the knobs per feature cluster, so each cluster's growth lands in its
      own module.

Do NOT attempt a declarative field table before (a). ``gen_env_registry.py`` records an
env read only when the variable name is a STRING LITERAL at the read site, so collapsing
the knobs into spec rows silently drops 19 rows from ``docs/env-vars.md`` and fails the
registry drift gate. That constraint is why the relieving cut here is a different-concern
extraction rather than the field table ADR 0056 speculated about.

RE-EXPORT CONTRACT: every name below is re-exported from ``rebar.llm.config`` and NO
consumer under ``src/`` imports it from here except ``gate_source.py`` (which always did).
Thirteen ``monkeypatch.setattr`` targets in the suite name ``rebar.llm.config.<name>``, and
they keep working only because consumers resolve the name at call time out of ``config``'s
module globals (a function-level ``from rebar.llm.config import ...``, e.g.
``plan_review/attest.py:123``). Repointing a consumer here would leave those patches
applying to a module the consumer no longer reads — the tests would pass while asserting
nothing. Import from ``rebar.llm.config``, not from this module.

Dependency direction is one-way: ``config`` imports ``gate_context``; this module must
never import ``rebar.llm.config`` (that is a cycle, and an import-time failure).
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
from collections.abc import Iterator

from rebar import config as _root_config

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
    """The single authoritative code read-root resolver for the LLM gates.

    Cascade (first truthy wins):

      1. an explicit ``repo_root`` (a caller override),
      2. ``cfg_repo_path`` (a pinned snapshot already resolved onto an explicit ``LLMConfig``),
      3. the ACTIVE attested-gate snapshot (:func:`current_code_root`) — a gate pins this to
         ``[snapshot].ref`` (``origin/main`` HEAD by default), so an in-gate caller that
         threads nothing still grounds against the pinned snapshot,
      4. the live checkout root (:func:`rebar.config.repo_root`, which itself falls back to
         the cwd and so never returns ``None``) — UNLESS ``allow_checkout_fallback`` is False.

    Centralizing this is what kills the *class* of bug where a gate consumer handed
    ``repo_root=None`` (because the value was dropped on one of the threading hops) silently
    degrades — e.g. the det-floor P2 'resolution' check abstaining ``no_repo_root``, or an
    agentic verifier reading the server's mutable checkout instead of the pinned snapshot.

    The default (``allow_checkout_fallback=True``) NEVER returns ``None`` and is for the gate
    BOUNDARY (a workflow run needs a concrete root; in non-attested local mode the checkout IS
    the correct root). Lightweight context builders that must NOT force a checkout default
    (where ``None`` legitimately means "no code to ground against", and a forced checkout root
    would induce writes) pass ``allow_checkout_fallback=False`` to get snapshot-or-``None``.

    ``require=True`` makes the read-root contract ENFORCEABLE for a stage that genuinely cannot
    run blind: if the cascade would yield ``None`` (only reachable with
    ``allow_checkout_fallback=False``), it raises :class:`~rebar.llm.errors.LLMConfigError`
    (fail-closed) instead of returning ``None`` — so a stage that requires a root never silently
    degrades against one (the #71 class of bug). It is opt-in (default ``False`` preserves the
    snapshot-or-``None`` behavior) and composes with the cascade: a resolved snapshot/checkout
    satisfies it without raising. See docs/adr/0006-llm-stage-seam-contracts.md."""
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
