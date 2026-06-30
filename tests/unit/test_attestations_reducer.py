"""Reducer-level tests for the kind-keyed attestations map (story 114f, epic dark-acme-lumen).

These pin the ADDITIVE, behavior-preserving reducer slice: ``state['attestations']`` is a
kind-keyed map (additive across kinds, LWW within a kind) while ``state['signature']`` stays
a most-recent mirror with exact prior single-slot semantics. They guard the coexistence
(grumpy-site-beard) fix, the manifest-authoritative routing, ``last_reopened_at``,
old-snapshot fold-in, and the two-level HMAC-hex strip against regression.
"""

from __future__ import annotations

from rebar.reducer._present import public_state
from rebar.reducer._processors import (
    attestation_kind,
    process_signature,
    process_snapshot,
    process_status,
)
from rebar.reducer._state import make_initial_state


def _sig_event(manifest, *, uuid="u", ts=1, sig="hex", kind=None):
    data = {"manifest": manifest, "signature": sig, "key_id": "k", "algorithm": "HMAC-SHA256"}
    if kind is not None:
        data["kind"] = kind
    return {"uuid": uuid, "timestamp": ts, "author": "a", "data": data}


def _apply_sig(state, manifest, **kw):
    ev = _sig_event(manifest, **kw)
    process_signature(state, ev, ev["data"])


# ── coexistence + within-kind replace (the grumpy-site-beard fix) ───────────────
def test_attestations_coexist_across_kinds() -> None:
    s = make_initial_state()
    _apply_sig(s, ["plan-review: PASS", "ticket: t", "material: m"], uuid="a", ts=1)
    _apply_sig(s, ["completion-verifier: PASS", "ticket: t"], uuid="b", ts=2)
    # Both kinds coexist in the map — signing completion did NOT clobber plan-review.
    assert set(s["attestations"]) == {"plan-review", "completion-verifier"}
    assert s["attestations"]["plan-review"]["manifest"][0] == "plan-review: PASS"
    # The mirror is the most-recent attestation (unchanged single-slot LWW semantics).
    assert s["signature"]["manifest"][0] == "completion-verifier: PASS"


def test_within_kind_replace() -> None:
    s = make_initial_state()
    _apply_sig(s, ["plan-review: PASS", "material: old"], uuid="a", ts=1)
    _apply_sig(s, ["plan-review: PASS", "material: new"], uuid="b", ts=2)
    assert list(s["attestations"]) == ["plan-review"]
    assert s["attestations"]["plan-review"]["manifest"][1] == "material: new"


# ── blank/retired event: no-op for the map, clears the mirror (legacy semantics) ─
def test_blank_event_skips_map_but_clears_mirror() -> None:
    s = make_initial_state()
    _apply_sig(s, ["completion-verifier: PASS"], uuid="a", ts=1)
    # A legacy retirement/blank event (empty manifest).
    ev = {"uuid": "b", "timestamp": 2, "author": "a", "data": {"manifest": [], "signature": ""}}
    process_signature(s, ev, ev["data"])
    # Map keeps the real attestation (blank cannot key a kind → not clobbered).
    assert list(s["attestations"]) == ["completion-verifier"]
    # Mirror is cleared exactly as old single-slot behavior (blank record).
    assert s["signature"]["manifest"] == []


# ── manifest[0] is authoritative; a mismatched unsigned hint never misroutes ────
def test_manifest_authoritative_over_data_kind_hint() -> None:
    # Forged/mismatched envelope hint must NOT override the signed manifest.
    assert attestation_kind(["plan-review: PASS"], {"kind": "completion-verifier"}) == "plan-review"
    assert attestation_kind(["plan-review: PASS"], {"kind": "plan-review"}) == "plan-review"
    assert attestation_kind([], {"kind": "plan-review"}) is None  # blank → unkindable
    assert attestation_kind(["no-colon-here"], {}) is None
    s = make_initial_state()
    _apply_sig(s, ["plan-review: PASS"], kind="completion-verifier")  # lying hint
    assert list(s["attestations"]) == ["plan-review"]  # routed by the manifest, not the hint


# ── last_reopened_at: set only on closed -> open ────────────────────────────────
def _status_event(target, current, *, uuid="s", ts=10):
    return {
        "uuid": uuid,
        "timestamp": ts,
        "author": "a",
        "env_id": "e",
        "data": {"status": target, "current_status": current},
    }


def test_last_reopened_at_set_on_reopen() -> None:
    s = make_initial_state()
    s["status"] = "closed"
    ev = _status_event("open", "closed", ts=42)
    process_status(s, ev, ev["data"], "f")
    assert s["status"] == "open"
    assert s["last_reopened_at"] == 42


def test_last_reopened_at_not_set_on_non_reopen() -> None:
    s = make_initial_state()  # status "open"
    ev = _status_event("in_progress", "open", ts=7)
    process_status(s, ev, ev["data"], "f")
    assert "last_reopened_at" not in s


def test_last_reopened_at_fork_branch_reopen() -> None:
    # Fork (current_status mismatches state) resolving to open from closed still records it.
    s = make_initial_state()
    s["status"] = "closed"
    s["parent_status_uuid"] = "zzzz"  # high → incoming low uuid wins
    ev = _status_event("open", "in_progress", uuid="aaaa", ts=99)  # mismatch → fork
    process_status(s, ev, ev["data"], "f")
    assert s["status"] == "open"
    assert s["last_reopened_at"] == 99


# ── old-snapshot fold-in: legacy single signature -> attestations map ───────────
def test_foldin_legacy_snapshot_signature() -> None:
    s = make_initial_state()
    compiled = {
        "ticket_id": "t",
        "status": "closed",
        "signature": {"manifest": ["completion-verifier: PASS"], "signature": "x", "key_id": "k"},
    }
    process_snapshot(s, {"compiled_state": compiled})
    assert s["attestations"]["completion-verifier"]["manifest"][0] == "completion-verifier: PASS"


def test_foldin_skips_when_attestations_present() -> None:
    s = make_initial_state()
    compiled = {
        "ticket_id": "t",
        "signature": {"manifest": ["plan-review: PASS"], "signature": "x"},
        "attestations": {"completion-verifier": {"manifest": ["completion-verifier: PASS"]}},
    }
    process_snapshot(s, {"compiled_state": compiled})
    # Verbatim restore — fold-in is a no-op when the map already exists.
    assert list(s["attestations"]) == ["completion-verifier"]


def test_foldin_drops_blank_legacy_signature() -> None:
    s = make_initial_state()
    process_snapshot(s, {"compiled_state": {"ticket_id": "t", "signature": {"manifest": []}}})
    assert "attestations" not in s  # blank legacy record → nothing to fold


# ── public_state strips the HMAC hex from every kind + the legacy mirror ────────
def test_public_state_strips_hex_for_all_kinds_and_mirror() -> None:
    s = make_initial_state()
    _apply_sig(s, ["plan-review: PASS"], uuid="a", ts=1, sig="PLAN_HEX")
    _apply_sig(s, ["completion-verifier: PASS"], uuid="b", ts=2, sig="COMP_HEX")
    pub = public_state(s)
    for kind, rec in pub["attestations"].items():
        assert "signature" not in rec, f"hex leaked for kind {kind}"
        assert rec["manifest"][0].startswith(kind)
    assert "signature" not in pub["signature"]  # legacy mirror hex also stripped
    # Non-mutating: the raw reducer state keeps the hex.
    assert s["attestations"]["plan-review"]["signature"] == "PLAN_HEX"


def test_empty_attestations_omitted_from_state() -> None:
    s = make_initial_state()
    assert "attestations" not in s  # never created empty
    assert "attestations" not in public_state(s)
