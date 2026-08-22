"""Story 4cee — the outbound comment dedup key is the READBACK of the bytes sent.

Before this story the differ's dedup key was a *prediction*: it composed
decorate → ``sanitizer.fit_comment`` → ``codec.to_wire`` → decode, while the
mutation it queued carried the UNFITTED decorated body and each adapter
re-derived the wire independently at send time.  The key was therefore correct
only while three separately-maintained compositions stayed in lockstep, and
every historical divergence re-posted an over-length comment on every pass.

``fit_for_send`` computes the wire text ONCE.  The differ keys on the decoded
readback of that text and queues the text itself, so both enactment consumers
hand the transport exactly the bytes the key was taken from.

The convergence property is asserted end-to-end rather than by restating the
composition: post a comment, feed the wire the transport received back in as
Jira's stored comment, and the next diff must emit NOTHING.  Under the old
shape that second pass re-emits, because the stored body is the FITTED text
while the key predicted a differently-fitted one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from rebar_reconciler.outbound_comments import (
    RECONCILER_MARKER,
    _diff_comments,
    _normalize_comment_body,
    fit_for_send,
    fit_preserving_marker,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCH_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "dispatch_one.py"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "cloud_adf_corpus"

#: Long enough to exceed Cloud's serialized-ADF comment ceiling by a wide margin.
_CLOUD_OVER_LIMIT = "c" * 40_000
#: DC's ceiling is injected, so a short body suffices there.
_DC_CEILING = 200
_DC_OVER_LIMIT = "d" * 2_000


def _cloud_parts() -> tuple[Any, Any, Any, str]:
    from rebar_reconciler.adapters.jira.backend import (
        _JiraInbound,
        _JiraOutbound,
        _JiraSanitizer,
    )

    return _JiraSanitizer(), _JiraOutbound().comment_codec, _JiraInbound(), _CLOUD_OVER_LIMIT


def _dc_parts() -> tuple[Any, Any, Any, str]:
    from rebar_reconciler.adapters.jira_datacenter.backend import (
        _DCInbound,
        _DCOutbound,
        _DCSanitizer,
    )

    return (
        _DCSanitizer(comment_max_chars=_DC_CEILING),
        _DCOutbound().comment_codec,
        _DCInbound(),
        _DC_OVER_LIMIT,
    )


@pytest.fixture(params=["cloud", "dc"])
def deployment(request):
    """A real (sanitizer, codec, inbound_mapper, over_limit_body) quadruple.

    Both Jira deployments are exercised because each has its OWN fitter and its
    own wire shape; a key that is only symmetric for one of them is the bug.
    """
    parts = _cloud_parts() if request.param == "cloud" else _dc_parts()
    sanitizer, codec, mapper, over_limit = parts
    return request.param, sanitizer, codec, mapper, over_limit


@pytest.fixture(scope="module")
def dispatch():
    """``dispatch_one`` loaded from source (matches the sibling dispatch suites)."""
    spec = importlib.util.spec_from_file_location("dispatch_one_fit_for_send_test", DISPATCH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch_one_fit_for_send_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _ticket(body: str) -> dict[str, Any]:
    return {"comments": [{"body": body, "timestamp": "1000-abc"}]}


def _snapshot(jira_key: str, bodies: list[Any]) -> dict[str, Any]:
    return {jira_key: {"comment": {"comments": [{"body": b} for b in bodies]}}}


def _diff(ticket, snapshot, sanitizer, codec, mapper, jira_key="DIG-1"):
    return _diff_comments(
        ticket,
        jira_key,
        snapshot,
        client=None,
        inbound_mapper=mapper,
        sanitizer=sanitizer,
        codec=codec,
    )


def _send_and_capture(dispatch, mutations, *, path: str) -> str:
    """Drive a real enactment consumer over *mutations* and return the body the
    transport received.

    The transport is a double that records what it was handed and sends it
    verbatim — the contract this story establishes for the enactment sites:
    the bytes are decided by the differ, not re-derived downstream.
    """
    client = MagicMock()
    client.search_issues.return_value = []
    client.create_issue.return_value = {"key": "DIG-1"}
    client.update_issue.return_value = {"key": "DIG-1", "ok": True}
    client.add_comment.return_value = {"id": "10001"}
    if path == "create":
        dispatch.create_one(
            {
                "local_id": "t-1",
                "action": "create",
                "fields": {"summary": "s", "issuetype": {"name": "Task"}},
                "comments": mutations,
            },
            client,
            repo_root=REPO_ROOT,
        )
    else:
        dispatch.update_one(
            {"action": "update", "key": "DIG-1", "fields": {"summary": "s"}, "comments": mutations},
            client,
        )
    assert client.add_comment.call_count == 1, "exactly one comment post expected"
    return client.add_comment.call_args[0][1]


@pytest.mark.parametrize("path", ["create", "update"])
def test_over_limit_comment_converges_after_one_pass(deployment, dispatch, path):
    """AC-1. The key IS the readback of the bytes sent, so a comment that has
    landed is never re-emitted.

    Pass 1 diffs against an empty Jira, posts, and the transport records the
    wire.  Pass 2 diffs against a Jira holding exactly that wire.  A prediction
    that disagreed with the send by even one character re-emits here.
    """
    _name, sanitizer, codec, mapper, over_limit = deployment

    first = _diff(_ticket(over_limit), _snapshot("DIG-1", []), sanitizer, codec, mapper)
    assert len(first) == 1, "an unmirrored comment must be emitted once"

    sent = _send_and_capture(dispatch, first, path=path)
    landed_wire = codec.to_wire(sent)

    second = _diff(_ticket(over_limit), _snapshot("DIG-1", [landed_wire]), sanitizer, codec, mapper)
    assert second == [], (
        "the comment landed, so the next pass must emit nothing; the key and the "
        "bytes actually sent have diverged"
    )


def test_key_equals_the_marker_stripped_decode_of_what_the_transport_received(deployment, dispatch):
    """AC-1, stated directly on the two values it equates."""
    _name, sanitizer, codec, mapper, over_limit = deployment

    fitted = fit_for_send(over_limit, sanitizer=sanitizer, codec=codec, inbound_mapper=mapper)
    mutations = _diff(_ticket(over_limit), _snapshot("DIG-1", []), sanitizer, codec, mapper)
    sent = _send_and_capture(dispatch, mutations, path="create")

    assert sent == fitted.wire_body
    assert fitted.landed_text == _normalize_comment_body(codec.to_wire(sent), inbound_mapper=mapper)
    assert RECONCILER_MARKER not in fitted.landed_text, "the key compares USER content"
    assert fitted.wire_body.endswith(RECONCILER_MARKER), (
        "the loop-breaker marker must survive the fit (bug 5931)"
    )


def test_both_enactment_paths_send_identical_bytes(deployment, dispatch):
    """AC-4. Create and update are structurally identical consumers; scoping
    only one leaves the other posting the unfitted body."""
    _name, sanitizer, codec, mapper, over_limit = deployment
    mutations = _diff(_ticket(over_limit), _snapshot("DIG-1", []), sanitizer, codec, mapper)

    created = _send_and_capture(dispatch, mutations, path="create")
    updated = _send_and_capture(dispatch, mutations, path="update")

    assert created == updated
    assert len(created) < len(over_limit), "an over-limit body must reach the wire fitted"


@pytest.mark.parametrize("path", ["create", "update"])
def test_refitting_the_sent_bytes_changes_nothing(deployment, dispatch, path):
    """AC-5. The adapters keep a defensive fit for their direct callers, so the
    key is only sound if re-fitting an already-fitted body is byte-identical.
    Asserted on each deployment's REAL fitter, not assumed."""
    name, sanitizer, codec, mapper, over_limit = deployment
    mutations = _diff(_ticket(over_limit), _snapshot("DIG-1", []), sanitizer, codec, mapper)
    sent = _send_and_capture(dispatch, mutations, path=path)

    assert _refitter(name, codec)(sent) == sent, (
        "the adapter's defensive fit must not move the bytes"
    )


@pytest.mark.parametrize("path", ["create", "update"])
def test_an_entry_without_wire_body_still_sends_its_body(deployment, dispatch, path):
    """AC-6. Mutation entries queued before the cutover — and every entry the
    create-path mapper emits — carry no wire text; they must still post."""
    _name, _sanitizer, _codec, _mapper, _over = deployment
    legacy = [{"action": "add", "body": "queued before the cutover", "local_comment_key": "1"}]

    assert _send_and_capture(dispatch, legacy, path=path) == "queued before the cutover"


def test_an_already_mirrored_in_limit_comment_is_not_re_emitted(deployment):
    """AC-3. The key is byte-identical to the pre-story one, so the ordinary
    in-limit dedup decision is unchanged."""
    _name, sanitizer, codec, mapper, _over = deployment
    body = "an ordinary short comment"
    fitted = fit_for_send(body, sanitizer=sanitizer, codec=codec, inbound_mapper=mapper)
    landed = codec.to_wire(fitted.wire_body)

    assert _diff(_ticket(body), _snapshot("DIG-1", [landed]), sanitizer, codec, mapper) == []
    assert len(_diff(_ticket(body), _snapshot("DIG-1", []), sanitizer, codec, mapper)) == 1


def _refitter(name: str, codec: Any):
    """Each deployment's REAL comment fitter, as the adapter would apply it."""
    if name == "cloud":
        return lambda text: fit_preserving_marker(text, codec.fit_outbound)
    from rebar_reconciler.adapters.jira_datacenter import backend as dc_backend

    return lambda text: fit_preserving_marker(
        text, lambda inner: dc_backend._truncate_dc_comment_body(inner, _DC_CEILING)
    )


def _corpus_bodies() -> list[str]:
    bodies: list[str] = []
    for path in sorted(CORPUS_DIR.glob("bodies_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload if isinstance(payload, list) else payload.get("bodies", [])
        for entry in entries:
            value = entry.get("body") if isinstance(entry, dict) else entry
            if isinstance(value, str) and value:
                bodies.append(value)
    return bodies


def test_the_corpus_converges_and_carries_the_marker_on_every_body(deployment):
    """AC-2, over real captured bodies rather than synthetic strings.

    Three statements per body, each one a way the dedup has actually broken:
    the wire text still carries the loop-breaker marker (bug 5931 sheared it off
    the tail); re-fitting the wire text is a no-op (an unfitted or double-fitted
    wire lands as different bytes than the key predicted); and once that wire is
    what Jira holds, the differ emits NOTHING (bugs 6afc/17c3 re-posted forever).
    """
    name, sanitizer, codec, mapper, _over = deployment
    bodies = _corpus_bodies()
    assert bodies, f"corpus fixture is empty or missing under {CORPUS_DIR}"
    refit = _refitter(name, codec)

    for body in bodies:
        fitted = fit_for_send(body, sanitizer=sanitizer, codec=codec, inbound_mapper=mapper)
        assert fitted.wire_body.endswith(RECONCILER_MARKER), (
            f"the marker must survive the fit; body {body[:60]!r}"
        )
        assert refit(fitted.wire_body) == fitted.wire_body, (
            f"the wire text must already be fitted; body {body[:60]!r}"
        )
        landed = codec.to_wire(fitted.wire_body)
        assert _diff(_ticket(body), _snapshot("DIG-1", [landed]), sanitizer, codec, mapper) == [], (
            f"a landed corpus body must not be re-emitted; body {body[:60]!r}"
        )
