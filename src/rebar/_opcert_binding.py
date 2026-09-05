"""Context-local op-cert signer binding (RP-04 S6, story 6f14).

The transport that carries a startup-composed op-cert signer from the gate service's
``run_job`` into the deep signing seam (:mod:`rebar._opcert_signing`) WITHOUT mutating
process-global env (the old per-job ``REBAR_OPCERT_KEY_PATH`` / ``REBAR_OPCERT_ENV_ID`` /
``REBAR_SYNC_PUSH`` patching). A :class:`~contextvars.ContextVar` isolates concurrent /
timed-out gate workers: each binds its own signer for the dispatch and resets it in a
``finally``, so no worker can observe or mutate another's binding.

When NO signer is bound (the developer-local CLI close / review-plan callers, and every
non-service caller) the signing seam keeps its EXACT env/genesis key + principal resolution
and the push policy falls back to the env/config default — this is regression-critical, so
the unbound path is byte-for-byte unchanged.

Lives in rebar core (not under ``opcert_service``) so the signing seam can read the binding
without importing the FastAPI-adjacent service package. The binding is duck-typed to a small
:class:`OpcertBinding` protocol (``key_path`` + ``principal``); the concrete producer is
:class:`rebar.opcert_service.keyprov.OpcertSigner`.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Protocol, runtime_checkable


@runtime_checkable
class OpcertBinding(Protocol):
    """The signing material a bound gate threads context-locally: a private-key FILE path
    (the process-owned 0600 copy) and the DSSE principal to sign under. Declared as read-only
    properties so a frozen dataclass (``OpcertSigner``) satisfies the protocol."""

    @property
    def key_path(self) -> str: ...

    @property
    def principal(self) -> str | None: ...


_BOUND: ContextVar[OpcertBinding | None] = ContextVar("rebar_opcert_binding", default=None)
_PUSH_MODE: ContextVar[str | None] = ContextVar("rebar_opcert_push_mode", default=None)


def current_binding() -> OpcertBinding | None:
    """The signer bound for the current context, or ``None`` (the unbound env/genesis path)."""
    return _BOUND.get()


def current_push_mode() -> str | None:
    """The context-local push policy, or ``None`` (fall back to the env/config default)."""
    return _PUSH_MODE.get()


@contextlib.contextmanager
def bound_signer(binding: OpcertBinding | None, *, push_mode: str | None = "off") -> Iterator[None]:
    """Bind ``binding`` (and its push policy) for the enclosed block, resetting on exit.

    When ``binding`` is ``None`` the push policy is left unbound too, so nesting an unbound
    block inside a bound one does not silently disable pushes. The trusted op-cert gate service
    always passes a concrete signer with the default ``push_mode="off"`` (the gate never pushes).

    ``push_mode=None`` binds the SIGNER without overriding the push policy — the enclosed block
    signs from ``binding`` but the outbound push still falls back to the env/config default
    (:func:`rebar.config.resolve_push_mode`). The on-box MCP server uses this: it must sign its
    certified-op certs under the box environment AND still auto-push its ticket writes to the
    shared store, unlike the store-read-only gate service."""
    push = push_mode if binding is not None else None
    bound_token = _BOUND.set(binding)
    push_token = _PUSH_MODE.set(push)
    try:
        yield
    finally:
        _PUSH_MODE.reset(push_token)
        _BOUND.reset(bound_token)


def spawn_context_daemon(
    target: Callable[[], object], *, name: str | None = None
) -> threading.Thread:
    """Start ``target`` on a daemon thread running inside a COPY of the CALLER's context.

    A bare :class:`threading.Thread` starts with a FRESH, EMPTY
    :class:`~contextvars.Context`, so it does NOT inherit the ``_BOUND`` binding
    :func:`bound_signer` set in the spawning thread. That loss is bug
    ``ff4a-2832-def4-4e55``: a certified op running on such a thread resolved its PRINCIPAL
    from the process-global ``REBAR_OPCERT_ENV_ID`` (which threads DO share) but its KEY from
    the unbound ``<tracker>/.opcert-key`` genesis path — which silently AUTO-GENERATES a fresh
    Ed25519 key. The resulting op-cert claimed the pinned production environment while being
    signed under an untrusted key, so ``ssh-keygen -Y verify`` failed and the attestation read
    as ``mismatch`` / "no certified plan-review attestation". The synchronous surface never had
    this problem because ``anyio.to_thread.run_sync`` propagates the caller's context.

    This module owns the ContextVar, so it also owns carrying it across a thread boundary.
    A fresh :func:`contextvars.copy_context` is taken per call in the CALLER (a ``Context``
    cannot be entered twice, and copying inside the thread would copy the wrong one).
    """
    ctx = contextvars.copy_context()
    thread = threading.Thread(target=lambda: ctx.run(target), name=name, daemon=True)
    thread.start()
    return thread
