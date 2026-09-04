"""S4b — claim-gate <-> plan-review ref-resolution coherence (epic raze-vet-ditch,
amended by bug 72d9 ``athletic-esthetical-polecat``).

Plan-review hashes its file_impact dependency map AT the review's pinned snapshot; the
claim-gate freshness re-check hashes those paths at the CURRENT gate ref. Both resolve
through the ONE shared boundary (`attest._hash_basis`) with the same semantics — a
committed snapshot under the same gate configuration — which is what prevents the
staleness false-positive ADR 0002 exists for (a mere working-tree edit, or an unrelated
commit, must not invalidate). They must NOT resolve to the identical tree: re-hashing at
the signature's own ``verified_at_sha`` compared the manifest against the very tree it
was generated from, which always matched, so scoped drift was structurally undetectable
in attested mode (bug 72d9). The back-out (a configured ``source=local`` gate) falls
back to the working-tree basis on both sides.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar
import rebar.llm
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
    else:
        # Simulate a PRE-S4B (legacy) attestation: working-tree dependency hashes, no
        # verified-at-sha pin. Minted BELOW the policy seam (build_manifest +
        # signing.sign_manifest directly) because sign_plan_review's no-null-pin invariant
        # (bug 5128-0856) now refuses to create these — but stores still hold them, and the
        # verify side must keep reading them.
        from rebar import signing
        from rebar.llm.plan_review import relation_snapshot as _rs
        from rebar.llm.plan_review.manifest import build_manifest, registry_version

        snapshot = _rs.collect_plan_relation_snapshot(tid, repo_root=str(repo))
        manifest = build_manifest(
            verdict,
            material=material,
            deps=attest.dependency_hashes(verdict, repo_root=str(repo)),
            regver=registry_version(str(repo)),
            pins=snapshot.related_material,
        )
        signing.sign_manifest(tid, manifest, kind="plan-review", repo_root=str(repo))


# --------------------------------------------------------------------------------------
# AC1/AC2 — both sides hash committed snapshots (guards working-tree divergence), while a
# committed change to a signed dependency IS visible (guards the 72d9 tautology)
# --------------------------------------------------------------------------------------
def test_attested_claim_gate_ignores_uncommitted_working_tree_drift(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=True)

    # Drift the WORKING TREE only (the current gate ref still says v1).
    (repo / "dep.py").write_text("DRIFTED = 999\n")

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    # The claim gate re-hashes at the CURRENT gate ref's snapshot — a mere working-tree
    # edit moves neither side, so no false stale-code. This is the S4b property we keep.
    assert chk["verdict"] != "stale-code", chk
    assert chk["ok"] is True, chk


def test_attested_claim_gate_sees_committed_drift_in_signed_dependency(tmp_path, monkeypatch):
    """The 72d9 regression pin: signed hashes stay at the review's pinned SHA, the gate
    re-hashes at the current ref — so a LANDED change to a reviewed file invalidates."""
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=True)

    (repo / "dep.py").write_text("DRIFTED = 999\n")
    _git(repo, "add", "dep.py")
    _git(repo, "commit", "-q", "-m", "dep v2")  # the attested basis (ref=HEAD) moved

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    assert chk["verdict"] == "stale-code", chk
    assert chk["ok"] is False, chk


def test_attested_claim_gate_ignores_unrelated_committed_change(tmp_path, monkeypatch):
    """The worm-folly-barge property scoping exists for: an unrelated commit moves the
    gate ref but not the signed dependency's content — the attestation stays valid."""
    repo = _repo(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=True)

    (repo / "unrelated.py").write_text("OTHER = 1\n")
    _git(repo, "add", "unrelated.py")
    _git(repo, "commit", "-q", "-m", "unrelated")

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
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
# AC4 — back-out: a configured local gate hashes the working tree on BOTH sides
# --------------------------------------------------------------------------------------
def test_local_gate_config_uses_working_tree_basis_on_both_sides(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")  # the documented back-out
    tid = rebar.create_ticket("task", "coherence", repo_root=str(repo))
    _sign(repo, tid, attested=False)  # local: working-tree basis, no verified-at-sha pin
    from rebar import signing

    v = rebar.verify_signature(tid, repo_root=str(repo))
    assert signing.verified_at_sha_from_manifest(v["manifest"]) is None
    # A local-configured gate re-hashes the working tree (pre-S4b behavior), so a
    # working-tree drift DOES register as stale-code (coherent: both sides used the tree).
    (repo / "dep.py").write_text("DRIFTED = 999\n")
    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    assert chk["verdict"] == "stale-code", chk


# --------------------------------------------------------------------------------------
# the shared boundary resolves the three bases (single source of truth)
# --------------------------------------------------------------------------------------
def test_hash_basis_resolution(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    from rebar.llm.gate_context import use_code_root

    # default → the working tree (checkout)
    assert attest._hash_basis(str(repo)) == str(rebar.config.repo_root(str(repo)))
    # active attested code root → that snapshot
    with use_code_root("/some/snap"):
        assert attest._hash_basis(str(repo)) == "/some/snap"
    # at_current_gate_ref + attested config → the materialized snapshot at the gate ref
    main_sha = _git(repo, "rev-parse", "origin/main")
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    basis = attest._hash_basis(str(repo), at_current_gate_ref=True)
    assert basis.endswith(main_sha)
    assert (Path(basis) / "dep.py").read_text() == "ORIGINAL = 1\n"
    # at_current_gate_ref + local config → the working tree (the documented back-out)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    basis = attest._hash_basis(str(repo), at_current_gate_ref=True)
    assert basis == str(rebar.config.repo_root(str(repo)))
    # at_current_gate_ref + an unresolvable ref → degrade to the working tree (never crash)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "no-such-ref")
    basis = attest._hash_basis(str(repo), at_current_gate_ref=True)
    assert basis == str(rebar.config.repo_root(str(repo)))


# --------------------------------------------------------------------------------------
# bug 505d-b2c5-734f-47d9 — a DEGRADED basis must not certify (the claim gate is
# fail-CLOSED: docs/plan-review-gate.md "| Claim gate | fail-closed |")
# --------------------------------------------------------------------------------------
def test_attested_claim_gate_refuses_to_certify_when_the_gate_ref_is_unresolvable(
    tmp_path, monkeypatch
):
    """When the configured ATTESTED basis cannot be obtained, ``_hash_basis`` substitutes the
    in-place working tree. If that tree happens to still hold the REVIEWED content while the
    gate ref has moved, the drift comparison sees nothing and the gate certifies a stale
    attestation — turning the fail-closed claim gate fail-OPEN. The substitution must instead
    be visible to the verdict and block the claim."""
    repo = _repo(tmp_path, monkeypatch)
    # Pin the PRODUCTION gate ref (the suite default is `HEAD`); `_sign` already reviews at
    # origin/main, so both sides agree — this is the deployed configuration.
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    tid = rebar.create_ticket("task", "degraded basis", repo_root=str(repo))
    _sign(repo, tid, attested=True)

    # The signed dependency drifts and LANDS on the gate ref …
    (repo / "dep.py").write_text("DRIFTED = 999\n")
    _git(repo, "add", "dep.py")
    _git(repo, "commit", "-q", "-m", "dep v2")
    _git(repo, "push", "-q", "origin", "main")
    # … but this checkout is rolled back to the REVIEWED content, so a working-tree basis
    # sees no drift whatsoever. CONTROL: while the ref resolves, the gate correctly blocks.
    _git(repo, "reset", "--hard", "-q", "HEAD~1")
    assert attest.claim_gate_check(tid, repo_root=str(repo))["verdict"] == "stale-code"

    # Now make the configured gate ref unresolvable — the exact shape of the review-bot's
    # clone (`git init` + fetch a patchset ref; no `origin` remote at all).
    _git(repo, "remote", "remove", "origin")
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", "refs/remotes/origin/main"], check=True
    )

    # PRECONDITION PROOF (anti-vacuity): the substitution really fired, and the substituted
    # basis genuinely holds the reviewed bytes — so an unguarded comparison finds NO drift.
    basis = attest._hash_basis(str(repo), at_current_gate_ref=True)
    assert basis == str(rebar.config.repo_root(str(repo))), "the working-tree substitution fired"
    assert (Path(basis) / "dep.py").read_text() == "ORIGINAL = 1\n"

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    assert chk["ok"] is False, chk
    assert chk["verdict"] == "stale-code", chk
    # and the reason names the basis it could not obtain, not a false "the files drifted"
    assert "origin/main" in chk["reason"], chk


def test_pre_s4b_attestation_still_verifies_when_the_gate_ref_is_unresolvable(
    tmp_path, monkeypatch
):
    """The NEGATIVE half of the degraded-basis guard (bug 505d-b2c5-734f-47d9): it must fire
    ONLY for an attestation whose hashes were produced against a committed snapshot.

    A pre-S4b attestation carries no ``verified_at_sha`` — its hashes were always computed
    against the working tree, so hashing the working tree is its CORRECT basis, not a
    substitution. Failing closed here would break every legacy attestation: a fail-closed
    regression hiding inside a fail-open fix. Paired with
    ``test_attested_claim_gate_refuses_to_certify_when_the_gate_ref_is_unresolvable``, which
    proves the guard DOES fire when the pin is present — one test alone cannot tell "fails
    closed on a degraded basis" from "fails closed on everything"."""
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    tid = rebar.create_ticket("task", "legacy tolerance", repo_root=str(repo))
    _sign(repo, tid, attested=False)  # pre-S4b: working-tree hashes, no verified-at-sha pin

    # The SAME unresolvable gate ref as the positive test — using a RESOLVABLE one here would
    # exercise the wrong arm entirely and pass vacuously.
    _git(repo, "remote", "remove", "origin")
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", "refs/remotes/origin/main"], check=True
    )

    # PRECONDITION PROOFS (anti-vacuity): the guard's degraded-basis conjunct really IS
    # satisfied, so only the legacy-tolerance conjunct can be holding the gate open.
    from rebar import signing
    from rebar.llm.plan_review.manifest import gate_ref_hash_basis

    signed = rebar.verify_signature(tid, repo_root=str(repo))
    assert signing.verified_at_sha_from_manifest(signed["manifest"]) is None, "legacy shape"
    basis = gate_ref_hash_basis(str(repo))
    assert basis.degraded is True, "the degraded-basis conjunct is satisfied"
    assert basis.path == str(rebar.config.repo_root(str(repo)))

    chk = attest.claim_gate_check(tid, repo_root=str(repo))
    assert chk["ok"] is True, chk
    assert chk["verdict"] == "certified", chk
