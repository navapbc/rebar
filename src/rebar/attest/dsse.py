"""DSSE v1.0.0 envelope + Pre-Authentication Encoding (PAE).

API STUB — signatures are pinned here so downstream tickets (the scheme registry,
518b) can build against a stable surface; bodies are filled by the implementer.

Contract:

* ``pae(payload_type, body) -> bytes`` implements DSSE v1.0.0 PAE:
  ``"DSSEv1" SP LEN(type) SP type SP LEN(body) SP body`` (SP = one ASCII space,
  LEN = ASCII-decimal *byte* length).
* ``encode(payload_type, body, signatures) -> str`` / ``decode(text) -> Envelope``
  serialize a DSSE envelope with base64-encoded ``payload``/``sig`` fields.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Signature:
    keyid: str
    sig: bytes


@dataclass(frozen=True)
class Envelope:
    payload_type: str
    payload: bytes
    signatures: list[Signature]

    def pae(self) -> bytes:
        return pae(self.payload_type, self.payload)


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE v1.0.0 Pre-Authentication Encoding of ``(payload_type, body)``.

    ``PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body``
    where ``SP`` is a single ASCII space and ``LEN(x)`` is the ASCII-decimal
    UTF-8 *byte* length of ``x``. The type and body bytes are appended verbatim;
    the length prefixes disambiguate any embedded spaces/newlines.
    """
    if not isinstance(body, bytes):
        raise TypeError(f"body must be bytes, got {type(body).__name__}")
    type_bytes = payload_type.encode("utf-8")
    return b" ".join(
        [
            b"DSSEv1",
            str(len(type_bytes)).encode("ascii"),
            type_bytes,
            str(len(body)).encode("ascii"),
            body,
        ]
    )


def encode(payload_type: str, body: bytes, signatures: list[dict]) -> str:
    """Serialize a DSSE envelope to a JSON string.

    ``payload`` and each ``sig`` are standard base64 (RFC 4648 §4, with padding).
    Each input signature is a ``{"keyid": str, "sig": bytes}`` mapping.
    """
    envelope = {
        "payload": base64.b64encode(body).decode("ascii"),
        "payloadType": payload_type,
        "signatures": [
            {
                "keyid": sig["keyid"],
                "sig": base64.b64encode(sig["sig"]).decode("ascii"),
            }
            for sig in signatures
        ],
    }
    return json.dumps(envelope)


def decode(text: str) -> Envelope:
    """Parse a DSSE envelope JSON string into an :class:`Envelope`.

    The ``payload`` and each ``sig`` are base64-decoded back to the exact
    original bytes. Malformed JSON or a missing required field raises.
    """
    obj = json.loads(text)
    payload = base64.b64decode(obj["payload"])
    payload_type = obj["payloadType"]
    signatures = [
        Signature(keyid=sig["keyid"], sig=base64.b64decode(sig["sig"])) for sig in obj["signatures"]
    ]
    return Envelope(
        payload_type=payload_type,
        payload=payload,
        signatures=signatures,
    )
