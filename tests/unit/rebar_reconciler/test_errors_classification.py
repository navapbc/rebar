"""Characterization tests for the reconciler's HTTP error-classification helpers.

Pins the CURRENT behavior of ``http_status`` / ``is_not_found`` BEFORE the error-handling
sweep (epic ring-gun-jot, ticket 0569) touches the broad ``except`` sites that feed them
(``apply_outbound.py`` / ``batch_dispatch.py`` read ``is_not_found(exc)`` / ``http_status(exc)``
/ ``getattr(exc, "status_code", ...)`` to drive 404/error classification). Narrowing those
catches would change which exceptions reach the body and thus the classification — these
tests are the safety net that detects any such drift.
"""

from __future__ import annotations

from http import HTTPStatus

from rebar_reconciler._errors import http_status, is_not_found


class _StatusCodeExc(Exception):
    """REST-layer style: carries ``.status_code``."""

    def __init__(self, status_code: int | None) -> None:
        super().__init__("status_code exc")
        self.status_code = status_code


class _CodeExc(Exception):
    """urllib.HTTPError style: carries ``.code``."""

    def __init__(self, code: int | None) -> None:
        super().__init__("code exc")
        self.code = code


class _BothExc(Exception):
    def __init__(self, status_code: int, code: int) -> None:
        super().__init__("both exc")
        self.status_code = status_code
        self.code = code


def test_http_status_reads_status_code() -> None:
    assert http_status(_StatusCodeExc(404)) == 404
    assert http_status(_StatusCodeExc(500)) == 500


def test_http_status_reads_urllib_code() -> None:
    assert http_status(_CodeExc(410)) == 410


def test_http_status_prefers_status_code_over_code() -> None:
    # status_code is consulted first; .code is only the fallback.
    assert http_status(_BothExc(status_code=404, code=500)) == 404


def test_http_status_none_for_non_http_error() -> None:
    assert http_status(ValueError("not an HTTP error")) is None
    assert http_status(_StatusCodeExc(None)) is None


def test_http_status_none_for_non_int_code() -> None:
    # A non-int status (e.g. a string "404") is not a usable HTTP status.
    assert http_status(_StatusCodeExc("404")) is None  # type: ignore[arg-type]


def test_is_not_found_only_true_for_404() -> None:
    assert is_not_found(_StatusCodeExc(404)) is True
    assert is_not_found(_CodeExc(404)) is True
    assert is_not_found(_StatusCodeExc(HTTPStatus.NOT_FOUND)) is True
    # Everything else is False — including the other "gone"-ish codes the callers
    # treat distinctly (410/403) and non-HTTP errors.
    assert is_not_found(_StatusCodeExc(410)) is False
    assert is_not_found(_StatusCodeExc(403)) is False
    assert is_not_found(_StatusCodeExc(500)) is False
    assert is_not_found(ValueError("nope")) is False
