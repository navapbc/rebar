"""Unified reconciler exception taxonomy (epic 5ca8 / romp-swath-wince).

Before this story, ``acli``/``acli_subprocess`` and ``applier``/``batch_dispatch`` each defined
their OWN ``RetryExhaustedError`` with a DIFFERENT base class, so ``except RetryExhaustedError``
against one import silently MISSED the one the other retry loop raised (exception identity drives
control flow — a latent reliability bug). These tests pin the unification:

* ONE canonical type in ``_errors`` (``is``-identity across both public surfaces);
* caught by the acli path's ``except RuntimeError`` AND the batch path's ``except Exception`` AND
  ``except ReconcilerError``;
* both retry loops CHAIN the cause (``__cause__``) and populate ``last_exception``/``attempts``.

No grep/glob/substring heuristics — pure object-identity / isinstance / ``__cause__`` assertions.
"""

from __future__ import annotations

import urllib.error

import pytest

from rebar_reconciler._errors import JiraAPIError, ReconcilerError, RetryExhaustedError


def test_retry_exhausted_is_one_object_across_both_surfaces() -> None:
    """AC1: the acli and applier re-exports are the SAME object (the formerly-divergent bodies)."""
    from rebar_reconciler.acli import RetryExhaustedError as FromAcli
    from rebar_reconciler.applier import RetryExhaustedError as FromApplier

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
    from rebar_reconciler.applier import JiraAPIError as FromApplier
    from rebar_reconciler.batch_dispatch import JiraAPIError as FromBatch

    assert FromApplier is FromBatch is JiraAPIError
    err = JiraAPIError("nope", 404)
    assert err.status_code == 404
    assert isinstance(err, ReconcilerError)


def test_batch_retry_loop_chains_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 (batch path): ``_call_with_retry`` chains the underlying cause (the prior
    ``raise RetryExhaustedError(str(last_exc))`` DROPPED ``__cause__``) and records
    ``last_exception``/``attempts``."""
    from rebar_reconciler import batch_dispatch

    monkeypatch.setattr(batch_dispatch.time, "sleep", lambda *_a, **_k: None)
    boom = JiraAPIError("server error", 500)  # 5xx is retryable → the loop exhausts

    def always_500(*_a, **_k):
        raise boom

    with pytest.raises(RetryExhaustedError) as ei:
        batch_dispatch._call_with_retry(always_500, max_retries=2)
    assert ei.value.__cause__ is boom  # chained (previously dropped)
    assert ei.value.last_exception is boom
    assert ei.value.attempts == 3  # max_retries + 1


def test_acli_retry_loop_chains_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 (acli path): ``_call_with_backoff`` keeps chaining the cause AND now records
    ``last_exception``/``attempts`` on the unified type."""
    from rebar_reconciler import acli_subprocess

    monkeypatch.setattr(acli_subprocess.time, "sleep", lambda *_a, **_k: None)
    boom = urllib.error.HTTPError("http://x", 503, "Service Unavailable", None, None)  # retryable

    def always_503(*_a, **_k):
        raise boom

    with pytest.raises(RetryExhaustedError) as ei:
        acli_subprocess._call_with_backoff(always_503, max_retries=2)
    assert ei.value.__cause__ is boom
    assert ei.value.last_exception is boom
    assert ei.value.attempts == 3  # max_retries + 1
