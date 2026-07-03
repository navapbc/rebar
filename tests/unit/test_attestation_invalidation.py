"""Overlay-aware attestation invalidation (story 08af, epic 3156).

The plan-review claim gate must invalidate a prior attestation when the project's criteria
overlay changes — activating, re-tuning, or disabling a criterion. These tests pin:

* ``registry_version(repo_root)`` is overlay-aware, but overlay-ABSENT is BYTE-IDENTICAL to the
  packaged ``registry_version()`` (existing certs stay valid — zero churn);
* ``compute_validity`` returns ``stale-regver`` when the overlay changed vs the signed regver
  (and when the regver line is missing entirely), and ``valid`` when unchanged;
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
from rebar.llm.plan_review import attest, registry
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


# ── (c)/(d) compute_validity stale-regver ────────────────────────────────────────
def _plan_att(regver: str) -> dict:
    # Unscoped (no dep map) plan-review attestation, no material line (skips the material check),
    # so the regver check is what is under test.
    return {
        "manifest": ["plan-review: PASS", f"regver: {regver}"],
        "head_sha": "headA",
        "signed_at": 100,
    }


def test_compute_validity_valid_when_regver_unchanged(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.7}}})
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = _plan_att(attest.registry_version(root))
    state = {"ticket_id": "t", "status": "in_progress"}
    assert attest.compute_validity(att, state, "plan-review", repo_root=root)["valid"] is True


def test_compute_validity_stale_when_overlay_changed(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, overlay={"plan_review": {"F1": {"block_threshold": 0.7}}})
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = _plan_att(attest.registry_version(root))  # signed against the current overlay
    # Now EDIT the overlay (new content ⇒ new signature ⇒ new regver).
    (Path(root) / ".rebar" / "criteria_routing.json").write_text(
        json.dumps({"plan_review": {"F1": {"block_threshold": 0.2}}}), encoding="utf-8"
    )
    prompt_library._invalidate_caches()
    res = attest.compute_validity(
        att, {"ticket_id": "t", "status": "in_progress"}, "plan-review", repo_root=root
    )
    assert res["valid"] is False and res["verdict"] == "stale-regver"


def test_compute_validity_stale_when_regver_line_missing(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, overlay=None)
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    att = {"manifest": ["plan-review: PASS"], "head_sha": "headA", "signed_at": 100}
    res = attest.compute_validity(
        att, {"ticket_id": "t", "status": "in_progress"}, "plan-review", repo_root=root
    )
    assert res["valid"] is False and res["verdict"] == "stale-regver"


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
