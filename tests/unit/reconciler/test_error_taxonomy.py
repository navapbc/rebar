"""Pin the reconciler's canonical exception identity.

Both public surfaces must expose the same ``RetryExhaustedError`` from ``_errors`` with
the required ``RuntimeError`` and ``ReconcilerError`` ancestry. ADR 0103 S3 retired the
batch path's handler-level retry loop, so its cause-chain pin went with it. The type stays
public for transports that raise it. Oracles use identity and MRO rather than source-text
heuristics.
"""

from __future__ import annotations

from rebar.reconciler._errors import JiraAPIError, ReconcilerError, RetryExhaustedError


def test_retry_exhausted_is_one_object_across_both_surfaces() -> None:
    """AC1: the acli and applier re-exports are the SAME object (the formerly-divergent bodies)."""
    from rebar.reconciler.adapters.jira.acli import RetryExhaustedError as FromAcli
    from rebar.reconciler.applier import RetryExhaustedError as FromApplier

    assert FromAcli is FromApplier is RetryExhaustedError


def test_retry_exhausted_caught_by_runtime_exception_and_base() -> None:
    """AC2: the unified MRO is caught by BOTH existing call-site catches and the new base; the
    message-first positional constructor (both legacy call sites) still works."""
    err = RetryExhaustedError("x")
    assert isinstance(err, RuntimeError)  # the acli path's `except RuntimeError`
    assert isinstance(err, Exception)  # the batch path's `except Exception`
    assert isinstance(err, ReconcilerError)  # the new catch-all seam
    assert str(RetryExhaustedError("just a message")) == "just a message"


def test_jira_api_error_single_body_across_surfaces() -> None:
    """AC4: one ``JiraAPIError`` body, ``__init__(message, status_code)`` unchanged, both surfaces
    import the SAME object; it is a ``ReconcilerError``."""
    from rebar.reconciler.applier import JiraAPIError as FromApplier
    from rebar.reconciler.batch_dispatch import JiraAPIError as FromBatch

    assert FromApplier is FromBatch is JiraAPIError
    err = JiraAPIError("nope", 404)
    assert err.status_code == 404
    assert isinstance(err, ReconcilerError)
