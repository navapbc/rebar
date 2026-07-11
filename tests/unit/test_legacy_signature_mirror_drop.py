"""Contract-phase drop of the legacy ``state['signature']`` mirror (task 352b, epic
dark-acme-lumen).

Pins: (1) new SNAPSHOTs omit the legacy ``signature`` mirror by default but keep the
kind-keyed ``attestations`` map; (2) the rollback flag re-includes the mirror;
(3) ``most_recent_attestation`` reproduces the mirror's "latest signature of any kind"
semantics from the map, with a defensive fallback to the legacy mirror; (4) a mirror-less
snapshot still round-trips through ``process_snapshot`` and verifies via attestations.
"""

from __future__ import annotations

from rebar._commands.compact import _snapshot_strip_keys
from rebar.reducer._processors import process_signature, process_snapshot
from rebar.reducer._state import make_initial_state
from rebar.signing import most_recent_attestation


def _sig_event(manifest, *, uuid="u", ts=1, sig="hex", signed_at=None):
    data = {
        "manifest": manifest,
        "signature": sig,
        "key_id": "k",
        "algorithm": "HMAC-SHA256",
    }
    if signed_at is not None:
        data["signed_at"] = signed_at
    return {"uuid": uuid, "timestamp": ts, "author": "a", "data": data}


def _apply(state, manifest, **kw):
    ev = _sig_event(manifest, **kw)
    process_signature(state, ev, ev["data"])


# ── strip-key policy ───────────────────────────────────────────────────────────
def test_strip_keys_drop_mirror_by_default() -> None:
    keys = _snapshot_strip_keys(emit_legacy_mirror=False)
    assert "updated_at" in keys and "signature" in keys


def test_strip_keys_keep_mirror_on_rollback() -> None:
    keys = _snapshot_strip_keys(emit_legacy_mirror=True)
    assert "updated_at" in keys and "signature" not in keys


# ── most_recent_attestation reproduces the mirror's semantics from the map ──────
def test_most_recent_prefers_latest_signed_at_in_map() -> None:
    s = make_initial_state()
    _apply(s, ["plan-review: PASS", "ticket: t"], uuid="a", ts=1, signed_at="2026-01-01")
    _apply(s, ["completion-verifier: PASS", "ticket: t"], uuid="b", ts=2, signed_at="2026-02-02")
    # Both kinds coexist; the most-recent (by signed_at) is the completion-verifier record —
    # exactly what the legacy single-slot mirror held.
    rec = most_recent_attestation(s)
    assert rec["manifest"][0] == "completion-verifier: PASS"


def test_most_recent_falls_back_to_legacy_mirror_when_map_absent() -> None:
    # A pre-attestations snapshot the fold-in did not populate: only the mirror is present.
    state = {"signature": {"manifest": ["plan-review: PASS"], "signed_at": "2026-01-01"}}
    assert most_recent_attestation(state)["manifest"][0] == "plan-review: PASS"


def test_most_recent_none_when_neither_present() -> None:
    assert most_recent_attestation({}) is None


# ── a mirror-less snapshot round-trips and still verifies via attestations ──────
def test_mirrorless_snapshot_roundtrips_and_resolves_via_attestations() -> None:
    # Build a live state with an attestation + mirror, then simulate the contract-phase
    # SNAPSHOT strip (drop the mirror, keep the map).
    live = make_initial_state()
    _apply(live, ["completion-verifier: PASS", "ticket: t"], uuid="a", ts=1, signed_at="2026-03-03")
    assert "signature" in live and "attestations" in live

    strip = _snapshot_strip_keys(emit_legacy_mirror=False)
    compiled = {k: v for k, v in live.items() if k not in strip}
    assert "signature" not in compiled  # mirror dropped from the snapshot payload
    assert "completion-verifier" in compiled["attestations"]  # map retained

    # Restore the snapshot into a fresh state (as process_snapshot does on read).
    restored = make_initial_state()
    process_snapshot(restored, {"compiled_state": compiled})
    # Even without the mirror, the most-recent accessor resolves the record via the map,
    # so kind=None consumers (verify / close gate / validate) keep working.
    rec = most_recent_attestation(restored)
    assert rec is not None and rec["manifest"][0] == "completion-verifier: PASS"


def test_rollback_snapshot_keeps_mirror() -> None:
    live = make_initial_state()
    _apply(live, ["plan-review: PASS", "ticket: t"], uuid="a", ts=1, signed_at="2026-01-01")
    strip = _snapshot_strip_keys(emit_legacy_mirror=True)
    compiled = {k: v for k, v in live.items() if k not in strip}
    assert "signature" in compiled  # rollback keeps the legacy mirror in the snapshot
