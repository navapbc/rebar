"""Unit tests for the pure crypto core of rebar.signing.

These exercise the deterministic, I/O-free helpers (manifest validation, the
retired-scheme verdict, and the reducer's signature fold) in isolation — no git
store, no network. The store/CLI/MCP integration is pinned in
tests/interfaces/test_signature.py.
"""

from __future__ import annotations

import pytest

from rebar import signing
from rebar.signing import SigningError


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


# ── head_sha sentinel ─────────────────────────────────────────────────────────
def test_head_sha_unknown_on_non_git(tmp_path) -> None:
    # The freshness-binding sentinel: unresolvable HEAD -> 'unknown' (callers must
    # treat it as never-matching, never as a value, so the gate can't be voided).
    assert signing.head_sha(str(tmp_path)) == "unknown"


# ── retired_scheme_verdict (HMAC retired 2026-09) ─────────────────────────────
def _legacy_record(manifest: list[str]) -> dict:
    return {
        "manifest": manifest,
        "algorithm": "HMAC-SHA256",
        "signature": "ab" * 32,
        "key_id": "412fa5372ad877e0",
        "head_sha": "abc123",
        "signed_at": 1,
    }


def test_legacy_record_reads_unsigned_with_retired_reason() -> None:
    out = signing.retired_scheme_verdict(_legacy_record(["a", "b"]))
    assert out["verified"] is False and out["verdict"] == "unsigned"
    assert "legacy HMAC scheme retired" in out["reason"]
    assert out["step_count"] == 2 and out["key_id"] == "412fa5372ad877e0"


def test_no_record_reads_plain_unsigned() -> None:
    out = signing.retired_scheme_verdict(None)
    assert out["verdict"] == "unsigned" and out["reason"] == "ticket has no signature"


def test_every_verdict_has_uniform_shape() -> None:
    # A consumer must be able to read manifest/step_count regardless of verdict
    # (no KeyError on the unsigned path — there is no outputSchema to enforce it).
    keys = {
        "verified",
        "verdict",
        "reason",
        "manifest",
        "step_count",
        "key_id",
        "signed_at",
        "head_sha",
    }
    for out in (
        signing.retired_scheme_verdict(_legacy_record(["a"])),
        signing.retired_scheme_verdict(None),
    ):
        assert keys <= set(out), f"{out['verdict']} missing keys: {keys - set(out)}"


def test_verify_attestation_record_routes_non_envelope_to_retired_verdict() -> None:
    out = signing.verify_attestation_record(_legacy_record(["plan-review: PASS"]), "t")
    assert out["verdict"] == "unsigned" and "retired" in out["reason"]


@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 42, 3.14, True])
def test_retired_verdict_non_dict_never_raises(bad) -> None:
    out = signing.retired_scheme_verdict(bad)
    assert out["verified"] is False and out["verdict"] == "unsigned"
    assert out["manifest"] == [] and out["step_count"] == 0


def test_retired_verdict_non_list_manifest_reports_zero_steps() -> None:
    rec = {"manifest": {"not": "a list"}, "signature": "deadbeef", "key_id": "k"}
    out = signing.retired_scheme_verdict(rec)
    assert out["verdict"] == "unsigned" and out["step_count"] == 0


def test_process_signature_coerces_non_list_manifest() -> None:
    # A non-list manifest (e.g. a dict) must not be persisted into reduced state.
    from rebar.reducer._processors import process_signature
    from rebar.reducer._state import make_initial_state

    s = make_initial_state()
    ev = {
        "uuid": "u",
        "timestamp": 1,
        "author": "a",
        "data": {"manifest": {"not": "a list"}, "signature": "x", "key_id": "k"},
    }
    process_signature(s, ev, ev["data"])
    assert s["signature"]["manifest"] == []


# ── process_signature is last-writer-wins (the merge contract) ────────────────
def test_process_signature_is_last_writer_wins() -> None:
    from rebar.reducer._processors import process_signature
    from rebar.reducer._state import make_initial_state

    ev_a = {
        "uuid": "aaaa",
        "timestamp": 1,
        "author": "x",
        "data": {"manifest": ["A"], "signature": "sa", "key_id": "k"},
    }
    ev_b = {
        "uuid": "bbbb",
        "timestamp": 2,
        "author": "x",
        "data": {"manifest": ["B"], "signature": "sb", "key_id": "k"},
    }

    # Whichever is applied LAST wins — the reducer fixes a deterministic apply
    # order (basename sort) so the on-disk replay is convergent (see the interface
    # test for the end-to-end reduce-from-files proof).
    s = make_initial_state()
    process_signature(s, ev_a, ev_a["data"])
    process_signature(s, ev_b, ev_b["data"])
    assert s["signature"]["manifest"] == ["B"]
    s = make_initial_state()
    process_signature(s, ev_b, ev_b["data"])
    process_signature(s, ev_a, ev_a["data"])
    assert s["signature"]["manifest"] == ["A"]
