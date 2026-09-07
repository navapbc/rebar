"""Provide Jira Data Center retry, TLS, and error translation.

``TlsVerificationError`` remains with its factory to avoid circular imports.
``transport`` re-exports the public retry names.
"""

from __future__ import annotations

import random
import sys
import time
from email.message import Message
from typing import Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler._errors import MAX_BACKOFF_S, parse_retry_after

# Rate-limit retries honor ``Retry-After`` and add up to 20 percent jitter. The
# harness does not exercise a Data Center limiter. Jitter separates clients that
# share one rate-limit bucket.
_RETRY_AFTER_JITTER = 0.20


def _connection_retry_exceptions() -> tuple[type[BaseException], ...]:
    """The exception types worth retrying: ``requests``' ``ConnectionError`` /
    ``Timeout`` (the underlying transport ``pycontribs`` itself raises for a
    transient connectivity fault). ``requests`` ships as a transitive dependency
    of the ``[jira-datacenter]`` extra, so it is present whenever a REAL client
    is in play; a transport built with a fake client (the unit tests — no
    extra installed) never raises these, so an empty tuple here is harmless:
    ``except ()`` matches nothing and every call just runs straight through.
    """
    # Builtin TimeoutError is ALWAYS retryable, independent of requests: since
    # Python 3.10 ``socket.timeout`` is an alias of it, so a read-timeout from the
    # ssl/socket layer can surface as this rather than as a requests exception.
    # ``acli_rest._rest_urlopen_with_retry`` — the policy this module mirrors —
    # retries it explicitly for exactly that reason ("read-timeout from ssl/socket
    # layer"); omitting it here would leave the DC path failing a transient fault
    # the Cloud path already survives.
    try:
        import requests.exceptions as _req_exc
    except ImportError:
        return (TimeoutError,)
    return (_req_exc.ConnectionError, _req_exc.Timeout, TimeoutError)


def _jira_http_error_types() -> tuple[type[BaseException], ...]:
    """The library error type that means "the server answered with a 4xx/5xx":
    ``jira.exceptions.JIRAError``.

    Returned as a tuple (and imported lazily, mirroring
    :func:`_connection_retry_exceptions`) so a transport built with a FAKE client —
    the unit tests, with no ``[jira-datacenter]`` extra installed — still works:
    ``except ()`` matches nothing, and a fake's own error propagates untouched.
    """
    try:
        from jira.exceptions import JIRAError
    except ImportError:
        return ()
    return (JIRAError,)


def _as_backend_http_error(exc: BaseException) -> BackendHTTPError:
    """Translate a library HTTP error into the port's ``BackendHTTPError``.

    THE adapter-boundary translation this transport owes the core: ``JIRAError``
    carries the status as ``.status_code``, which becomes ``BackendHTTPError.code``
    (urllib's spelling) so the core's existing ``except urllib.error.HTTPError``
    clauses classify a DC failure exactly as they classify a Cloud one — e.g. a 404
    read reaching ``outbound_differ._safe_get_issue`` is seen as ``_DELETED``. A
    library error with no usable status degrades to ``0``, which no core branch
    mistakes for a 404/success.
    """
    status = getattr(exc, "status_code", None)
    return BackendHTTPError(
        getattr(exc, "url", None) or "",
        int(status) if isinstance(status, int) else 0,
        str(exc),
        Message(),
        None,
    )


class TlsVerificationError(ConnectionError):
    """A TLS certificate verification failure reaching the DC instance.

    Distinct from the transient connectivity faults :func:`_with_connection_retry`
    retries, because ``requests.exceptions.SSLError`` SUBCLASSES
    ``requests.exceptions.ConnectionError`` — so without this it is swallowed by the
    retry set and re-attempted three times with backoff. A certificate does not
    become valid on retry: that is seven wasted seconds and a guaranteed failure,
    ending in an opaque SSL error that never mentions the setting which fixes it.
    """


def _tls_verification_error(exc: BaseException) -> Exception | None:
    """Return an actionable :class:`TlsVerificationError` for a cert failure, else None."""

    try:
        from requests.exceptions import SSLError
    except ImportError:  # no extra installed → no requests → nothing to classify
        return None
    if not isinstance(exc, SSLError):
        return None
    return TlsVerificationError(
        f"TLS certificate verification failed for the Data Center instance: {exc}. "
        "This is NOT retried — a certificate does not become valid on a retry. If this "
        "deployment presents a certificate from an internal CA, set reconciler.ca_bundle "
        "to that CA bundle's PATH; certificate verification is never disabled, and "
        "reconciler.allow_insecure does not affect it (it governs the URL scheme only)."
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Read a usable ``Retry-After`` delay from ``exc.response.headers``.

    Return ``None`` when the response or header is absent or invalid.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    return parse_retry_after(getter("Retry-After"))


def _rate_limit_delay(retry_after: float) -> float:
    """``Retry-After`` plus up to 20% jitter, CLAMPED to ``MAX_BACKOFF_S``.

    Jitter is added BEFORE the clamp so the ceiling is a real ceiling: jittering after clamping
    would let the delay exceed ``MAX_BACKOFF_S`` by up to 20%, which is the sort of off-by-a-bit
    that only shows up under the load the limiter exists for.
    """
    return min(MAX_BACKOFF_S, retry_after * (1.0 + random.random() * _RETRY_AFTER_JITTER))


def _with_connection_retry(
    fn: Any,
    *,
    rate_limit_retry: bool = False,
    attempts: int = 3,
    backoffs: tuple[int, ...] = (2, 5),
) -> Any:
    """Run a transport call through the shared retry and translation boundary.

    Connection faults receive at most two retries with bounded backoff. HTTP
    failures are not retried, except an opted-in 429 with a usable
    ``Retry-After`` header. Default opt-out protects mutations from duplicate
    effects. Unretriable vendor HTTP errors become ``BackendHTTPError``.
    """
    retryable = _connection_retry_exceptions()
    http_errors = _jira_http_error_types()
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except http_errors as exc:
            # 429 is the ONE HTTP status this function may retry, and only for a call that
            # explicitly opted in. Everything else — every other 4xx/5xx, and every 429 on a
            # non-opted-in call — still fails on the FIRST attempt, translated at this boundary.
            if (
                rate_limit_retry
                and getattr(exc, "status_code", None) == 429
                and attempt < attempts - 1
                and (retry_after := _retry_after_seconds(exc)) is not None
            ):
                delay = _rate_limit_delay(retry_after)
                print(
                    f"[jira-dc-retry] HTTP 429 rate limited; server asked for "
                    f"{retry_after}s, sleeping {delay:.2f}s (attempt {attempt + 1}) …",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise _as_backend_http_error(exc) from exc
        except retryable as exc:
            # Checked BEFORE the retry bookkeeping: SSLError is a ConnectionError
            # subclass, so it lands in `retryable` and would otherwise be re-attempted.
            tls_error = _tls_verification_error(exc)
            if tls_error is not None:
                raise tls_error from exc
            last_exc = exc
        if attempt < attempts - 1:
            delay = backoffs[attempt]
            print(
                f"[jira-dc-retry] attempt {attempt + 1} failed ({last_exc!r}); "
                f"retrying in {delay}s …",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
