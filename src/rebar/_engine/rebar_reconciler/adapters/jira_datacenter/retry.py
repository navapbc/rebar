"""Retry policy + error translation for the Jira Data Center transport (story
S1 [rebar:f2f3-9cb1-335b-4e31], epic e369).

RELOCATED VERBATIM out of ``transport.py``; this module changes no behaviour.
The move is forced by the LOCKED 800-line module-size cap
(``.github/module-size-limit.txt``): ``transport.py`` stood at 789 lines, leaving
eleven lines of headroom, and the 429 rate-limit policy that story S2
[rebar:6758-26b8-c9ea-4c5d] adds cannot be written in place.

``TlsVerificationError`` moves WITH its factory rather than staying behind:
``_tls_verification_error`` returns it, so splitting the two would make this
module import ``transport`` while ``transport`` imports ``_with_connection_retry``
from here — a circular import. The class and its factory are one unit.

``transport.py`` re-exports every name defined here, so existing importers keep
working unedited — in particular ``test_jira_dc_config_settings.py``, which
reaches for ``TlsVerificationError`` and ``_with_connection_retry`` through the
transport module.
"""

from __future__ import annotations

import sys
import time
from email.message import Message
from typing import Any

from rebar_reconciler._backend import BackendHTTPError


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


def _with_connection_retry(fn: Any) -> Any:
    """Run ``fn()`` with the transport's retry policy.

    Retries up to 2 times (3 total attempts), 2s then 5s backoff, on a
    connection-level fault (see :func:`_connection_retry_exceptions`).
    ``jira.exceptions.JIRAError`` (any HTTP 4xx/5xx response) is NOT one of
    those exception types, so it fails on the FIRST attempt, unretried —
    mirroring ``acli_rest._rest_urlopen_with_retry``'s HTTP-vs-connection
    distinction exactly (retrying a mutation on an HTTP error risks
    duplicates).

    This is ALSO the transport's single translation choke point: every method of
    :class:`JiraDataCenterTransport` routes its library call through here, so
    converting the unretried HTTP error to :class:`BackendHTTPError` here (rather
    than per method) is what stops a vendor exception escaping the adapter.
    """
    retryable = _connection_retry_exceptions()
    http_errors = _jira_http_error_types()
    backoffs = (2, 5)
    last_exc: BaseException | None = None
    for attempt in range(3):
        try:
            return fn()
        except http_errors as exc:
            raise _as_backend_http_error(exc) from exc
        except retryable as exc:
            # Checked BEFORE the retry bookkeeping: SSLError is a ConnectionError
            # subclass, so it lands in `retryable` and would otherwise be re-attempted.
            tls_error = _tls_verification_error(exc)
            if tls_error is not None:
                raise tls_error from exc
            last_exc = exc
        if attempt < 2:
            delay = backoffs[attempt]
            print(
                f"[jira-dc-retry] attempt {attempt + 1} failed ({last_exc!r}); "
                f"retrying in {delay}s …",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
