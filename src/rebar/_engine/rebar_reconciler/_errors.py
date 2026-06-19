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
