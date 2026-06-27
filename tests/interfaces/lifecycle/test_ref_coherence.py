"""S4b — claim-gate <-> plan-review ref-resolution coherence (epic raze-vet-ditch).

Plan-review hashes its file_impact dependency map AT a ref basis; the claim-gate freshness
re-check must hash those paths at the SAME basis or it re-introduces the staleness
false-positive ADR 0002 prevents. This proves both sides resolve through the ONE shared
boundary (`attest._hash_basis`) at the SAME pinned-SHA — and that the back-out (a local
attestation, no pin) cleanly falls back to the working-tree basis on both sides.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar
import rebar.llm  # noqa: F401
from rebar.llm import gate_source
from rebar.llm.plan_review import attest


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    rebar.init_repo(repo_root=str(repo))
    (repo / "dep.py").write_text("ORIGINAL = 1\n")
    _git(repo, "add", "dep.py")
    _git(repo, "commit", "-q", "-m", "dep v1")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    return repo


def _sign(repo: Path, tid: str, *, attested: bool):
    rebar.set_file_impact(tid, [{"path": "dep.py", "reason": "r"}], repo_root=str(repo))
    material = attest.current_material_fingerprint(tid, repo_root=str(repo))
    verdict = {
        "ticket_id": tid,
        "verdict": "PASS",
        "model": "m",
        "runner": "fake",
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 0}},
    }
    if attested:
        handle = gate_source.resolve_gate_handle("origin/main", "attested", str(repo))
        with gate_source.gate_read_root(handle):
            attest.sign_plan_review(verdict, material=material, repo_root=str(repo))
    else:  # local: no contextvar → working-tree basis, no pin
        attest.sign_plan_review(verdict, material=material, repo_root=str(repo))


# --------------------------------------------------------------------------------------
# AC1/AC2 — both sides hash at the SAME pinned-SHA basis (guards whole-HEAD divergence)
# --------------------------------------------------------------------------------------
def test_attested_claim_gate_hashes_pinned_sha_not_drifted_working_tree(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=True)

    # Drift the WORKING TREE away from origin/main (the pinned snapshot still says v1).
    (repo / "dep.py").write_text("DRIFTED = 999\n")

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    # The claim gate re-hashes at the PINNED snapshot (same basis plan-review signed) — so a
    # mere working-tree drift does NOT produce a false stale-code. Both sides agree.
    assert chk["verdict"] != "stale-code", chk
    assert chk["ok"] is True, chk


def test_attested_pin_recorded_in_signature(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=True)
    from rebar import signing

    v = rebar.verify_signature(tid, repo_root=str(repo))
    main_sha = _git(repo, "rev-parse", "origin/main")
    assert signing.verified_at_sha_from_manifest(v["manifest"]) == main_sha


# --------------------------------------------------------------------------------------
# AC4 — back-out: a local attestation (no pin) hashes the working tree on BOTH sides
# --------------------------------------------------------------------------------------
def test_local_attestation_uses_working_tree_basis_on_both_sides(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=False)  # local: working-tree basis, no verified-at-sha pin
    from rebar import signing

    v = rebar.verify_signature(tid, repo_root=str(repo))
    assert signing.verified_at_sha_from_manifest(v["manifest"]) is None
    # No pin → the claim gate falls back to the working tree (prior per-site behavior). A
    # working-tree drift therefore DOES register as stale-code (coherent: both used the tree).
    (repo / "dep.py").write_text("DRIFTED = 999\n")
    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    assert chk["verdict"] == "stale-code", chk


# --------------------------------------------------------------------------------------
# the shared boundary resolves the three bases (single source of truth)
# --------------------------------------------------------------------------------------
def test_hash_basis_resolution(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    from rebar.llm.config import use_code_root

    # default → the working tree (checkout)
    assert attest._hash_basis(str(repo)) == str(rebar.config.repo_root(str(repo)))
    # active attested code root → that snapshot
    with use_code_root("/some/snap"):
        assert attest._hash_basis(str(repo)) == "/some/snap"
    # pinned_sha → the materialized snapshot at that SHA
    main_sha = _git(repo, "rev-parse", "origin/main")
    basis = attest._hash_basis(str(repo), pinned_sha=main_sha)
    assert basis.endswith(main_sha)
    assert (Path(basis) / "dep.py").read_text() == "ORIGINAL = 1\n"
