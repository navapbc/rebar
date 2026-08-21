"""Bug b9b4-f460-2d54-4872 — DC outbound comments must be LENGTH-FITTED on the SEND path.

On Jira Data Center the outbound comment send path applied no length fit at all:
``JiraDataCenterBackend.add_comment`` was a bare delegate to the transport, and the
transport's ``_CommentsMixin.add_comment`` hands ``body`` straight to the jira client.
Bug 6afc established that an over-length comment rejection does not land and is
re-emitted on every pass (the outbound comment-sync loop); DC's ceiling is
deployment-resolved (``comment_max_chars()``, bug 049e), so an unfitted body is MORE
likely to be rejected there, not less.

This suite drives the REAL DC send path — the real ``JiraDataCenterTransport``
(``_links.py`` mixins) under the real ``JiraDataCenterBackend`` — stubbing ONLY the
jira client, and asserts on the body the client actually receives (the wire value):

* an over-ceiling RECONCILER_MARKER-decorated body lands within the ceiling with the
  marker intact (the fit must reuse ``fit_preserving_marker`` — bug 5931's Cloud fix —
  so the loop-breaker marker is never the part that gets cut);
* the differ's dedup key (``sanitizer.fit_comment``) equals the marker-stripped body
  that actually landed — the convergence requirement commit e339 restored for Cloud
  (``fit_comment_as_sent``): if the key and the send disagree, an over-length comment
  can never match on the next pass and re-posts forever;
* in-limit and unlimited-ceiling bodies pass through byte-identical.

The ceiling is injected via ``_DCSanitizer(comment_max_chars=...)`` — the bug-049e
test seam — so no process config is consulted.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.backend import (
    JiraDataCenterBackend,
    _DCSanitizer,
)
from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport
from rebar_reconciler.outbound_comments import (
    RECONCILER_MARKER,
    _decorate_outbound_comment,
)

pytestmark = pytest.mark.unit

#: A module-local ceiling literal, deliberately NOT imported from the code under
#: test (an expectation derived from the constant moves with it and cannot fail).
_CEILING = 4096
#: A user body the ceiling above must truncate.
_LONG_USER_BODY = "x" * 50_000


class _RecordingClient:
    """A ``jira.JIRA``-shaped stub: records exactly what the transport sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        self.sent.append((remote_id, body))
        return {"id": "10001", "body": body}


def _dc_backend(ceiling: int) -> tuple[JiraDataCenterBackend, _RecordingClient]:
    client = _RecordingClient()
    transport = JiraDataCenterTransport(client=client, project="FAKE")
    backend = JiraDataCenterBackend(transport)
    # The bug-049e injection seam: bind a known ceiling without touching config.
    backend.sanitizer = _DCSanitizer(comment_max_chars=ceiling)
    return backend, client


def test_over_ceiling_decorated_body_lands_fitted_with_marker_intact() -> None:
    """THE bug: the wire body must be within ``comment_max_chars()`` AND still
    carry the reconciler marker (the loop-breaker) at its end."""
    backend, client = _dc_backend(_CEILING)
    decorated = _decorate_outbound_comment(_LONG_USER_BODY)
    backend.add_comment("DC-1", decorated)
    ((_, wire),) = client.sent
    assert len(wire) <= _CEILING, (
        f"DC send path handed the transport client an UNFITTED {len(wire)}-char body "
        f"under a {_CEILING}-char ceiling: no length fit is applied before "
        "self._client.add_comment, so Jira rejects it and the outbound comment-sync "
        "loop re-emits it every pass (bug 6afc's mechanism, on DC)"
    )
    assert wire.endswith(RECONCILER_MARKER), (
        "the fit cut the RECONCILER_MARKER off the wire body: DC must fit through "
        "fit_preserving_marker (bug 5931's Cloud fix), never a bare right-truncation"
    )
    assert wire.startswith("x"), "the fitted body lost its leading user content"


def test_dedup_key_equals_the_marker_stripped_landed_body() -> None:
    """Convergence (the e339 class, on DC): ``sanitizer.fit_comment`` must produce
    exactly the marker-stripped body the fitted send path lands, or the differ's
    secondary body-equality skip can never fire for an over-length comment."""
    backend, client = _dc_backend(_CEILING)
    backend.add_comment("DC-1", _decorate_outbound_comment(_LONG_USER_BODY))
    ((_, wire),) = client.sent
    decoration = _decorate_outbound_comment("")
    assert wire.endswith(decoration)
    landed_user_content = wire[: len(wire) - len(decoration)]
    assert backend.sanitizer.fit_comment(_LONG_USER_BODY) == landed_user_content, (
        "the differ's dedup key (fit_comment) and the landed wire body disagree: "
        "an over-length DC comment can never match on the next pass and re-posts "
        "forever — the exact desync commit e339 fixed for Cloud"
    )


def test_in_limit_decorated_body_passes_through_byte_identical() -> None:
    backend, client = _dc_backend(_CEILING)
    decorated = _decorate_outbound_comment("y" * 100)
    backend.add_comment("DC-1", decorated)
    ((_, wire),) = client.sent
    assert wire == decorated


def test_zero_ceiling_means_unlimited_passes_through_untouched() -> None:
    """``0`` is jpm.xml's documented "unlimited"; the fit must not bite at all."""
    backend, client = _dc_backend(0)
    decorated = _decorate_outbound_comment(_LONG_USER_BODY)
    backend.add_comment("DC-1", decorated)
    ((_, wire),) = client.sent
    assert wire == decorated
