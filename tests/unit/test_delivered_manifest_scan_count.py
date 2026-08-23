"""Regression for bug bb3a-c931-47ba-4808: the delivered-children manifest must not pay
a full-store scan per child.

``orchestrator.delivered_children_manifest`` loops a container's children through
``attest.delivered_now`` → ``_attested_delivered`` →
``compute_validity(sig, child, "completion-verifier")`` per closed child; the completion
branch recomputes ``current_material_fingerprint`` (one full-store reduction via
``relation_snapshot.live_material_children`` → ``_reads.list_tickets(parent=…)`` →
``reduce_all_tickets``), a stale fingerprint retries three legacy generations, and the
stale-material explanation enumerates children once more — so one Pass-2 completion
sub-call manifest assembly over N children cost O(5·N) full-store scans. Sibling of bugs
3d57-602f-0b75-48ee (pin health) and a3f5-5c19-faad-417b (completion child gate); the
same shared ``material_child_index`` snapshot collapses them. The contract pinned here:
full-store reductions during manifest assembly are a small constant, independent of
child count."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import rebar
from rebar.llm.plan_review.attest import current_material_fingerprint
from rebar.llm.plan_review.orchestrator import delivered_children_manifest

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
    epic = rebar.create_ticket("epic", f"Manifest scan subject ({count})", repo_root=repo)
    children = []
    for i in range(count):
        cid = rebar.create_ticket("story", f"Closed child {i}", parent=epic, repo_root=repo)
        rebar.transition(cid, "open", "closed", repo_root=repo)
        children.append(cid)
    return epic, children


def _fake_certified_sig(material_fp: str):
    """A certified completion-verifier attestation pinning ``material_fp`` — routes
    ``_attested_delivered`` into ``compute_validity``'s completion branch (the expensive
    fingerprint path)."""

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
    manifest = delivered_children_manifest(epic, repo_root=repo)
    monkeypatch.undo()
    # Sanity: the stale fingerprint means no child proves delivery (the expensive
    # invalidation path ran per child), so the manifest is empty.
    assert manifest == []
    return calls["n"]


def test_delivered_manifest_scan_count_constant_in_child_count(monkeypatch, repo) -> None:
    """Full-store scans must NOT grow with the number of children (bug bb3a-c931).

    Before the fix each closed child cost 5 scans (1 current fingerprint + 3 legacy
    generations + 1 stale-material explanation), so 4 children cost ~4x what 1 child
    did; the fix answers every child enumeration in the loop from one shared snapshot."""
    scans_one = _scans_for_child_count(monkeypatch, repo, 1)
    scans_four = _scans_for_child_count(monkeypatch, repo, 4)
    assert scans_four == scans_one, (
        f"full-store scans grew with child count: {scans_one} scans for 1 child but "
        f"{scans_four} for 4 children — the delivered-children manifest is rescanning "
        "the store per child"
    )


def test_delivered_manifest_contents_stay_correct(monkeypatch, repo) -> None:
    """Correctness guard for any scan-collapsing fix: a child whose attestation pins its
    TRUE current material fingerprint is delivered (appears in the manifest); a stale
    child is not — the manifest's membership is unchanged."""
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
    manifest = delivered_children_manifest(epic, repo_root=repo)
    assert [m["ticket_id"] for m in manifest] == [children[0]]
