"""Retry policy + error translation for the Jira Data Center transport (story
S1 [rebar:f2f3-9cb1-335b-4e31], epic e369).

Extracted from ``transport.py`` under the module-size cap (see ADR 0058); this
module changes no behaviour.

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

import random
import sys
import time
from email.message import Message
from typing import Any

from rebar_reconciler._backend import BackendHTTPError
from rebar_reconciler._errors import MAX_BACKOFF_S, parse_retry_after

#: PROVENANCE OF THE THREE RATE-LIMIT NUMBERS, labelled rather than stated as fact (story S2's
#: own plan required each to be either confirmed against the target DC version or explicitly
#: marked unverified, and the OSS research they came from retracted two of its other claims on a
#: second pass — so an unlabelled number here would be exactly the over-trust that warning was
#: about):
#:
#:   * "Data Center only" — CONFIRMED for this project's harness. `/rest/api/2/serverInfo`
#:     reports `deploymentType != "Cloud"`, asserted live by
#:     `test_instance_is_a_server_deployment_at_the_pinned_version`.
#:   * "8.6+" — CONFIRMED to be SATISFIED, which is a weaker claim than confirming the threshold.
#:     The harness runs Jira 8.17.1 and that same live test asserts `version >= (8, 14)`, so the
#:     instance this code is exercised against is comfortably above 8.6. That 8.6 is the exact
#:     version the limiter ARRIVED in is **UNVERIFIED** — sourced from Atlassian documentation,
#:     not tested.
#:   * "off by default (admin-enabled)" — **UNVERIFIED.** Whether the limiter is enabled is an
#:     admin setting this suite never reads, and the harness has never had it switched on. This
#:     is precisely why the retry is driven by the PRESENCE of a `Retry-After` header rather than
#:     by any assumption about the limiter's configuration: with no header the code behaves
#:     exactly as it did before, so being wrong about the default costs nothing.
#:   * "Retry-After plus up to 20% jitter" — **UNVERIFIED against a live limiter.** Sourced from
#:     Atlassian's guidance; no test has observed a real 429 from a real DC token bucket, because
#:     the harness cannot produce one. The jitter is nonetheless load-bearing rather than
#:     decorative: every rebar process sharing a bucket would otherwise wake in lockstep and
#:     re-collide.
#:
#: The jitter is a DELIBERATE divergence from ``dispatch_one``'s 429 branch, which applies
#: ``min(MAX_BACKOFF_S, retry_after)`` with none — the parser and the ceiling are reused from
#: ``_errors``; the delay arithmetic is not.
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
    """Seconds the SERVER asked us to wait, read off the 429 response's ``Retry-After``.

    READ FROM ``exc.response.headers``, **NOT** ``exc.headers`` — verified against
    pycontribs/jira 3.10.5 at runtime rather than assumed. ``JIRAError.__init__`` does
    ``self.headers = kwargs.get("headers", None)`` and its own docstring describes that kwarg as
    "will be used to get REQUEST headers"; the RESPONSE headers, the ones carrying
    ``Retry-After``, hang off ``.response``. Reading ``.headers`` would look right, type-check,
    and silently never find the header — so the rate-limit retry would degrade to "no header
    present" on every single 429.

    Returns ``None`` when there is no usable header, which the caller treats as "do not retry".
    ``getattr`` throughout because a fake client in the unit tests raises an error object with
    neither attribute, and this must not be the thing that breaks it.
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
    """Run ``fn()`` with the transport's retry policy.

    ``rate_limit_retry`` OPTS THIS CALL IN to retrying HTTP 429, and it **defaults to False**.
    That default is the load-bearing part. This function is the single choke point for ALL
    transport call sites INCLUDING ``create_issue``, ``add_comment`` and ``add_label``, and a 429
    can arrive AFTER the server began a write with nothing in the response distinguishing that
    from rejection at the gate — so retrying a mutation here would reintroduce the duplicate-issue
    class bug 21fc just fixed. Opting in per call, rather than opting out, means a mutation added
    later cannot inherit the retry by omission.

    The 429 retry fires ONLY when the response carries a usable ``Retry-After``. Data Center's
    limiter is admin-enabled and absent by default (8.6+, DC only), so with no header this
    behaves exactly as it does today: the error is translated and raised on the first occurrence.

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
