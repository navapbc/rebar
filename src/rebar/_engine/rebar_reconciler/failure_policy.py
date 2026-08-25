"""Provider-neutral normalization policy for the non-create coordinator.

This is a *pure* policy leaf for the reconciler mutate path (RP-03 S3 T1). It
carries no I/O and no clock: every function here is a total, deterministic
projection over the ``operation_outcome`` value vocabulary.

It projects the 11-member :class:`Disposition` vocabulary onto the five exact
AC6 outcome buckets, classifies broad vs ticket-local :class:`FailureScope`,
maps adapter signal-statuses and S2 ``DeferReason``s onto dispositions, and
classifies a raw HTTP status code (+ action) into a provider-neutral
``(signal-status, scope)`` pair.

Cross-sibling value types (``Disposition`` / ``FailureScope``) are loaded by
file path via the package's shared ``lazy_load`` idiom (``_loader.py``), which
resolves both under the real package and when this module is exec'd standalone
in tests.
"""

from __future__ import annotations

import importlib.util
import sys
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


# The five exact AC6 outcome buckets, in canonical order.
OUTCOME_BUCKETS: tuple[str, ...] = (
    "applied",
    "recovered",
    "deferred",
    "failed",
    "skipped",
)


# Total 11 -> 5 projection. Every Disposition member MUST appear here.
_BUCKET_BY_DISPOSITION: dict = {
    Disposition.applied: "applied",
    Disposition.already_satisfied: "applied",
    Disposition.recovered: "recovered",
    Disposition.retryable_deferred: "deferred",
    Disposition.dependency_deferred: "deferred",
    Disposition.scope_deferred: "deferred",
    Disposition.safety_aborted: "deferred",
    Disposition.commit_unknown: "deferred",
    Disposition.permanent_failure: "failed",
    Disposition.exhausted_transient: "failed",
    Disposition.skipped: "skipped",
}

# Fail loudly at import time if a new Disposition member is ever added without a
# bucket — the projection must stay total.
assert set(_BUCKET_BY_DISPOSITION) == set(Disposition), (
    "bucket projection is not total over Disposition"
)


def bucket_for(disposition) -> str:
    """Project a :class:`Disposition` onto its AC6 outcome bucket."""
    return _BUCKET_BY_DISPOSITION[disposition]


SUCCESS_DISPOSITIONS: frozenset = frozenset(
    {Disposition.applied, Disposition.already_satisfied, Disposition.recovered}
)


def is_success(disposition) -> bool:
    """True for the three success dispositions (applied / already_satisfied /
    recovered)."""
    return disposition in SUCCESS_DISPOSITIONS


BROAD_SCOPES: frozenset = frozenset(
    {
        FailureScope.endpoint,
        FailureScope.tenant,
        FailureScope.provider,
        FailureScope["global"],
    }
)


def is_broad_scope(scope) -> bool:
    """True for an authoritative scope that can stop siblings (endpoint / tenant /
    provider / global). ``ticket`` and ``none`` are local, never broad."""
    return scope in BROAD_SCOPES


_STATUS_TO_DISPOSITION: dict = {
    "applied": Disposition.applied,
    "already_satisfied": Disposition.already_satisfied,
    "recovered": Disposition.recovered,
    "permanent": Disposition.permanent_failure,
    "unknown": Disposition.commit_unknown,
    "skip": Disposition.skipped,
}


def status_to_disposition(status: str):
    """Map a *terminal* adapter signal-status onto a Disposition.

    ``"transient"`` is NOT terminal — it is resolved by the retry budget in the
    coordinator and never reaches here.
    """
    try:
        return _STATUS_TO_DISPOSITION[status]
    except KeyError:  # pragma: no cover - defensive
        raise ValueError(f"non-terminal or unknown signal status: {status!r}") from None


def defer_reason_to_disposition(defer_reason):
    """Map an S2 ``DeferReason`` (enum or its str value) onto the identically-named
    Disposition."""
    return Disposition(getattr(defer_reason, "value", defer_reason))


def classify_http_error(status_code: int, action: str) -> tuple:
    """Provider-neutral HTTP classification -> ``(signal_status, scope)``.

    All outcomes are ticket-scoped (or ``none`` for success). A 404 on a delete is
    idempotent already-gone success; other 4xx are permanent; 429 and 5xx are
    transient.
    """
    if status_code < 400:
        return ("applied", FailureScope.none)
    if status_code == 404 and action == "delete":
        return ("already_satisfied", FailureScope.ticket)
    if status_code == 404:
        return ("permanent", FailureScope.ticket)
    if status_code == 429:
        return ("transient", FailureScope.ticket)
    if status_code < 500:
        return ("permanent", FailureScope.ticket)
    return ("transient", FailureScope.ticket)


def tally(outcomes) -> dict:
    """Count ticket outcomes by ``.bucket``, always returning all five buckets
    (0-filled)."""
    counts: dict = {bucket: 0 for bucket in OUTCOME_BUCKETS}
    for outcome in outcomes:
        counts[outcome.bucket] += 1
    return counts
