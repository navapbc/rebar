"""Read-set-scoped SHA-drift invalidation for NO-``file_impact`` attestations (ticket 81ca).

ADR 0002 scopes attestation currency to a per-path ``{path: sha256}`` dependency set. With no
declared ``file_impact`` that set was empty and the claim gate degraded to whole-HEAD freshness.
These tests pin the scoping that replaces the degradation, and — more importantly — every path
back to it:

* the six-step ``distinct_fetches`` → read-set normalization protocol;
* the read-set living INSIDE the signed manifest (tamper ⇒ verification failure);
* the no-``file_impact`` dependency set = read-set ∪ cited ∪ expanded blast radius, decided by
  the UNCHANGED ADR-0002 per-path comparison;
* glob handling: expansion catches content drift, the membership digest catches ADDITIONS;
* every fail-safe — no recorded read-set, a declared ``file_impact``, an expansion failure —
  falling back to the pre-change whole-HEAD behavior;
* the reported ``currency-basis``, in the manifest, in ``plan_review_status``, and on the CLI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.llm.plan_review import attest, read_set
from rebar.llm.plan_review.manifest import (
    CURRENCY_BASIS_FAIL_SAFE,
    CURRENCY_BASIS_FILE_IMPACT,
    CURRENCY_BASIS_READ_SET,
)


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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-81ca")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tree(root: Path) -> Path:
    """A small tree with one reviewer rubric and one gate workflow under the blast radius."""
    (root / "src/rebar/llm/reviewers").mkdir(parents=True, exist_ok=True)
    (root / "src/rebar/llm/workflow/gates").mkdir(parents=True, exist_ok=True)
    (root / "src/rebar/llm/reviewers/plan_review_a.md").write_text("rubric a", encoding="utf-8")
    (root / "src/rebar/llm/workflow/gates/plan-review.yaml").write_text(
        "steps: []", encoding="utf-8"
    )
    (root / "read_me.py").write_text("x = 1\n", encoding="utf-8")
    (root / "cited.py").write_text("y = 2\n", encoding="utf-8")
    return root


# ── (1) the read-set normalization protocol ─────────────────────────────────────────
def test_read_set_normalization_keeps_only_read_file_targets(tmp_path: Path) -> None:
    """Step 1: ``search_files`` targets are QUERIES and ``list_directory`` targets are
    DIRECTORIES; neither is whole-file hashable, so neither enters the read-set."""
    root = _tree(tmp_path)
    fetches = [
        {"tool": "read_file", "target": "read_me.py"},
        {"tool": "search_files", "target": "def compute_validity"},
        {"tool": "list_directory", "target": "src"},
    ]
    assert read_set.normalize_read_set(fetches, base=str(root)) == ["read_me.py"]


def test_read_set_normalization_dedupes_spellings_and_sorts(tmp_path: Path) -> None:
    """Steps 4 and 6: ``./x`` and ``x`` are ONE file, and the output is sorted so the signed
    manifest is reproducible regardless of the order the agent happened to read in."""
    root = _tree(tmp_path)
    fetches = [
        {"tool": "read_file", "target": "./read_me.py"},
        {"tool": "read_file", "target": "read_me.py"},
        {"tool": "read_file", "target": "cited.py"},
    ]
    assert read_set.normalize_read_set(fetches, base=str(root)) == ["cited.py", "read_me.py"]


def test_read_set_normalization_drops_outside_repo_and_nonexistent(tmp_path: Path) -> None:
    """Steps 3 and 5: a ``../`` escape, an absolute path outside the root, and a path that
    names no existing regular file are all dropped rather than baked in as ``absent`` — an
    absent entry would let an unrelated later file creation invalidate the attestation."""
    root = _tree(tmp_path / "root")
    outside = tmp_path / "outside.py"
    outside.write_text("z = 3\n", encoding="utf-8")
    fetches = [
        {"tool": "read_file", "target": "../outside.py"},
        {"tool": "read_file", "target": str(outside)},
        {"tool": "read_file", "target": "never_written.py"},
        {"tool": "read_file", "target": "src"},  # a directory, not a regular file
        {"tool": "read_file", "target": "read_me.py"},
    ]
    assert read_set.normalize_read_set(fetches, base=str(root)) == ["read_me.py"]


def test_read_set_normalization_tolerates_malformed_entries(tmp_path: Path) -> None:
    """Telemetry is untrusted input: a non-dict, a missing target, and an empty target are
    skipped rather than raising inside the gate."""
    root = _tree(tmp_path)
    fetches = ["nonsense", {"tool": "read_file"}, {"tool": "read_file", "target": ""}, None]
    assert read_set.normalize_read_set(fetches, base=str(root)) == []


# ── (2) the read-set inside the SIGNED manifest ─────────────────────────────────────
def test_signed_manifest_records_read_set_and_verifies(store: Path) -> None:
    ticket_id = rebar.create_ticket("task", "read-set recorded", repo_root=str(store))
    manifest = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material="m",
        read_set=["b.py", "a.py", "a.py"],
        currency_basis=CURRENCY_BASIS_READ_SET,
    )
    signing.sign_manifest(ticket_id, manifest, kind="plan-review", repo_root=str(store))
    verified = signing.verify_signature(ticket_id, kind="plan-review", repo_root=str(store))

    assert verified["verdict"] == "certified"
    assert attest.manifest_read_set(verified["manifest"]) == ["a.py", "b.py"]
    assert attest.manifest_currency_basis(verified["manifest"]) == CURRENCY_BASIS_READ_SET


def test_modified_read_set_does_not_survive_verification(store: Path) -> None:
    """The read-set is INSIDE the signed material, not a plaintext mirror. Two properties
    make a tampered read-set worthless: the signed payload carries the read paths, and the
    authoritative reader every currency decision uses sources them from that payload — so
    rewriting the plaintext copy changes nothing the gate looks at."""
    ticket_id = rebar.create_ticket("task", "read-set tamper", repo_root=str(store))
    manifest = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material="m",
        read_set=["a.py"],
        currency_basis=CURRENCY_BASIS_READ_SET,
    )
    signing.sign_manifest(ticket_id, manifest, kind="plan-review", repo_root=str(store))
    verified = signing.verify_signature(ticket_id, kind="plan-review", repo_root=str(store))
    assert verified["verdict"] == "certified"
    assert "read-path: a.py" in (verified["signed_manifest"] or verified["manifest"])

    tampered = dict(verified)
    tampered["manifest"] = [
        "read-path: attacker_controlled.py" if line == "read-path: a.py" else line
        for line in verified["manifest"]
    ]

    assert attest.manifest_read_set(attest._authoritative_manifest(tampered)) == ["a.py"]


def test_unverified_record_never_yields_a_scope(store: Path) -> None:
    """And when verification itself fails, the record is refused outright rather than being
    mined for whatever read-set it claims (the fail-safe)."""
    result = attest.compute_validity(
        {
            "verified": False,
            "manifest": ["plan-review: PASS", "read-set: 1", "read-path: attacker.py"],
        },
        {"ticket_id": "t", "status": "open"},
        "plan-review",
        repo_root=str(store),
    )
    assert result["valid"] is False
    assert result["verdict"] == "unsigned"


def test_absent_read_set_line_is_distinguishable_from_an_empty_one() -> None:
    """``None`` (nothing recorded — the fail-safe still governs) and ``[]`` (the review
    verifiably read nothing) are different states and must not collapse."""
    recorded_empty = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": "t"}, material="m", read_set=[]
    )
    nothing = attest.build_manifest({"verdict": "PASS", "ticket_id": "t"}, material="m")
    assert attest.manifest_read_set(recorded_empty) == []
    assert attest.manifest_read_set(nothing) is None


# ── (3) the no-file_impact dependency set ───────────────────────────────────────────
def _verdict(ticket_id: str, *, recorded: bool, paths: list[str]) -> dict:
    coverage: dict = {"counts": {}}
    if recorded:
        coverage["read_set"] = paths
        coverage["read_set_recorded"] = True
    return {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "coverage": coverage,
        "blocking": [
            {"citations": [{"kind": "file", "path": "cited.py"}]},
        ],
    }


def test_no_file_impact_scopes_to_read_set_cited_and_blast_radius(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(store)
    monkeypatch.setattr(attest, "_hash_basis", lambda *a, **k: str(store))
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))
    ticket_id = rebar.create_ticket("task", "no impact", repo_root=str(store))
    verdict = _verdict(ticket_id, recorded=True, paths=["read_me.py"])

    deps = attest.dependency_hashes(verdict, repo_root=str(store))

    assert "read_me.py" in deps, "the read-set entered the dependency set"
    assert "cited.py" in deps, "cited paths are still part of ADR 0002's set"
    assert "src/rebar/llm/reviewers/*.md" in deps, "the glob is recorded as a membership entry"
    assert "src/rebar/llm/reviewers/plan_review_a.md" in deps, "and expanded to its members"
    assert verdict["coverage"]["currency_basis"] == CURRENCY_BASIS_READ_SET


def test_declared_file_impact_keeps_the_pre_change_dependency_set(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticket that DOES declare ``file_impact`` is untouched: the read-set is recorded for
    observability but contributes no dependency entry and no blast radius."""
    _tree(store)
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))
    ticket_id = rebar.create_ticket("task", "declared", repo_root=str(store))
    rebar.set_file_impact(
        ticket_id, [{"path": "cited.py", "reason": "declared"}], repo_root=str(store)
    )
    verdict = _verdict(ticket_id, recorded=True, paths=["read_me.py"])

    deps = attest.dependency_hashes(verdict, repo_root=str(store))

    assert set(deps) == {"cited.py"}
    assert verdict["coverage"]["currency_basis"] == CURRENCY_BASIS_FILE_IMPACT


def test_scoped_set_stays_current_until_a_dependency_moves(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both directions through the UNCHANGED ADR-0002 comparison: an unrelated commit leaves
    the scoped attestation current; touching a read path invalidates it."""
    _tree(store)
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))
    ticket_id = rebar.create_ticket("task", "scoped drift", repo_root=str(store))
    deps = attest.dependency_hashes(
        _verdict(ticket_id, recorded=True, paths=["read_me.py"]), repo_root=str(store)
    )

    (store / "unrelated.py").write_text("nothing to do with the review\n", encoding="utf-8")
    assert attest._rehash(deps.keys(), repo_root=str(store)) == deps

    (store / "read_me.py").write_text("x = 999\n", encoding="utf-8")
    assert attest._rehash(deps.keys(), repo_root=str(store)) != deps


# ── (4) blast-radius globs: expansion + membership digest ───────────────────────────
def test_blast_radius_membership_digest_catches_an_added_file(tmp_path: Path) -> None:
    """The addition blind spot a purely per-path expansion leaves: a rubric added AFTER
    signing has no baked per-file hash, but it moves the glob's membership digest."""
    root = _tree(tmp_path)
    pattern = "src/rebar/llm/reviewers/*.md"
    before = read_set.hash_dep_entry(pattern, base=str(root))

    (root / "src/rebar/llm/reviewers/plan_review_z.md").write_text("new", encoding="utf-8")

    assert read_set.hash_dep_entry(pattern, base=str(root)) != before


def test_blast_radius_membership_digest_catches_a_deleted_file(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    pattern = "src/rebar/llm/reviewers/*.md"
    before = read_set.hash_dep_entry(pattern, base=str(root))

    (root / "src/rebar/llm/reviewers/plan_review_a.md").unlink()

    assert read_set.hash_dep_entry(pattern, base=str(root)) != before


def test_blast_radius_membership_digest_ignores_member_content(tmp_path: Path) -> None:
    """Membership is MEMBERSHIP: editing a member's bytes leaves the digest alone (its own
    expanded ``dep`` entry catches that), so the two mechanisms stay complementary rather
    than redundant."""
    root = _tree(tmp_path)
    pattern = "src/rebar/llm/reviewers/*.md"
    before = read_set.hash_dep_entry(pattern, base=str(root))

    (root / "src/rebar/llm/reviewers/plan_review_a.md").write_text("edited", encoding="utf-8")

    assert read_set.hash_dep_entry(pattern, base=str(root)) == before


def test_plain_path_entries_hash_exactly_as_before(tmp_path: Path) -> None:
    """The shared dispatcher must not change a single pre-existing manifest: a non-glob entry
    delegates to the unchanged whole-file hash, and a missing path is still ``absent``."""
    from rebar.llm.plan_review.manifest import _ABSENT_HASH, _hash_file

    root = _tree(tmp_path)
    assert read_set.hash_dep_entry("read_me.py", base=str(root)) == _hash_file(
        "read_me.py", base=str(root)
    )
    assert read_set.hash_dep_entry("gone.py", base=str(root)) == _ABSENT_HASH


# ── (5) fail-safes ──────────────────────────────────────────────────────────────────
def test_fallback_when_no_read_set_was_recorded(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No agentic pass ran (or telemetry collection failed): the dependency set collapses to
    the pre-change citations-only set and the basis reports the composition honestly."""
    _tree(store)
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))
    ticket_id = rebar.create_ticket("task", "unrecorded", repo_root=str(store))
    verdict = _verdict(ticket_id, recorded=False, paths=[])

    deps = attest.dependency_hashes(verdict, repo_root=str(store))

    assert set(deps) == {"cited.py"}
    assert not any(read_set.is_glob(path) for path in deps)


def test_fallback_to_whole_head_when_nothing_is_declared_cited_or_read(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-dependency-set fail-safe (ADR 0002 decision 5) is intact for a pre-81ca
    attestation: no citations, no declared impact, no recorded read-set ⇒ no scoping."""
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))
    ticket_id = rebar.create_ticket("task", "nothing", repo_root=str(store))
    verdict = {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}}

    assert attest.dependency_hashes(verdict, repo_root=str(store)) == {}
    assert verdict["coverage"]["currency_basis"] == CURRENCY_BASIS_FAIL_SAFE


def test_fallback_when_blast_radius_expansion_raises(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise anywhere in the scoping must never scope: it is caught, logged, and the set
    degrades to the pre-change citations-only composition."""
    _tree(store)
    monkeypatch.setattr("rebar.llm.plan_review.manifest._hash_basis", lambda *a, **k: str(store))

    def boom(*_args, **_kwargs):
        raise OSError("snapshot vanished")

    monkeypatch.setattr(read_set, "read_set_dependency_paths", boom)
    ticket_id = rebar.create_ticket("task", "expansion boom", repo_root=str(store))
    verdict = _verdict(ticket_id, recorded=True, paths=["read_me.py"])

    deps = attest.dependency_hashes(verdict, repo_root=str(store))

    assert set(deps) == {"cited.py"}
    assert verdict["coverage"]["currency_basis"] == CURRENCY_BASIS_FILE_IMPACT


def test_fallback_unverifiable_attestation_reports_unsigned(store: Path) -> None:
    """A read-set that cannot be authenticated is worth nothing: ``compute_validity`` refuses
    the record outright rather than trusting its scope."""
    result = attest.compute_validity(
        {"verified": False, "manifest": ["plan-review: PASS", "read-set: 1", "read-path: a.py"]},
        {"ticket_id": "t", "status": "open"},
        "plan-review",
        repo_root=str(store),
    )
    assert result["valid"] is False
    assert result["verdict"] == "unsigned"


# ── (6) the reported currency basis ─────────────────────────────────────────────────
def test_currency_basis_is_derived_for_pre_change_manifests() -> None:
    """A manifest signed before ticket 81ca carries no basis line, so it is derived
    conservatively rather than reported as unknown."""
    scoped = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": "t"}, material="m", deps={"a.py": "h"}
    )
    unscoped = attest.build_manifest({"verdict": "PASS", "ticket_id": "t"}, material="m")
    assert attest.manifest_currency_basis(scoped) == CURRENCY_BASIS_FILE_IMPACT
    assert attest.manifest_currency_basis(unscoped) == CURRENCY_BASIS_FAIL_SAFE


def test_currency_basis_surfaces_in_plan_review_status(store: Path) -> None:
    from rebar.llm.plan_review.attest_gate import plan_review_status

    ticket_id = rebar.create_ticket("task", "status basis", repo_root=str(store))
    manifest = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material="m",
        read_set=["read_me.py"],
        currency_basis=CURRENCY_BASIS_READ_SET,
    )
    signing.sign_manifest(ticket_id, manifest, kind="plan-review", repo_root=str(store))

    status = plan_review_status(ticket_id, repo_root=str(store))

    assert status["currency_basis"] == CURRENCY_BASIS_READ_SET


def test_currency_basis_is_none_without_a_readable_attestation(store: Path) -> None:
    from rebar.llm.plan_review.attest_gate import plan_review_status

    ticket_id = rebar.create_ticket("task", "no attestation", repo_root=str(store))

    assert plan_review_status(ticket_id, repo_root=str(store))["currency_basis"] is None


def test_currency_basis_is_printed_by_the_status_cli(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._cli import _llm_commands

    monkeypatch.setattr(_llm_commands, "ensure_initialized", lambda **_kwargs: None)
    monkeypatch.setattr(
        "rebar.llm.plan_review_status",
        lambda *_a, **_k: {
            "ok": True,
            "verdict": "certified",
            "reason": "certified plan-review attestation",
            "verified_at_sha": "abc123",
            "signed_at": 1,
            "currency_basis": CURRENCY_BASIS_READ_SET,
        },
    )

    assert _llm_commands._review_plan(["t", "--status", "-o", "text"]) == 0
    assert f"currency-basis={CURRENCY_BASIS_READ_SET}" in capsys.readouterr().out


def test_currency_basis_is_carried_in_status_json(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from rebar._cli import _llm_commands

    monkeypatch.setattr(_llm_commands, "ensure_initialized", lambda **_kwargs: None)
    monkeypatch.setattr(
        "rebar.llm.plan_review_status",
        lambda *_a, **_k: {
            "ok": False,
            "verdict": "stale-code",
            "reason": "drifted",
            "verified_at_sha": None,
            "signed_at": None,
            "currency_basis": CURRENCY_BASIS_FAIL_SAFE,
        },
    )

    assert _llm_commands._review_plan(["t", "--status", "-o", "json"]) == 12
    assert json.loads(capsys.readouterr().out)["currency_basis"] == CURRENCY_BASIS_FAIL_SAFE
