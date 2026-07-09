"""Domain exceptions for rebar_reconciler."""

from http import HTTPStatus


def http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status code from a transport/HTTP exception.

    Normalizes the two attribute conventions that coexist in the reconciler:
    urllib's ``HTTPError.code`` and the REST layer's ``.status_code``. Returns
    ``None`` when neither is present (a non-HTTP error). Centralizing this removes
    the prior inconsistency where some sites read ``exc.code`` (which raised
    AttributeError when the exception instead carried ``status_code``) and others
    read ``getattr(exc, "status_code", None)``.
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def is_not_found(exc: BaseException) -> bool:
    """True when *exc* is an HTTP 404 (the Jira issue is gone → idempotent delete)."""
    return http_status(exc) == HTTPStatus.NOT_FOUND


# ADR 0036: jittered exponential backoff, capped. Mirrors the acli_subprocess
# domain helper so the two retry floors agree on ceiling + jitter shape.
MAX_BACKOFF_S = 60.0


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (integer-seconds form only; ADR 0036).

    The rarer HTTP-date form is not honored (-> None; caller falls back to
    jittered backoff). Jira Cloud does not guarantee the header at all.
    """
    if not value:
        return None
    try:
        secs = float(value.strip())
    except (TypeError, ValueError):
        return None
    return secs if secs >= 0 else None


# ── the unified reconciler error taxonomy (epic 5ca8 / romp-swath-wince) ────────────────────
# OSS-informed (requests RequestException / urllib3 HTTPError / tenacity RetryError + PEP 3134):
# ONE package-level base in this dedicated errors module, raised + re-exported through both the
# `acli` and `applier`/`batch_dispatch` surfaces, so `except RetryExhaustedError` against either
# import catches BOTH retry loops (the two formerly-divergent bodies were `is`-distinct — a latent
# control-flow bug).
class ReconcilerError(Exception):
    """Base class for rebar_reconciler domain errors — the single catch-all seam a caller can
    ``except`` once (mirrors requests' ``RequestException`` / urllib3's ``HTTPError``)."""


class JiraAPIError(ReconcilerError):
    """A Jira HTTP error response, carrying the HTTP ``status_code``. The single canonical body
    (formerly duplicated in ``batch_dispatch``), re-exported via ``applier``/``batch_dispatch``."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryExhaustedError(ReconcilerError, RuntimeError):
    """All retry attempts exhausted after transient HTTP/network errors.

    The MRO ``RetryExhaustedError → ReconcilerError → RuntimeError → Exception`` is caught by
    BOTH the acli path's ``except RuntimeError`` AND the batch path's ``except Exception`` (and by
    ``except ReconcilerError``), so unifying the two formerly-divergent bodies breaks no call site.
    ``message``-first keeps the existing positional ``RetryExhaustedError("…")`` calls working; the
    optional ``last_exception`` / ``attempts`` mirror tenacity ``RetryError`` / urllib3
    ``MaxRetryError`` for post-hoc inspection. Both raise sites chain the cause
    (``raise … from last_error``) per PEP 3134."""

    def __init__(
        self,
        message: str = "",
        *,
        last_exception: BaseException | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.last_exception = last_exception
        self.attempts = attempts


class RebarIdLabelWriteError(Exception):
    """Raised when an unauthorized leaf attempts to emit a rebar-id label mutation.

    Only two applier leaves are authorized to write rebar-id labels:
      - outbound_create: authorized for {create} on rebar-id labels (adds the
        label when a new Jira issue is created for an outbound ticket).
      - inbound_clean_label: authorized for {delete} on rebar-id labels (removes
        stale or duplicated rebar-id-* labels on the Jira side).

    All other applier leaves (outbound_update, outbound_delete, outbound_probe,
    outbound_conflict, inbound_create, inbound_update, inbound_repair_property)
    MUST NOT emit rebar-id label mutations. inbound_repair_property writes the
    local_id entity property field, NOT the label.

    Raise this error when a leaf that is not in _AUTHORIZED_REBAR_ID_LABEL_WRITERS
    attempts to write a rebar-id label.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DirectionMismatchError(Exception):
    """Raised when a Mutation's direction doesn't match its dispatch context."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnknownActionError(Exception):
    """Raised when an unmapped MutationAction reaches the applier dispatch table."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class StatusMappingError(Exception):
    """Raised when Jira <-> local status mapping cannot be resolved."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
