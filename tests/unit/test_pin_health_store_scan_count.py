"""Regression for bug 3d57-602f-0b75-48ee: plan-material pin health must not pay a
full-store scan per pin.

``derive_plan_material_pin_health`` fingerprints every pinned target, and each
fingerprint enumerates the target's children via
``relation_snapshot.live_material_children`` → ``_reads.list_tickets(parent=…)`` →
``reads.list_states`` → ``reduce_all_tickets`` — a FULL-store reduction. A stale pin
retries up to three legacy fingerprint generations, so a container with N stale pins
cost O(4·N) full-store scans; on the production store (5 247 tickets, 23 pins) one
``rebar show`` burned 466 983 single-ticket reduces (~745 s of CPU) while
``rebar list --parent`` — a larger payload — did one scan. The contract pinned here:
the number of full-store reductions is a small constant, independent of pin count."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm.plan_review import attest
from rebar.llm.plan_review.attest import compute_validity, derive_plan_material_pin_health
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin

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


def _make_children(repo: str, count: int) -> tuple[str, list[str]]:
    epic = rebar.create_ticket("epic", "Scan-count subject", repo_root=repo)
    children = [
        rebar.create_ticket("story", f"Pinned child {i}", parent=epic, repo_root=repo)
        for i in range(count)
    ]
    return epic, children


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


def _scans_for_pin_count(monkeypatch, repo: str, children: list[str], pins: int) -> int:
    records = [PlanMaterialPin("child", cid, STALE_FP) for cid in children[:pins]]
    calls = _count_scans(monkeypatch)
    health = derive_plan_material_pin_health(records, repo_root=repo, enforced=True)
    monkeypatch.undo()
    # Sanity: every stale pin was actually fingerprinted (the expensive path ran).
    assert [t["pin_status"] for t in health["targets"]] == ["stale-pin-drift"] * pins
    return calls["n"]


def test_pin_health_scan_count_constant_in_pin_count(monkeypatch, repo) -> None:
    """Full-store scans must NOT grow with the number of pins (bug 3d57).

    Before the fix each stale pin cost 4 scans (1 current + 3 legacy generations),
    so 4 pins cost 4x what 1 pin did; the fix answers every child enumeration in
    the derivation from one shared snapshot."""
    _, children = _make_children(repo, 4)
    scans_one = _scans_for_pin_count(monkeypatch, repo, children, 1)
    scans_four = _scans_for_pin_count(monkeypatch, repo, children, 4)
    assert scans_four == scans_one, (
        f"full-store scans grew with pin count: {scans_one} scans for 1 pin but "
        f"{scans_four} for 4 pins — pin-health derivation is rescanning the store per pin"
    )


def test_compute_validity_scan_count_constant_in_pin_count(monkeypatch, repo) -> None:
    """The same bound must hold through the public validity-on-read entry point
    (what `rebar show` reaches via plan_review_health)."""
    epic, children = _make_children(repo, 4)
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    state = {"ticket_id": epic, "status": "in_progress"}

    def scans(pins: int) -> int:
        manifest = ["plan-review: PASS", "material: " + STALE_FP, "regver: rv0"] + [
            f"plan-material-pin: child {cid} {STALE_FP}" for cid in children[:pins]
        ]
        att = {"manifest": manifest, "head_sha": "headA", "signed_at": 100}
        calls = _count_scans(monkeypatch)
        compute_validity(att, state, "plan-review", repo_root=repo)
        count = calls["n"]
        monkeypatch.undo()
        monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
        monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
        return count

    assert scans(4) == scans(1), "compute_validity full-store scans grew with pin count"


def test_pin_health_fingerprints_stay_correct(repo) -> None:
    """Correctness guard for any scan-collapsing fix: a pin carrying the child's TRUE
    current fingerprint must derive `current`, and the derived current_fingerprint of a
    stale pin must equal the directly-computed one (no stale/wrong child index)."""
    _, children = _make_children(repo, 2)
    true_fps = [attest.current_material_fingerprint(cid, repo_root=repo) for cid in children]
    assert all(isinstance(fp, str) and len(fp) == 16 for fp in true_fps)

    records = [
        PlanMaterialPin("child", children[0], true_fps[0]),  # matching → current
        PlanMaterialPin("child", children[1], STALE_FP),  # non-matching → drift
    ]
    health = derive_plan_material_pin_health(records, repo_root=repo, enforced=True)
    by_id = {t["canonical_id"]: t for t in health["targets"]}
    assert by_id[children[0]]["pin_status"] == "current"
    assert by_id[children[1]]["pin_status"] == "stale-pin-drift"
    assert by_id[children[1]]["current_fingerprint"] == true_fps[1]
