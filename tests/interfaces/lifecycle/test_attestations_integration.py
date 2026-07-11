"""End-to-end integration suite locking the additive-attestations invariants across the real
store (story dbe6, epic dark-acme-lumen). The per-slice unit tests cover each mechanism; this
suite pins the cross-cutting behavior: coexistence (the grumpy-site-beard regression),
compaction round-trip + cross-version snapshot mirror, concurrent different-kind signs,
completion validity under material-vs-non-material edits, and legacy-snapshot fold-in.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config, signing
from rebar._commands._seam import append_event
from rebar.llm.plan_review.attest import compute_validity
from rebar.reducer import reduce_ticket


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-key-dbe6")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _sign(store: Path, tid: str, kind: str, material: str | None = None) -> None:
    manifest = [f"{kind}: PASS", f"ticket: {tid}"]
    if material is not None:
        manifest.append(f"material: {material}")
    signing.sign_manifest(tid, manifest, kind=kind, repo_root=str(store))


def _tdir(store: Path, tid: str) -> Path:
    tracker = Path(config.tracker_dir(str(store)))
    return next(
        d for d in tracker.iterdir() if d.is_dir() and (d.name == tid or tid.startswith(d.name[:4]))
    )


# ── coexistence (the grumpy-site-beard regression) ──────────────────────────────
def test_plan_review_and_completion_coexist_no_clobber(store: Path) -> None:
    tid = rebar.create_ticket("task", "coexist", repo_root=str(store))
    _sign(store, tid, "plan-review", material="m")
    _sign(store, tid, "completion-verifier")  # signing this must NOT destroy plan-review
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert set(state["attestations"]) == {"plan-review", "completion-verifier"}
    for kind in ("plan-review", "completion-verifier"):
        v = signing.verify_signature(tid, kind=kind, repo_root=str(store))
        assert v["verdict"] == "certified"


def test_resign_replaces_within_kind_only(store: Path) -> None:
    tid = rebar.create_ticket("task", "resign", repo_root=str(store))
    _sign(store, tid, "plan-review", material="m1")
    _sign(store, tid, "completion-verifier")
    _sign(store, tid, "plan-review", material="m2")  # re-sign plan-review
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert "material: m2" in state["attestations"]["plan-review"]["manifest"]  # replaced
    assert "completion-verifier" in state["attestations"]  # untouched


# ── compaction round-trip + cross-version snapshot mirror ───────────────────────
def _snapshot_compiled_state(store: Path, tid: str):
    tdir = _tdir(store, tid)
    snap = None
    for f in tdir.glob("*.json"):
        ev = json.loads(f.read_text())
        if ev.get("event_type") == "SNAPSHOT":
            snap = ev["data"]["compiled_state"]
    assert snap is not None, "no SNAPSHOT event written"
    return snap


def test_compaction_drops_legacy_mirror_by_default(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Contract phase (352b): a new SNAPSHOT carries only the kind-keyed attestations map;
    # the legacy `signature` mirror is dropped by default.
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")  # 3 events > 1 → force a SNAPSHOT
    tid = rebar.create_ticket("task", "compact", repo_root=str(store))
    _sign(store, tid, "plan-review", material="m")
    _sign(store, tid, "completion-verifier")
    rebar.compact(tid, repo_root=str(store))
    state = rebar.show_ticket(tid, repo_root=str(store))
    # Both kinds survive in the authoritative map through compaction...
    assert set(state["attestations"]) == {"plan-review", "completion-verifier"}

    snap = _snapshot_compiled_state(store, tid)
    # ...but the SNAPSHOT no longer carries the legacy single-slot mirror.
    assert "attestations" in snap and "signature" not in snap
    assert set(snap["attestations"]) == {"plan-review", "completion-verifier"}


def test_compaction_keeps_mirror_on_rollback(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rollback (352b): set compact.emit_legacy_signature_mirror=true → the SNAPSHOT keeps
    # the legacy `signature` mirror (most-recent attestation of any kind), so a not-yet-
    # upgraded clone reading it still sees one attestation.
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_COMPACT_EMIT_LEGACY_SIGNATURE_MIRROR", "true")
    tid = rebar.create_ticket("task", "compact", repo_root=str(store))
    _sign(store, tid, "plan-review", material="m")
    _sign(store, tid, "completion-verifier")
    rebar.compact(tid, repo_root=str(store))

    snap = _snapshot_compiled_state(store, tid)
    assert "attestations" in snap and "signature" in snap
    assert snap["signature"]["manifest"][0].startswith("completion-verifier")  # most-recent mirror


# ── concurrent / order-independent different-kind signs ─────────────────────────
@pytest.mark.parametrize(
    "order",
    [("plan-review", "completion-verifier"), ("completion-verifier", "plan-review")],
)
def test_different_kind_signs_survive_regardless_of_order(store: Path, order) -> None:
    tid = rebar.create_ticket("task", "order", repo_root=str(store))
    for kind in order:
        _sign(store, tid, kind, material="m" if kind == "plan-review" else None)
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert set(state["attestations"]) == {"plan-review", "completion-verifier"}


# ── completion validity: material edit invalidates, a tag edit does NOT ──────────
def test_completion_validity_material_vs_tag_edit(store: Path) -> None:
    tid = rebar.create_ticket("task", "matedit", description="x" * 60, repo_root=str(store))
    rebar.transition(
        tid, "open", "closed", reason="Fixed: x", force_close="x", repo_root=str(store)
    )
    # Sign a completion attestation binding the CURRENT material fingerprint.
    from rebar.llm.plan_review.attest import current_material_fingerprint

    fp = current_material_fingerprint(tid, repo_root=str(store))
    _sign(store, tid, "completion-verifier", material=fp)
    sig = signing.verify_signature(tid, kind="completion-verifier", repo_root=str(store))
    st = rebar.show_ticket(tid, repo_root=str(store))
    assert compute_validity(sig, st, "completion-verifier", repo_root=str(store))["valid"] is True

    # A tag change is NOT material → still valid.
    rebar.tag(tid, "some-tag", repo_root=str(store))
    st2 = rebar.show_ticket(tid, repo_root=str(store))
    assert compute_validity(sig, st2, "completion-verifier", repo_root=str(store))["valid"] is True

    # A description (material) edit DOES invalidate.
    rebar.edit_ticket(tid, description="y" * 80, repo_root=str(store))
    st3 = rebar.show_ticket(tid, repo_root=str(store))
    res = compute_validity(sig, st3, "completion-verifier", repo_root=str(store))
    assert res["valid"] is False and res["verdict"] == "stale-material"


# ── legacy single-signature snapshot folds into the kind-keyed map on read ───────
def test_legacy_snapshot_signature_folds_into_map(store: Path) -> None:
    tid = rebar.create_ticket("task", "legacy", repo_root=str(store))
    tdir = _tdir(store, tid)
    resolved = tdir.name
    uuids = [json.loads(f.read_text()).get("uuid") for f in tdir.glob("*.json")]
    # Simulate an OLD-clone SNAPSHOT: compiled_state carries only the legacy single `signature`
    # (a plan-review record), with NO `attestations` map; it subsumes the genesis events.
    legacy_state = {
        "ticket_id": resolved,
        "ticket_type": "task",
        "title": "legacy",
        "status": "open",
        "priority": 2,
        "tags": [],
        "signature": {
            "manifest": ["plan-review: PASS", f"ticket: {resolved}"],
            "algorithm": "HMAC-SHA256",
            "signature": "deadbeef",
            "key_id": "k",
            "signed_at": 1,
        },
    }
    append_event(
        resolved,
        "SNAPSHOT",
        {"compiled_state": legacy_state, "source_event_uuids": uuids, "compacted_at": 1},
        tdir.parent,
        repo_root=str(store),
    )
    state = reduce_ticket(str(tdir))
    # Fold-in: the legacy signature is reachable under its manifest-derived kind.
    assert "plan-review" in state.get("attestations", {})
    assert state["attestations"]["plan-review"]["manifest"][0] == "plan-review: PASS"
