"""Per-pass, in-memory scoped finite-pass fuse over normalized coordinator outcomes.

This is a *pure-decision* leaf for the reconciler mutate path (RP-03 S3 T2). It folds
each coordinator ``TicketOutcome`` (read duck-typed: only ``.identity`` /
``.disposition`` / ``.failure_scope``) into per-scope consecutive counters keyed off the
``(provider, endpoint)`` binding resolved from an injected ``locate(identity)``.

An endpoint opens after :data:`ENDPOINT_THRESHOLD` consecutive fuse-eligible outcomes
spanning at least :data:`MIN_DISTINCT_TICKETS` distinct tickets; a provider additionally
requires the failures to span at least :data:`PROVIDER_MIN_ENDPOINTS` distinct endpoints.
A matching success fully resets that scope — clearing its consecutive run AND re-closing
it if it had opened, so a proven-healthy scope stops deferring. An outcome whose
``(provider, endpoint)`` cannot be resolved from ``locate`` participates in no scope (it
is never folded into a shared phantom scope). Independent scopes keep their own state. The
DECISION derivation reads no clock and does no I/O — ``retry_not_before`` is computed
purely from the injected ``now_ms`` / ``cooldown_ms``.

Cross-sibling value types (``Disposition`` / ``FailureScope`` / the failure policy) are
loaded by file path via the package's shared ``lazy_load`` idiom (``_loader.py``), which
resolves both under the real package and when this module is exec'd standalone in tests.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load

_outcome_mod = lazy_load("rebar_reconciler.operation_outcome", "operation_outcome.py")
Disposition = _outcome_mod.Disposition
FailureScope = _outcome_mod.FailureScope

_policy = lazy_load("rebar_reconciler.failure_policy", "failure_policy.py")


FUSE_COOLDOWN_MS = 60000
ENDPOINT_THRESHOLD = 3
MIN_DISTINCT_TICKETS = 2
PROVIDER_MIN_ENDPOINTS = 2


def _rfc3339_utc(epoch_ms: int) -> str:
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class FuseDecision:
    scope: str
    reason: str
    retry_not_before: str
    provider: str | None
    endpoint: str | None


class _ScopeState:
    __slots__ = ("consecutive", "endpoints", "opened", "tickets")

    def __init__(self) -> None:
        self.consecutive = 0
        self.tickets: set = set()
        self.endpoints: set = set()
        self.opened: FuseDecision | None = None

    def reset(self) -> None:
        self.consecutive = 0
        self.tickets = set()
        self.endpoints = set()
        self.opened = None


class PassFuse:
    def __init__(self, *, locate, now_ms: int = 0, cooldown_ms: int = FUSE_COOLDOWN_MS):
        self._locate = locate
        self._now_ms = now_ms
        self._cooldown_ms = cooldown_ms
        self._endpoints: dict = {}
        self._providers: dict = {}

    def _resolve(self, identity) -> tuple:
        binding: Mapping = self._locate(identity) or {}
        return binding.get("provider"), binding.get("endpoint")

    def _endpoint_state(self, provider, endpoint) -> _ScopeState:
        return self._endpoints.setdefault((provider, endpoint), _ScopeState())

    def _provider_state(self, provider) -> _ScopeState:
        return self._providers.setdefault(provider, _ScopeState())

    def _retry_not_before(self) -> str:
        return _rfc3339_utc(self._now_ms + self._cooldown_ms)

    def record(self, outcome) -> None:
        disposition = outcome.disposition
        provider, endpoint = self._resolve(outcome.identity)
        if _policy.is_success(disposition):
            self._reset_scopes(provider, endpoint)
            return
        if not _policy.is_fuse_eligible(disposition):
            return
        self._bump(outcome.identity, provider, endpoint)

    def _reset_scopes(self, provider, endpoint) -> None:
        if endpoint is not None:
            self._endpoint_state(provider, endpoint).reset()
        if provider is not None:
            self._provider_state(provider).reset()

    def _bump(self, identity, provider, endpoint) -> None:
        if endpoint is not None:
            ep = self._endpoint_state(provider, endpoint)
            ep.consecutive += 1
            ep.tickets.add(identity)
            self._maybe_open_endpoint(ep, provider, endpoint)
        if provider is not None:
            pv = self._provider_state(provider)
            pv.consecutive += 1
            pv.tickets.add(identity)
            if endpoint is not None:
                pv.endpoints.add(endpoint)
            self._maybe_open_provider(pv, provider)

    def _maybe_open_endpoint(self, state, provider, endpoint) -> None:
        if state.opened is not None:
            return
        if state.consecutive >= ENDPOINT_THRESHOLD and len(state.tickets) >= MIN_DISTINCT_TICKETS:
            state.opened = FuseDecision(
                scope=FailureScope.endpoint.value,
                reason="endpoint_fuse_open",
                retry_not_before=self._retry_not_before(),
                provider=provider,
                endpoint=endpoint,
            )

    def _maybe_open_provider(self, state, provider) -> None:
        if state.opened is not None:
            return
        if (
            state.consecutive >= ENDPOINT_THRESHOLD
            and len(state.tickets) >= MIN_DISTINCT_TICKETS
            and len(state.endpoints) >= PROVIDER_MIN_ENDPOINTS
        ):
            state.opened = FuseDecision(
                scope=FailureScope.provider.value,
                reason="provider_fuse_open",
                retry_not_before=self._retry_not_before(),
                provider=provider,
                endpoint=None,
            )

    def decision_for(self, identity) -> FuseDecision | None:
        provider, endpoint = self._resolve(identity)
        provider_state = self._providers.get(provider)
        if provider_state is not None and provider_state.opened is not None:
            return provider_state.opened
        endpoint_state = self._endpoints.get((provider, endpoint))
        if endpoint_state is not None and endpoint_state.opened is not None:
            return endpoint_state.opened
        return None
