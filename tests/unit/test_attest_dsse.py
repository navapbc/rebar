"""Happy-path DSSE v1.0.0 tests.

These tests cover the worked PAE example and an envelope encode and decode round trip.
Edge cases, byte identity, and malformed inputs live in ``test_attest_dsse_heldout.py``.

``pae(payload_type: str, body: bytes) -> bytes`` implements this encoding::

    PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body

SP is one ASCII space. LEN is the ASCII-decimal byte length of the following field.
``encode`` and ``decode`` round-trip envelopes with base64-encoded ``payload`` and ``sig``
fields.
"""

from __future__ import annotations

from rebar.attest import dsse


def test_pae_spec_worked_example() -> None:
    # The canonical DSSE v1.0.0 worked example.
    # len("http://example.com/HelloWorld") == 29 ; len("hello world") == 11.
    result = dsse.pae("http://example.com/HelloWorld", b"hello world")
    assert result == b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    assert isinstance(result, bytes)


def test_envelope_roundtrip_preserves_all_fields() -> None:
    body = b'{"predicate": "plan-review", "verdict": "PASS"}'
    text = dsse.encode(
        "application/vnd.rebar.attest+json",
        body,
        [{"keyid": "abc123", "sig": b"\x01\x02\x03rawsig"}],
    )
    # encode returns a JSON string.
    assert isinstance(text, str)

    env = dsse.decode(text)
    assert env.payload_type == "application/vnd.rebar.attest+json"
    assert env.payload == body
    assert len(env.signatures) == 1
    assert env.signatures[0].keyid == "abc123"
    assert env.signatures[0].sig == b"\x01\x02\x03rawsig"


def test_envelope_pae_matches_module_pae() -> None:
    # An Envelope knows how to produce its own PAE bytes, and they equal the
    # module-level pae() over the same (type, body).
    body = b"the exact body bytes"
    env = dsse.decode(dsse.encode("t/type", body, [{"keyid": "", "sig": b"s"}]))
    assert env.pae() == dsse.pae("t/type", body)
