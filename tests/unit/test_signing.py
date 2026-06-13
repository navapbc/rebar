"""Unit tests for the pure crypto core of rebar.signing.

These exercise the deterministic, I/O-free helpers (manifest validation, payload
canonicalisation, HMAC computation, key fingerprinting, and verdict logic) in
isolation — no git store, no network. The store/CLI/MCP integration is pinned in
tests/interfaces/test_signature.py.
"""

from __future__ import annotations

import pytest

from rebar import signing
from rebar.signing import SigningError

KEY = b"environment-secret-key"
OTHER = b"a-different-environment-key"


# ── parse_manifest validation ─────────────────────────────────────────────────
def test_parse_manifest_accepts_list_and_json_string() -> None:
    steps = ["ran tests", "lint clean"]
    assert signing.parse_manifest(steps) == steps
    assert signing.parse_manifest('["ran tests", "lint clean"]') == steps


def test_parse_manifest_rejects_non_json() -> None:
    with pytest.raises(SigningError, match="not valid JSON"):
        signing.parse_manifest("not json")


def test_parse_manifest_rejects_non_array() -> None:
    with pytest.raises(SigningError, match="must be a JSON array"):
        signing.parse_manifest('{"a": 1}')


def test_parse_manifest_rejects_empty() -> None:
    with pytest.raises(SigningError, match="at least one verified step"):
        signing.parse_manifest("[]")


def test_parse_manifest_rejects_non_string_or_blank_item() -> None:
    with pytest.raises(SigningError, match=r"manifest\[1\]"):
        signing.parse_manifest('["ok", 42]')
    with pytest.raises(SigningError, match=r"manifest\[0\]"):
        signing.parse_manifest('["   "]')


# ── compute_signature determinism + binding ───────────────────────────────────
def test_signature_is_deterministic() -> None:
    m = ["a", "b"]
    assert signing.compute_signature("tid-1", m, KEY) == signing.compute_signature("tid-1", m, KEY)


def test_signature_binds_ticket_id() -> None:
    m = ["a", "b"]
    assert signing.compute_signature("tid-1", m, KEY) != signing.compute_signature("tid-2", m, KEY)


def test_signature_binds_manifest_and_order() -> None:
    assert signing.compute_signature("t", ["a", "b"], KEY) != signing.compute_signature(
        "t", ["a", "c"], KEY
    )
    # order is significant (the manifest is an ordered list of steps)
    assert signing.compute_signature("t", ["a", "b"], KEY) != signing.compute_signature(
        "t", ["b", "a"], KEY
    )


def test_signature_binds_key() -> None:
    m = ["a"]
    assert signing.compute_signature("t", m, KEY) != signing.compute_signature("t", m, OTHER)


# ── key fingerprint ───────────────────────────────────────────────────────────
def test_key_fingerprint_is_stable_and_distinct() -> None:
    assert signing.key_fingerprint(KEY) == signing.key_fingerprint(KEY)
    assert signing.key_fingerprint(KEY) != signing.key_fingerprint(OTHER)
    # never leaks the key material
    assert KEY.decode() not in signing.key_fingerprint(KEY)


# ── verify_record verdicts ────────────────────────────────────────────────────
def _record(ticket_id: str, manifest: list[str], key: bytes) -> dict:
    return {
        "manifest": manifest,
        "algorithm": signing.ALGORITHM,
        "signature": signing.compute_signature(ticket_id, manifest, key),
        "key_id": signing.key_fingerprint(key),
        "head_sha": "abc123",
        "signed_at": 1,
    }


def test_verify_certified() -> None:
    rec = _record("t", ["a", "b"], KEY)
    out = signing.verify_record(rec, "t", KEY)
    assert out["verified"] is True
    assert out["verdict"] == "certified"
    assert out["step_count"] == 2


def test_verify_unsigned() -> None:
    out = signing.verify_record(None, "t", KEY)
    assert out["verified"] is False and out["verdict"] == "unsigned"


def test_verify_mismatch_on_tampered_manifest() -> None:
    rec = _record("t", ["a", "b"], KEY)
    rec["manifest"] = ["a", "b", "sneaky extra step"]  # tamper, keep old signature
    out = signing.verify_record(rec, "t", KEY)
    assert out["verified"] is False and out["verdict"] == "mismatch"


def test_verify_foreign_key() -> None:
    # Signed by OTHER environment; certifying with our KEY must not claim certified.
    rec = _record("t", ["a"], OTHER)
    out = signing.verify_record(rec, "t", KEY)
    assert out["verified"] is False and out["verdict"] == "foreign_key"
    assert signing.key_fingerprint(OTHER) in out["reason"]


def test_verify_mismatch_when_ticket_id_differs() -> None:
    # A signature lifted onto another ticket fails (ticket_id is bound).
    rec = _record("t", ["a"], KEY)
    out = signing.verify_record(rec, "other-ticket", KEY)
    assert out["verified"] is False and out["verdict"] == "mismatch"


# ── signing_key resolution (env override vs generated file) ───────────────────
def test_signing_key_prefers_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("REBAR_SIGNING_KEY", "injected")
    assert signing.signing_key(tmp_path) == b"injected"


def test_signing_key_generates_and_gitignores(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    k1 = signing.signing_key(tmp_path)
    assert (tmp_path / ".signing-key").exists()
    assert ".signing-key" in (tmp_path / ".gitignore").read_text()
    # stable across calls (does not regenerate)
    assert signing.signing_key(tmp_path) == k1
