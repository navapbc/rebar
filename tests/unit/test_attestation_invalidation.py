"""Overlay-aware registry stamping and its (grandfathered) effect on attestations
(story 08af, epic 3156; amended by ADR 0053).

``registry_version`` is still overlay-aware — activating, re-tuning, or disabling a criterion
rotates the stamp, which still gates drift-refresh REUSE. What changed in ADR 0053 is the claim
gate's response: a rotated stamp is GRANDFATHERED rather than invalidating. These tests pin:

* ``registry_version(repo_root)`` is overlay-aware, but overlay-ABSENT is BYTE-IDENTICAL to the
  packaged ``registry_version()`` (existing certs stay valid — zero churn);
* ``compute_validity`` stays ``valid`` and reports ``registry_drift`` when the overlay changed
  vs the signed regver (and when the regver line is missing entirely), and reports no drift
  when unchanged;
* a ``"disabled": true`` built-in is removed from ``effective_criteria`` + surfaces in
  ``disabled_builtins`` (and ``disabled`` on a ``project.`` id is a located load error);
* ``build_manifest`` emits + ``manifest_disabled_builtins`` parses the ``disabled_builtins:``
  line (absent when empty), and the signed manifest still HMAC-verifies.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._opcert_signing import verify_opcert_record
from rebar.llm.plan_review import attest, registry
from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin
from rebar.llm.prompting import prompt_library

_ROUTING = {
    "exec": "1-TURN",
    "facet": "project-invariants",
    "applies_at": {"scope": ["container", "leaf"]},
    "block_threshold": 0.9,
    "default_posture": "advisory",
    "checklist": [],
}


def _make_repo(tmp_path: Path, *, overlay: dict | None) -> str:
    """A project root with an optional `.rebar/criteria_routing.json` overlay (mirrors the
    test_criteria_overlay.py fixture)."""
    if overlay is not None:
        rebar_dir = tmp_path / ".rebar"
        rebar_dir.mkdir(parents=True, exist_ok=True)
        (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    yield
    prompt_library._invalidate_caches()


# ── (a) overlay-absent parity: existing certs stay valid ─────────────────────────
def test_overlay_absent_registry_version_is_packaged_identical(tmp_path):
    """A repo with NO overlay hashes to EXACTLY the packaged (no-repo) stamp — so an
    attestation signed before this change (packaged regver) still matches at the gate."""
    root = _make_repo(tmp_path, overlay=None)
    assert attest.registry_version(root) == attest.registry_version()
    assert attest.registry_version(root) == attest.registry_version(None)


def test_retune_that_is_a_noop_still_changes_nothing_unexpected(tmp_path):
    """Sanity: an overlay whose only entry re-tunes a built-in to its OWN value still differs
    from packaged only through effective_routing (not spuriously) — here we prove a real change
    (0.5) DOES differ, complementing the parity test above."""
    packaged = attest.registry_version()
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.5}}})
    assert attest.registry_version(root) != packaged


# ── (b) activating a project criterion changes the stamp ─────────────────────────
def test_activating_project_criterion_changes_registry_version(tmp_path):
    baseline = _make_repo(tmp_path / "base", overlay=None)
    base_ver = attest.registry_version(baseline)
    active = _make_repo(
        tmp_path / "active",
        overlay={"plan_review": {"project.no-print": _ROUTING}, "activate": ["project.no-print"]},
    )
    assert attest.registry_version(active) != base_ver
    # activating opens the vocabulary AND flips the stamp — a prior regver no longer matches
    assert "project.no-print" in registry.effective_criteria(active)


# ── (c)/(d) compute_validity grandfathers registry drift (ADR 0053) ──────────────
def _plan_att(regver: str) -> dict:
    # Unscoped (no dep map) plan-review attestation, no material line (skips the material check),
    # so the regver handling is what is under test.
    return {
        "manifest": ["plan-review: PASS", f"regver: {regver}"],
        "head_sha": "headA",
        "signed_at": 100,
    }


def test_compute_validity_valid_and_no_drift_when_regver_unchanged(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.7}}})
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = _plan_att(attest.registry_version(root))
    state = {"ticket_id": "t", "status": "in_progress"}
    res = attest.compute_validity(att, state, "plan-review", repo_root=root)
    assert res["valid"] is True and "registry_drift" not in res


def test_compute_validity_grandfathers_when_overlay_changed(tmp_path, monkeypatch):
    """ADR 0053: editing the overlay rotates the stamp but must NOT block the claim — the
    plan and the code it was reviewed against are both untouched."""
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.7}}})
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    signed_regver = attest.registry_version(root)
    att = _plan_att(signed_regver)  # signed against the current overlay
    # Now EDIT the overlay (new content ⇒ new signature ⇒ new regver).
    (Path(root) / ".rebar" / "criteria_routing.json").write_text(
        json.dumps({"plan_review": {"F1": {"block_threshold": 0.2}}}), encoding="utf-8"
    )
    prompt_library._invalidate_caches()
    res = attest.compute_validity(
        att, {"ticket_id": "t", "status": "in_progress"}, "plan-review", repo_root=root
    )
    current_regver = attest.registry_version(root)
    assert current_regver != signed_regver, "overlay edit should rotate the stamp"
    assert res["valid"] is True
    assert res["registry_drift"] == {"signed": signed_regver, "current": current_regver}


def test_compute_validity_grandfathers_when_regver_line_missing(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, overlay=None)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS"], "head_sha": "headA", "signed_at": 100}
    res = attest.compute_validity(
        att, {"ticket_id": "t", "status": "in_progress"}, "plan-review", repo_root=root
    )
    assert res["valid"] is True
    assert res["registry_drift"] == {"signed": None, "current": attest.registry_version(root)}


# ── (e) disabling a built-in ─────────────────────────────────────────────────────
def test_disabled_builtin_removed_from_effective_criteria(tmp_path):
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"disabled": True}}})
    assert "F1" not in registry.effective_criteria(root)
    assert registry.disabled_builtins(root) == ["F1"]
    # its routing entry is STILL resolvable (only the runnable vocabulary drops it)
    assert "F1" in registry.effective_routing(root)
    # and disabling flips the registry_version (the gate reads it as a change)
    assert attest.registry_version(root) != attest.registry_version()


def test_disabled_absent_is_empty_list(tmp_path):
    root = _make_repo(tmp_path, overlay=None)
    assert registry.disabled_builtins(root) == []


# ── (f) disabled on a project id is rejected ─────────────────────────────────────
def test_disabled_on_project_id_rejected(tmp_path):
    bad = {**_ROUTING, "disabled": True}
    root = _make_repo(tmp_path, overlay={"plan_review": {"project.x": bad}, "activate": []})
    with pytest.raises(registry.RegistryError, match="may not carry 'disabled'"):
        registry.effective_routing(root)


def test_non_bool_disabled_rejected(tmp_path):
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"disabled": "yes"}}})
    with pytest.raises(registry.RegistryError, match="'disabled' must be a boolean"):
        registry.effective_routing(root)


# ── (g) manifest line: emit / parse / absent-when-empty / still HMAC-verifies ─────
def test_build_manifest_emits_and_parses_disabled_builtins():
    verdict = {
        "verdict": "PASS",
        "ticket_id": "t",
        "coverage": {
            "counts": {"blocking": 0, "advisory_surfaced": 0},
            "disabled_builtins": ["G5", "F1"],
        },
    }
    manifest = attest.build_manifest(verdict, material="m", regver="rv0")
    assert "disabled_builtins: F1,G5" in manifest  # sorted, comma-joined
    assert attest.manifest_disabled_builtins(manifest) == ["F1", "G5"]


def test_manifest_disabled_builtins_absent_when_empty():
    verdict = {"verdict": "PASS", "ticket_id": "t", "coverage": {"counts": {}}}
    manifest = attest.build_manifest(verdict, material="m", regver="rv0")
    assert not any(str(line).startswith("disabled_builtins:") for line in manifest)
    assert attest.manifest_disabled_builtins(manifest) == []


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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-08af")
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_signed_manifest_with_disabled_line_still_verifies(store: Path):
    tid = rebar.create_ticket("task", "disabled-line HMAC", repo_root=str(store))
    verdict = {
        "verdict": "PASS",
        "ticket_id": tid,
        "coverage": {
            "counts": {"blocking": 0, "advisory_surfaced": 0},
            "disabled_builtins": ["F1"],
        },
    }
    manifest = attest.build_manifest(verdict, material="m", regver="rv0")
    signing.sign_manifest(tid, manifest, kind="plan-review", repo_root=str(store))
    result = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert result["verdict"] == "certified"
    assert attest.manifest_disabled_builtins(result["manifest"]) == ["F1"]


def _pin_validity_setup(monkeypatch: pytest.MonkeyPatch, *, target_fingerprint: str) -> dict:
    monkeypatch.setattr(attest, "registry_version", lambda *a, **k: "registry")
    monkeypatch.setattr("rebar.signing.head_sha", lambda *a, **k: "head-current")

    def fingerprint(ticket_id, repo_root=None):
        if ticket_id == "aaaa-bbbb-cccc-dddd":
            return target_fingerprint
        return "subject-material"

    monkeypatch.setattr(attest, "current_material_fingerprint", fingerprint)
    return {
        "manifest": [
            "plan-review: PASS",
            "regver: registry",
            "plan-material-pin: child aaaa-bbbb-cccc-dddd 1111111111111111",
        ],
        "head_sha": "head-current",
        "signed_at": 100,
    }


def test_advisory_pin_health_preserves_existing_validity_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _pin_validity_setup(monkeypatch, target_fingerprint="2222222222222222")
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: False)
    result = attest.compute_validity(
        attestation,
        {"ticket_id": "subject-0000-0000-0001", "status": "open"},
        "plan-review",
        repo_root="/repo",
    )
    assert {key: result[key] for key in ("valid", "reason", "verdict")} == {
        "valid": True,
        "reason": "certified plan-review attestation",
        "verdict": "certified",
    }
    assert result["health"]["pin_status"] == "stale-pin-drift"


def test_close_and_drift_refresh_disable_only_code_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _pin_validity_setup(monkeypatch, target_fingerprint="1111111111111111")
    attestation["head_sha"] = "old-head"
    state = {"ticket_id": "subject-0000-0000-0001", "status": "open"}
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    default = attest.compute_validity(
        attestation,
        state,
        "plan-review",
        repo_root="/repo",
        profile=attest.PlanValidityProfile.DEFAULT,
    )
    close = attest.compute_validity(
        attestation,
        state,
        "plan-review",
        repo_root="/repo",
        profile=attest.PlanValidityProfile.CLOSE,
    )
    refresh = attest.compute_validity(
        attestation,
        state,
        "plan-review",
        repo_root="/repo",
        profile=attest.PlanValidityProfile.DRIFT_REFRESH,
    )
    assert default["verdict"] == "stale-head"
    assert close["valid"] is refresh["valid"] is True
    assert close["health"] == refresh["health"]


def test_completion_verifier_return_shape_remains_unchanged() -> None:
    assert attest.compute_validity(
        {"manifest": [], "signed_at": 100},
        {"ticket_id": "t", "status": "closed"},
        "completion-verifier",
    ) == {
        "valid": True,
        "reason": "certified completion-verifier attestation",
        "verdict": "certified",
    }


def test_deriving_advisory_health_writes_no_ticket_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pin = PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "1111111111111111")
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "x" * 16)
    writes: list[tuple] = []
    monkeypatch.setattr("rebar._commands._seam.append_event", lambda *a, **k: writes.append(a))
    attest.derive_plan_material_pin_health((pin,), repo_root="/repo", enforced=False)
    assert writes == []


def _tracker_path(store: Path) -> Path:
    from rebar._commands._seam import tracker_dir

    return Path(tracker_dir(str(store)))


def _sign_scope_opcert(
    store: Path,
    ticket_id: str,
    manifest: list[str],
    *,
    material: str,
    commit: str,
) -> dict:
    tracker = _tracker_path(store)
    return signing.sign_opcert_manifest(
        ticket_id,
        manifest,
        material_fingerprint=material,
        merged_log_commit=commit,
        key_path=signing.ensure_opcert_key(str(tracker)),
        principal=signing.opcert_principal(str(tracker)),
        repo_root=str(store),
    )


def _commit_unrelated_head_move(store: Path) -> tuple[str, str]:
    before = signing.head_sha(str(store))
    (store / "unrelated.txt").write_text("unrelated change\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=store, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "unrelated"], cwd=store, check=True, capture_output=True
    )
    after = signing.head_sha(str(store))
    assert before != after
    return before, after


@pytest.mark.parametrize(
    ("scope_line", "expected_valid", "expected_verdict"),
    [
        ("file-scope: none", True, "certified"),
        (None, False, "stale-head"),
        ("file-scope: future-scope", False, "stale-head"),
    ],
)
def test_signed_empty_scope_head_drift_contract(
    store: Path,
    scope_line: str | None,
    expected_valid: bool,
    expected_verdict: str,
) -> None:
    ticket_id = rebar.create_ticket("task", "scope freshness", repo_root=str(store))
    if scope_line == "file-scope: none":
        rebar.declare_no_file_impact(
            ticket_id,
            "external operator action only",
            repo_root=str(store),
        )
    state = rebar.show_ticket(ticket_id, repo_root=str(store))
    material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert material is not None
    signed_head = signing.head_sha(str(store))
    manifest = [
        "plan-review: PASS",
        f"material: {material}",
        f"regver: {attest.registry_version(str(store))}",
    ]
    if scope_line is not None:
        manifest.append(scope_line)
    record = _sign_scope_opcert(
        store,
        ticket_id,
        manifest,
        material=material,
        commit=signed_head,
    )

    before, after = _commit_unrelated_head_move(store)
    assert before == signed_head and after != signed_head
    verified = verify_opcert_record(
        record,
        state["ticket_id"],
        kind="plan-review",
        repo_root=str(store),
    )
    result = attest.compute_validity(
        verified,
        state,
        "plan-review",
        repo_root=str(store),
    )

    assert result["valid"] is expected_valid
    assert result["verdict"] == expected_verdict


def _push_main_to_origin(store: Path) -> str:
    """Publish an ``origin`` remote at the current ``main`` and return its sha (the gate ref).

    Mirrors production: the attested plan-review's ``verified_at_sha`` is the ``origin/main``
    sha at review time, not the sha of whatever working tree the evaluator later sits in."""
    from rebar._snapshot.repo_snapshot import resolve_ref

    origin = store.parent / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=store, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=store,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=store, check=True, capture_output=True
    )
    return resolve_ref("origin/main", str(store), fetch=False)


def _diverge_working_tree(store: Path, message: str) -> str:
    """Commit a NEW head on a side branch WITHOUT moving ``origin/main``; return its sha."""
    subprocess.run(
        ["git", "checkout", "-q", "-b", f"feat-{message}"],
        cwd=store,
        check=True,
        capture_output=True,
    )
    (store / f"{message}.txt").write_text(f"{message}\n", encoding="utf-8")
    subprocess.run(["git", "add", f"{message}.txt"], cwd=store, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=store, check=True, capture_output=True
    )
    return signing.head_sha(str(store))


def _attested_unscoped_record(store: Path, ticket_id: str, gate_ref_sha: str) -> dict:
    """An ATTESTED unscoped plan-review opcert pinned to ``gate_ref_sha`` (verified-at-sha)."""
    material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert material is not None
    from rebar._signing_manifest import verified_at_sha_step

    manifest = [
        "plan-review: PASS",
        f"material: {material}",
        f"regver: {attest.registry_version(str(store))}",
        verified_at_sha_step(gate_ref_sha),
    ]
    return _sign_scope_opcert(
        store,
        ticket_id,
        manifest,
        material=material,
        commit=gate_ref_sha,
    )


def test_current_head_sha_attested_resolves_gate_ref_not_working_tree(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1137 (SHARED anchor): ``gate_source.current_head_sha`` for an ATTESTED manifest returns
    the current gate-ref sha from the LOCAL object DB, NOT the evaluator's (possibly foreign)
    working-tree HEAD. This single anchor is read by BOTH ``compute_validity``'s unscoped
    freshness check AND ``drift_floor``'s ``code_drifted`` axis, so neither consumer can read a
    stranger sha. A LEGACY manifest (no ``verified_at_sha``) keeps the working-tree read."""
    from rebar._signing_manifest import verified_at_sha_step
    from rebar._snapshot.repo_snapshot import resolve_ref
    from rebar.llm import gate_source

    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    gate_ref_sha = _push_main_to_origin(store)
    foreign_head = _diverge_working_tree(store, "shared-anchor")
    assert resolve_ref("origin/main", str(store), fetch=False) == gate_ref_sha
    assert foreign_head != gate_ref_sha

    attested_manifest = ["plan-review: PASS", verified_at_sha_step(gate_ref_sha)]
    got = gate_source.current_head_sha(attested_manifest, repo_root=str(store))
    assert got == gate_ref_sha, got
    assert got != foreign_head

    # Legacy (no verified_at_sha) still reads the working-tree HEAD unchanged.
    legacy_head = gate_source.current_head_sha(["plan-review: PASS"], repo_root=str(store))
    assert legacy_head == foreign_head, legacy_head


def test_current_head_sha_source_local_reads_working_tree_not_gate_ref(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1137 (SHARED anchor, ``source=local`` arm): even with a ``verified_at_sha`` present,
    when the gate ``source`` is ``local`` the anchor is the in-place checkout's working-tree HEAD
    (``source=local``'s documented basis), NOT the resolved ``origin/main`` gate ref. So the same
    signed sha compared against a MOVED working tree reads that moved HEAD (drift is real here),
    and the attested gate-ref resolution is bypassed entirely."""
    from rebar._signing_manifest import verified_at_sha_step
    from rebar.llm import gate_source

    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    monkeypatch.setenv("REBAR_GATE_SOURCE", gate_source.SOURCE_LOCAL)
    gate_ref_sha = _push_main_to_origin(store)
    working_head = _diverge_working_tree(store, "source-local")
    assert working_head != gate_ref_sha
    assert gate_source.default_source(str(store)) == gate_source.SOURCE_LOCAL

    attested_manifest = ["plan-review: PASS", verified_at_sha_step(gate_ref_sha)]
    got = gate_source.current_head_sha(attested_manifest, repo_root=str(store))
    assert got == working_head, got
    assert got != gate_ref_sha


def test_attested_unscoped_head_freshness_reads_gate_ref_not_working_tree(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1137: an attested unscoped ('fail-safe' whole-HEAD) attestation certified at an
    UNMOVED gate ref must stay valid even when evaluated from a working tree whose HEAD is a
    FOREIGN commit (a feature worktree, the MCP server's own cwd, or a walked-up enclosing
    repo). The currency check must resolve the CURRENT gate ref, not ``git rev-parse HEAD`` of
    the evaluator's tree. RED before the fix: it read the working-tree HEAD and reported a
    spurious ``stale-head``."""
    # Production models a REMOTE gate ref (origin/main); the suite default pins it to HEAD.
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    ticket_id = rebar.create_ticket("epic", "attested unscoped freshness", repo_root=str(store))
    state = rebar.show_ticket(ticket_id, repo_root=str(store))
    gate_ref_sha = _push_main_to_origin(store)
    record = _attested_unscoped_record(store, ticket_id, gate_ref_sha)

    # Diverge the working tree onto a foreign HEAD; origin/main (the gate ref) is UNMOVED.
    foreign_head = _diverge_working_tree(store, "foreign")
    from rebar._snapshot.repo_snapshot import resolve_ref

    assert resolve_ref("origin/main", str(store), fetch=False) == gate_ref_sha
    assert foreign_head != gate_ref_sha

    verified = verify_opcert_record(
        record, state["ticket_id"], kind="plan-review", repo_root=str(store)
    )
    result = attest.compute_validity(verified, state, "plan-review", repo_root=str(store))

    assert result["valid"] is True, result
    assert result["verdict"] == "certified"


def test_attested_unscoped_head_freshness_still_invalidates_on_moved_gate_ref(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for bug 1137: the fix must PRESERVE whole-HEAD invalidation semantics.
    When the gate ref (``origin/main``) genuinely advances after review, the attested unscoped
    attestation is still ``stale-head`` — the fix corrects only the foreign VALUE read, not the
    invalidation granularity (the ticket's declared non-goal)."""
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    ticket_id = rebar.create_ticket("epic", "attested unscoped moved ref", repo_root=str(store))
    state = rebar.show_ticket(ticket_id, repo_root=str(store))
    gate_ref_sha = _push_main_to_origin(store)
    record = _attested_unscoped_record(store, ticket_id, gate_ref_sha)

    # The gate ref genuinely moves: commit on main and push, so origin/main advances past the
    # signed verified_at_sha.
    (store / "moved.txt").write_text("gate ref advanced\n", encoding="utf-8")
    subprocess.run(["git", "add", "moved.txt"], cwd=store, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "advance main"], cwd=store, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=store, check=True, capture_output=True
    )
    from rebar._snapshot.repo_snapshot import resolve_ref

    assert resolve_ref("origin/main", str(store), fetch=False) != gate_ref_sha

    verified = verify_opcert_record(
        record, state["ticket_id"], kind="plan-review", repo_root=str(store)
    )
    result = attest.compute_validity(verified, state, "plan-review", repo_root=str(store))

    assert result["valid"] is False, result
    assert result["verdict"] == "stale-head"


def test_attested_unscoped_head_freshness_fails_closed_when_gate_ref_unresolvable(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1137 fail-CLOSED arm: when the attested gate ref cannot be resolved to a local
    snapshot (``fetch=False`` and the ref is absent from the object DB), the currency check
    must refuse honestly -- it must NOT fall back to reading the working tree, and must NOT
    emit a foreign value. The reason names the unresolvable gate ref."""
    # origin/main is the configured gate ref but no ``origin`` remote is ever published, so
    # resolve_ref(..., fetch=False) cannot resolve it locally -> SnapshotError -> fail closed.
    monkeypatch.setenv("REBAR_GATE_REF", "origin/main")
    ticket_id = rebar.create_ticket(
        "epic", "attested unscoped unresolvable ref", repo_root=str(store)
    )
    state = rebar.show_ticket(ticket_id, repo_root=str(store))
    # Sign against the current HEAD as the (now locally unreachable) gate-ref anchor.
    gate_ref_sha = signing.head_sha(str(store))
    record = _attested_unscoped_record(store, ticket_id, gate_ref_sha)

    from rebar._snapshot.repo_snapshot import SnapshotError, resolve_ref

    with pytest.raises(SnapshotError):
        resolve_ref("origin/main", str(store), fetch=False)

    verified = verify_opcert_record(
        record, state["ticket_id"], kind="plan-review", repo_root=str(store)
    )
    result = attest.compute_validity(verified, state, "plan-review", repo_root=str(store))

    assert result["valid"] is False, result
    assert result["verdict"] == "stale-head"
    assert "could not be resolved" in result["reason"], result
    # Fail closed, NOT with the working-tree HEAD as a foreign value.
    assert gate_ref_sha not in result["reason"], result


def test_plaintext_none_scope_cannot_override_authenticated_unscoped_manifest(
    store: Path,
) -> None:
    ticket_id = rebar.create_ticket("task", "scope tamper", repo_root=str(store))
    state = rebar.show_ticket(ticket_id, repo_root=str(store))
    material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert material is not None
    signed_head = signing.head_sha(str(store))
    signed_manifest = [
        "plan-review: PASS",
        f"material: {material}",
        f"regver: {attest.registry_version(str(store))}",
    ]
    record = _sign_scope_opcert(
        store,
        ticket_id,
        signed_manifest,
        material=material,
        commit=signed_head,
    )
    _commit_unrelated_head_move(store)

    tampered = {**record, "manifest": [*signed_manifest, "file-scope: none"]}
    verified = verify_opcert_record(
        tampered,
        state["ticket_id"],
        kind="plan-review",
        repo_root=str(store),
    )
    result = attest.compute_validity(
        verified,
        state,
        "plan-review",
        repo_root=str(store),
    )

    assert result["valid"] is False
    assert result["verdict"] == "stale-head"


@pytest.mark.parametrize(
    ("paths", "own_scope", "container_all_none", "expected"),
    [
        (["src/a.py"], "none", False, "paths"),
        ([], "none", False, "none"),
        ([], "undeclared", False, "unscoped"),
        ([], "paths", False, "unscoped"),
        ([], "undeclared", True, "none"),
        ([], "paths", True, "unscoped"),
    ],
)
def test_file_scope_classifier_contract(
    paths: list[str],
    own_scope: str,
    container_all_none: bool,
    expected: str,
) -> None:
    assert (
        attest.classify_file_scope(
            paths,
            own_scope,
            container_all_none=container_all_none,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("case", "expected_deps", "expected_scope"),
    [
        ("parent-paths-all-none", {"parent.py"}, "unscoped"),
        ("parent-none-all-none", set(), "none"),
        ("no-live-children", set(), "unscoped"),
        ("malformed-child-poisons", set(), "unscoped"),
    ],
)
def test_normal_sign_container_scope_contract(
    store: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_deps: set[str],
    expected_scope: str,
) -> None:
    for path in ("parent.py", "child.py"):
        (store / path).write_text(f"# {path}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "parent.py", "child.py"],
        cwd=store,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "scope fixtures"],
        cwd=store,
        check=True,
        capture_output=True,
    )

    ticket_id = rebar.create_ticket("story", case, repo_root=str(store))
    none_child = {
        "ticket_id": "none-child",
        "status": "open",
        "file_impact": [],
        "file_impact_scope": "none",
    }
    if case == "parent-paths-all-none":
        rebar.set_file_impact(
            ticket_id,
            [{"path": "parent.py", "reason": "parent implementation"}],
            repo_root=str(store),
        )
        children = [none_child]
    elif case == "parent-none-all-none":
        rebar.declare_no_file_impact(
            ticket_id,
            "coordination container",
            repo_root=str(store),
        )
        children = [none_child]
    elif case == "malformed-child-poisons":
        children = [
            {
                "ticket_id": "path-child",
                "status": "open",
                "file_impact": [{"path": "child.py"}],
                "file_impact_scope": "paths",
            },
            {
                "ticket_id": "malformed-child",
                "status": "open",
                "file_impact": [],
                "file_impact_scope": "paths",
            },
        ]
    else:
        children = []

    monkeypatch.setattr(
        "rebar.llm.gate_context.current_code_sha",
        lambda: signing.head_sha(str(store)),
    )
    material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert material is not None
    attest.sign_plan_review(
        {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "model": "test",
            "runner": "test",
            "coverage": {"counts": {}, "llm_ran": True},
        },
        material=material,
        children=children,
        repo_root=str(store),
    )
    verified = signing.verify_signature(
        ticket_id,
        kind="plan-review",
        repo_root=str(store),
    )

    assert set(attest.manifest_deps(verified["manifest"])) == expected_deps
    assert attest.manifest_file_scope(verified["manifest"]) == expected_scope


def test_checkbox_flip_keeps_signed_material_valid(store: Path) -> None:
    """Bug 330c: the 433c close precheck REQUIRES flipping AC boxes before close;
    a pure box-flip edit must not trip stale-material on the signed review."""
    ticket_id = rebar.create_ticket(
        "task",
        "box flip",
        description="## Acceptance Criteria\n- [ ] alpha\n- [ ] beta\n",
        repo_root=str(store),
    )
    before_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    before_material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert before_material is not None
    manifest = attest.build_manifest(
        {
            "verdict": "PASS",
            "ticket_id": before_state["ticket_id"],
            "coverage": {"counts": {}},
        },
        material=before_material,
        regver=attest.registry_version(str(store)),
        file_scope="none",
    )
    record = _sign_scope_opcert(
        store,
        ticket_id,
        manifest,
        material=before_material,
        commit=signing.head_sha(str(store)),
    )

    rebar.edit_ticket(
        ticket_id,
        description="## Acceptance Criteria\n- [x] alpha\n- [x] beta\n",
        repo_root=str(store),
    )
    after_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    after_material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert after_material == before_material, "a pure box flip must not move the fingerprint"
    verified = verify_opcert_record(
        record,
        after_state["ticket_id"],
        kind="plan-review",
        repo_root=str(store),
    )
    result = attest.compute_validity(
        verified,
        after_state,
        "plan-review",
        repo_root=str(store),
    )
    assert result["valid"] is True
    assert result["verdict"] != "stale-material"


def test_none_reason_edit_invalidates_signed_material(store: Path) -> None:
    ticket_id = rebar.create_ticket("task", "reason drift", repo_root=str(store))
    rebar.declare_no_file_impact(ticket_id, "external action alpha", repo_root=str(store))
    before_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    before_material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert before_material is not None
    manifest = attest.build_manifest(
        {
            "verdict": "PASS",
            "ticket_id": before_state["ticket_id"],
            "coverage": {"counts": {}},
        },
        material=before_material,
        regver=attest.registry_version(str(store)),
        file_scope="none",
    )
    record = _sign_scope_opcert(
        store,
        ticket_id,
        manifest,
        material=before_material,
        commit=signing.head_sha(str(store)),
    )

    rebar.declare_no_file_impact(ticket_id, "external action beta", repo_root=str(store))
    after_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    after_material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert after_material not in (None, before_material)
    verified = verify_opcert_record(
        record,
        after_state["ticket_id"],
        kind="plan-review",
        repo_root=str(store),
    )
    result = attest.compute_validity(
        verified,
        after_state,
        "plan-review",
        repo_root=str(store),
    )

    assert result["valid"] is False
    assert result["verdict"] == "stale-material"


def test_none_to_paths_invalidates_signed_material(store: Path) -> None:
    ticket_id = rebar.create_ticket("task", "scope drift", repo_root=str(store))
    rebar.declare_no_file_impact(ticket_id, "external action only", repo_root=str(store))
    before_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    before_material = attest.current_material_fingerprint(ticket_id, repo_root=str(store))
    assert before_material is not None
    manifest = attest.build_manifest(
        {
            "verdict": "PASS",
            "ticket_id": before_state["ticket_id"],
            "coverage": {"counts": {}},
        },
        material=before_material,
        regver=attest.registry_version(str(store)),
        file_scope="none",
    )
    record = _sign_scope_opcert(
        store,
        ticket_id,
        manifest,
        material=before_material,
        commit=signing.head_sha(str(store)),
    )

    rebar.set_file_impact(
        ticket_id,
        [{"path": "src/changed.py", "reason": "implementation"}],
        repo_root=str(store),
    )
    after_state = rebar.show_ticket(ticket_id, repo_root=str(store))
    verified = verify_opcert_record(
        record,
        after_state["ticket_id"],
        kind="plan-review",
        repo_root=str(store),
    )
    result = attest.compute_validity(
        verified,
        after_state,
        "plan-review",
        repo_root=str(store),
    )

    assert result["valid"] is False
    assert result["verdict"] == "stale-material"
