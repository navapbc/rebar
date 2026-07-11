"""Happy-path spec for the DSSE v1.0.0 envelope + PAE module (task 9fd5).

These are the behavioral tests the implementer works against: the PAE spec worked
example and a basic envelope encode/decode round-trip. Edge cases, the
byte-identity (no-re-serialization) contract, and error inputs live in the
held-out companion suite ``test_attest_dsse_heldout.py`` and are validated by the
orchestrator after implementation.

Contract under test (observable behavior only):

* ``pae(payload_type: str, body: bytes) -> bytes`` implements DSSE v1.0.0
  Pre-Authentication Encoding::

      PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body

  where SP is a single ASCII space (0x20) and LEN is the ASCII-decimal *byte*
  length of the following field.
* ``encode(payload_type, body, signatures) -> str`` and ``decode(text) -> Envelope``
  round-trip a DSSE envelope whose ``payload``/``sig`` fields are base64-encoded.
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
