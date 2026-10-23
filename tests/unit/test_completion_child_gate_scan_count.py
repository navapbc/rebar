"""Keep completion child-closure scans constant as child count grows.

Per-child validity can recompute material fingerprints and legacy variants.
The shared ``material_child_index`` snapshot must prevent full-store
reductions from scaling with the number of closed children."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import rebar
from rebar.llm.completion_child_gate import child_closure_findings
from rebar.llm.plan_review.attest import current_material_fingerprint

STALE_FP = "0" * 16  # syntactically valid, matches nothing → forces the legacy retries


@pytest.fixture
def repo(tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    rebar.init_repo(repo_root=str(root))
    return str(root)


def _make_closed_children(repo: str, count: int) -> tuple[str, list[str]]:
    epic = rebar.create_ticket("epic", f"Scan-count subject ({count})", repo_root=repo)
    children = []
    for i in range(count):
        cid = rebar.create_ticket("story", f"Closed child {i}", parent=epic, repo_root=repo)
        rebar.transition(cid, "open", "closed", repo_root=repo)
        children.append(cid)
    return epic, children


def _fake_certified_sig(material_fp: str):
    """A certified completion-verifier attestation pinning ``material_fp`` — routes the
    gate into ``compute_validity``'s completion branch (the expensive fingerprint path)."""

    def fake(cid, kind=None, repo_root=None):
        return {
            "verdict": "certified",
            "manifest": [f"material: {material_fp}"],
            "signed_at": time.time_ns(),
        }

    return fake


def _count_scans(monkeypatch) -> dict[str, int]:
    """Count full-store reductions through the one seam every list-style read uses."""
    from rebar._engine_support import reads

    calls = {"n": 0}
    real = reads.reduce_all_tickets

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(reads, "reduce_all_tickets", counting)
    return calls


def _scans_for_child_count(monkeypatch, repo: str, count: int) -> int:
    epic, _children = _make_closed_children(repo, count)
    monkeypatch.setattr(rebar, "verify_signature", _fake_certified_sig(STALE_FP))
    calls = _count_scans(monkeypatch)
    blocking, uncertified = child_closure_findings(epic, repo)
    monkeypatch.undo()
    # Sanity: every child was actually validity-checked (the expensive path ran).
    assert blocking == []
    assert len(uncertified) == count
    return calls["n"]


def test_child_gate_scan_count_constant_in_child_count(monkeypatch, repo) -> None:
    """Four children must use the same full-store scan count as one child."""
    scans_one = _scans_for_child_count(monkeypatch, repo, 1)
    scans_four = _scans_for_child_count(monkeypatch, repo, 4)
    assert scans_four == scans_one, (
        f"full-store scans grew with child count: {scans_one} scans for 1 child but "
        f"{scans_four} for 4 children — the child-closure gate is rescanning the store per child"
    )


def test_child_gate_verdicts_stay_correct(monkeypatch, repo) -> None:
    """Correctness guard for any scan-collapsing fix: a child whose attestation pins its
    TRUE current material fingerprint stays certified (NOT uncertified), while a stale
    pin still lands in ``uncertified`` — the gate's verdict sets are unchanged."""
    epic, children = _make_closed_children(repo, 2)
    true_fp = current_material_fingerprint(children[0], repo_root=repo)
    assert isinstance(true_fp, str) and len(true_fp) == 16

    def fake(cid, kind=None, repo_root=None):
        fp = true_fp if cid == children[0] else STALE_FP
        return {
            "verdict": "certified",
            "manifest": [f"material: {fp}"],
            "signed_at": time.time_ns(),
        }

    monkeypatch.setattr(rebar, "verify_signature", fake)
    blocking, uncertified = child_closure_findings(epic, repo)
    assert blocking == []
    unc_ids = [str(f.get("criterion", "")).split()[2] for f in uncertified]
    assert unc_ids == [children[1]]


def test_child_gate_unclosed_child_still_blocks(monkeypatch, repo) -> None:
    """The blocking set is untouched by the scan collapse: an unclosed direct child
    still blocks the close deterministically."""
    epic = rebar.create_ticket("epic", "Blocking subject", repo_root=repo)
    open_child = rebar.create_ticket("story", "Still open", parent=epic, repo_root=repo)
    blocking, uncertified = child_closure_findings(epic, repo)
    assert uncertified == []
    assert len(blocking) == 1
    assert open_child in str(blocking[0].get("criterion", ""))
