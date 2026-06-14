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


# ── head_sha sentinel ─────────────────────────────────────────────────────────
def test_head_sha_unknown_on_non_git(tmp_path) -> None:
    # The freshness-binding sentinel: unresolvable HEAD -> 'unknown' (callers must
    # treat it as never-matching, never as a value, so the gate can't be voided).
    assert signing.head_sha(str(tmp_path)) == "unknown"


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


def test_every_verdict_has_uniform_shape() -> None:
    # A consumer must be able to read manifest/step_count regardless of verdict
    # (no KeyError on the unsigned path — there is no outputSchema to enforce it).
    keys = {"verified", "verdict", "reason", "manifest", "step_count", "key_id", "signed_at", "head_sha"}
    certified = signing.verify_record(_record("t", ["a"], KEY), "t", KEY)
    unsigned = signing.verify_record(None, "t", KEY)
    foreign = signing.verify_record(_record("t", ["a"], OTHER), "t", KEY)
    for out in (certified, unsigned, foreign):
        assert keys <= set(out), f"{out['verdict']} missing keys: {keys - set(out)}"
    assert unsigned["manifest"] == [] and unsigned["step_count"] == 0


def test_verify_missing_key_id_fails_closed() -> None:
    # A record with a signature but no fingerprint cannot be attributed to an
    # environment; verifying with a foreign key must NOT certify (fail closed).
    rec = _record("t", ["a"], OTHER)
    rec["key_id"] = ""  # strip the fingerprint
    out = signing.verify_record(rec, "t", KEY)
    assert out["verified"] is False and out["verdict"] == "mismatch"


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


def test_signing_key_env_is_stripped_for_file_symmetry(monkeypatch, tmp_path) -> None:
    # An injected key copied with a trailing newline must fingerprint identically
    # to the bare value (the file form strips), so sign-here/verify-here agrees.
    monkeypatch.setenv("REBAR_SIGNING_KEY", "abc-key\n")
    assert signing.signing_key(tmp_path) == b"abc-key"


def test_signing_key_file_is_owner_only(monkeypatch, tmp_path) -> None:
    import os as _os
    import stat

    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    signing.signing_key(tmp_path)
    mode = stat.S_IMODE(_os.stat(tmp_path / ".signing-key").st_mode)
    assert mode == 0o600, f"signing key world/group-readable: {oct(mode)}"


def test_signing_key_generates_and_is_stable(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    k1 = signing.signing_key(tmp_path)
    assert (tmp_path / ".signing-key").exists()
    assert k1  # non-empty
    # stable across calls (does not regenerate)
    assert signing.signing_key(tmp_path) == k1


@pytest.mark.parametrize("envval", ["", "   ", "\n"])
def test_signing_key_blank_env_falls_back_to_file(monkeypatch, tmp_path, envval) -> None:
    # An empty / whitespace-only REBAR_SIGNING_KEY must NOT be used as the key
    # (it would fingerprint to a fixed, attacker-knowable value); fall back to file.
    monkeypatch.setenv("REBAR_SIGNING_KEY", envval)
    k = signing.signing_key(tmp_path)
    assert (tmp_path / ".signing-key").exists()
    assert k == (tmp_path / ".signing-key").read_text().strip().encode()


def test_signing_key_read_only_does_not_create_file(monkeypatch, tmp_path) -> None:
    # The verify path resolves read-only: a missing key must NOT be minted on disk.
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    k = signing.signing_key(tmp_path, create_if_missing=False)
    assert not (tmp_path / ".signing-key").exists()
    assert k == b""  # the _NO_KEY sentinel certifies nothing


def test_signing_key_no_runtime_gitignore_pollution(monkeypatch, tmp_path) -> None:
    # Generating the key on demand must not dirty a worktree .gitignore (N1) —
    # the committed gitignore (ticket-init.sh) owns ignoring .signing-key.
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    (tmp_path / ".gitignore").write_text(".cache.json\n")
    signing.signing_key(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == ".cache.json\n"


def test_concurrent_first_use_keys_agree(monkeypatch, tmp_path) -> None:
    # S1 regression: N threads racing the on-demand key generation (file absent,
    # no env key) must ALL resolve the SAME key/fingerprint — never a torn empty
    # read that splits one environment into two.
    import threading

    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    barrier = threading.Barrier(16)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        fp = signing.key_fingerprint(signing.signing_key(tmp_path))
        with lock:
            results.append(fp)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 1, f"threads disagreed on key: {set(results)}"
    # and the resolved fingerprint matches the file that actually landed
    assert results[0] == signing.key_fingerprint(
        (tmp_path / ".signing-key").read_text().strip().encode()
    )


# ── verify_record fails closed on malformed records ───────────────────────────
@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 42, 3.14, True])
def test_verify_record_non_dict_never_raises(bad) -> None:
    out = signing.verify_record(bad, "t", KEY)  # must not raise AttributeError
    assert out["verified"] is False
    assert out["verdict"] in ("unsigned", "mismatch")
    assert out["manifest"] == [] and out["step_count"] == 0


def test_verify_record_non_list_manifest_reports_zero_steps() -> None:
    rec = {"manifest": {"not": "a list"}, "signature": "deadbeef", "key_id": signing.key_fingerprint(KEY)}
    out = signing.verify_record(rec, "t", KEY)
    assert out["verified"] is False and out["verdict"] == "mismatch"
    assert out["step_count"] == 0  # honest count, not len(dict)


def test_verify_record_empty_signature_is_unsigned() -> None:
    rec = {"manifest": ["a"], "signature": "", "key_id": signing.key_fingerprint(KEY)}
    out = signing.verify_record(rec, "t", KEY)
    assert out["verdict"] == "unsigned"


# ── empty key (_NO_KEY) must never certify (security regression) ──────────────
def test_empty_key_never_certifies_even_with_crafted_signature() -> None:
    # The attack: in a key-less environment the key is b"" (_NO_KEY). HMAC under an
    # empty key is computable by anyone, so a crafted record whose signature was
    # made with the empty key must NOT be certified (would bypass the close gate).
    forged = ["forged: PASS"]
    crafted = {
        "manifest": forged,
        "signature": signing.compute_signature("t", forged, signing._NO_KEY),
        "key_id": signing.key_fingerprint(signing._NO_KEY),
    }
    out = signing.verify_record(crafted, "t", signing._NO_KEY)
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key"


def test_empty_key_unsigned_ticket_still_unsigned() -> None:
    assert signing.verify_record(None, "t", signing._NO_KEY)["verdict"] == "unsigned"


def test_signing_key_empty_file_raises_on_sign(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    (tmp_path / ".signing-key").write_text("   \n")  # whitespace-only == empty
    with pytest.raises(SigningError, match="empty"):
        signing.signing_key(tmp_path, create_if_missing=True)


def test_signing_key_empty_file_is_no_key_on_verify(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("REBAR_SIGNING_KEY", raising=False)
    (tmp_path / ".signing-key").write_text("\n\n")
    assert signing.signing_key(tmp_path, create_if_missing=False) == signing._NO_KEY


def test_process_signature_coerces_non_list_manifest() -> None:
    # A non-list manifest (e.g. a dict) must not be persisted into reduced state.
    from rebar.reducer._processors import process_signature
    from rebar.reducer._state import make_initial_state

    s = make_initial_state()
    ev = {"uuid": "u", "timestamp": 1, "author": "a",
          "data": {"manifest": {"not": "a list"}, "signature": "x", "key_id": "k"}}
    process_signature(s, ev, ev["data"])
    assert s["signature"]["manifest"] == []


# ── process_signature is last-writer-wins (the merge contract) ────────────────
def test_process_signature_is_last_writer_wins() -> None:
    from rebar.reducer._processors import process_signature
    from rebar.reducer._state import make_initial_state

    ev_a = {"uuid": "aaaa", "timestamp": 1, "author": "x", "data": {"manifest": ["A"], "signature": "sa", "key_id": "k"}}
    ev_b = {"uuid": "bbbb", "timestamp": 2, "author": "x", "data": {"manifest": ["B"], "signature": "sb", "key_id": "k"}}

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
