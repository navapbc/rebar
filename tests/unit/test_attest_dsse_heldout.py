"""Held-out oracle for the DSSE module (task 9fd5) — edge, contract, error cases.

The implementer does NOT see this file. The orchestrator restores and runs it
after implementation to validate behavior the implementer could not tailor code
to: byte-exact PAE for tricky inputs, the no-re-serialization contract (AC3),
and defined error behavior for malformed/invalid input (advisory adv7).

All assertions target observable behavior (return bytes, raised exceptions),
never internal structure.
"""

from __future__ import annotations

import base64
import json

import pytest

from rebar.attest import dsse

# --- PAE byte-exactness for tricky inputs -------------------------------------


def test_pae_empty_body() -> None:
    # LEN(body) is 0 and the trailing SP is still present before the empty body.
    assert dsse.pae("t", b"") == b"DSSEv1 1 t 0 "


def test_pae_length_is_byte_count_not_char_count() -> None:
    # A multi-byte UTF-8 payload type: "é" is 2 bytes in UTF-8, so LEN(type)
    # must count bytes (3), not characters (2).
    result = dsse.pae("aé", b"x")
    assert result == b"DSSEv1 3 a\xc3\xa9 1 x"


def test_pae_body_with_spaces_and_newlines_is_unambiguous() -> None:
    # Length-prefixing means embedded spaces/newlines in the body do not create
    # parsing ambiguity; the bytes are appended verbatim.
    body = b"a b\nc d"
    assert dsse.pae("ty", body) == b"DSSEv1 2 ty 7 a b\nc d"


def test_pae_rejects_non_bytes_body() -> None:
    # body must be bytes; a str body is a programming error, not silently encoded.
    with pytest.raises((TypeError, ValueError)):
        dsse.pae("ty", "not-bytes")  # type: ignore[arg-type]


# --- AC3: verify/decode operates on the EXACT stored bytes (no re-serialization) --


def test_decode_returns_exact_stored_body_bytes() -> None:
    # A JSON body with deliberately non-canonical key order and duplicate-ish
    # whitespace. Round-tripping through the envelope MUST return these exact
    # bytes — never a re-serialized (sorted/normalized) form.
    body = b'{"b": 1, "a": 2,  "z":\t3}'
    env = dsse.decode(dsse.encode("t/type", body, [{"keyid": "", "sig": b"s"}]))
    assert env.payload == body  # byte-exact

    # A canonicalizing re-serialization differs from the stored bytes...
    canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
    assert canonical != body
    # ...and pae() over the stored bytes differs from pae() over the re-serialized
    # form, proving a verifier that used the stored bytes and one that re-serialized
    # would compute different signing inputs.
    assert env.pae() != dsse.pae("t/type", canonical)
    assert env.pae() == dsse.pae("t/type", body)


def test_payload_field_is_base64_of_exact_body() -> None:
    # Fixed-vector: the envelope's payload field is standard base64 of the exact
    # body bytes, decodable back to those bytes.
    body = b"hello world"
    text = dsse.encode("t/type", body, [{"keyid": "k", "sig": b"\x00\xff"}])
    obj = json.loads(text)
    assert base64.b64decode(obj["payload"]) == body
    assert obj["payloadType"] == "t/type"
    assert base64.b64decode(obj["signatures"][0]["sig"]) == b"\x00\xff"
    assert obj["signatures"][0]["keyid"] == "k"


# --- Error behavior (advisory adv7) -------------------------------------------


def test_decode_rejects_malformed_json() -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        dsse.decode("this is not json {")


def test_decode_rejects_missing_required_fields() -> None:
    # Missing payloadType and signatures.
    with pytest.raises((ValueError, KeyError)):
        dsse.decode(json.dumps({"payload": base64.b64encode(b"x").decode()}))


def test_multiple_signatures_roundtrip() -> None:
    body = b"multi-sig body"
    text = dsse.encode(
        "t/type",
        body,
        [{"keyid": "one", "sig": b"sig1"}, {"keyid": "two", "sig": b"sig2"}],
    )
    env = dsse.decode(text)
    assert [(s.keyid, s.sig) for s in env.signatures] == [
        ("one", b"sig1"),
        ("two", b"sig2"),
    ]
