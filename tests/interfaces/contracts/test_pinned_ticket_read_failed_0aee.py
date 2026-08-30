"""Contract oracle for 0aee-a70d-045b-48a1.

``show_ticket``'s immutable/pinned-view read path stamps a raised failure with
``error_code = "pinned_ticket_read_failed"`` (``src/rebar/_reads.py``), and
``error_code_for`` returns that stamped code — but the code was NOT a member of
``KNOWN_ERROR_CODES``, an emit-without-register vocabulary-contract gap. A consumer that
validates a surfaced code against the known vocabulary would reject a genuinely-emitted
code.

This oracle pins the closure: the code a producer can EMIT (observed by driving the real
``show_ticket`` pinned-view failure path) is REGISTERED in the exported vocabulary.
"""

from __future__ import annotations

import pytest

import rebar
from rebar._engine_support.reads import use_ticket_view


class _RaisingView:
    """A bound immutable ticket view whose read fails with a NON-not-found error, so
    ``show_ticket`` classifies it as ``pinned_ticket_read_failed`` (not ``ticket_not_found``)."""

    def show_ticket(self, ticket_id: str, *, include_inbound: bool = False) -> dict:
        raise RuntimeError("pinned snapshot object is corrupt")


def test_pinned_ticket_read_failed_is_registered() -> None:
    assert "pinned_ticket_read_failed" in rebar.KNOWN_ERROR_CODES


def test_show_ticket_pinned_failure_emits_a_registered_code() -> None:
    with use_ticket_view(_RaisingView()):
        with pytest.raises(Exception) as excinfo:
            rebar.show_ticket("any-ticket")
    err = excinfo.value
    # Observable: the stamped code, the public classifier, and the vocabulary agree.
    assert getattr(err, "error_code", None) == "pinned_ticket_read_failed"
    code = rebar.error_code_for(err)
    assert code == "pinned_ticket_read_failed"
    assert code in rebar.KNOWN_ERROR_CODES
