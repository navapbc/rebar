"""Per-kind verification surface tests (story cc1b, epic dark-acme-lumen).

Pins ``verify_signature(kind=...)`` (None → most-recent mirror back-compat; explicit kind →
strict per-kind from the map), ``verify_attestations`` (all kinds), and the CLI's
``--kind`` / no-``--kind`` behavior.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing


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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-key-cc1b")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _two_kinds(store: Path) -> str:
    tid = rebar.create_ticket("task", "two kinds", repo_root=str(store))
    signing.sign_manifest(tid, ["plan-review: PASS", "m"], kind="plan-review", repo_root=str(store))
    signing.sign_manifest(
        tid, ["completion-verifier: PASS"], kind="completion-verifier", repo_root=str(store)
    )
    return tid


def test_verify_signature_explicit_kind_strict(store: Path) -> None:
    tid = _two_kinds(store)
    plan = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert plan["verdict"] == "certified"
    assert plan["kind"] == "plan-review"  # the requested kind is echoed back
    comp = signing.verify_signature(tid, kind="completion-verifier", repo_root=str(store))
    assert comp["verdict"] == "certified"
    assert comp["manifest"][0] == "completion-verifier: PASS"
    # A kind not present is strictly unsigned (the mirror of a different kind is NOT substituted).
    absent = signing.verify_signature(tid, kind="nonexistent-kind", repo_root=str(store))
    assert absent["verdict"] == "unsigned"


def test_verify_signature_none_is_most_recent_mirror_backcompat(store: Path) -> None:
    tid = _two_kinds(store)
    # No kind → the most-recent signature (the mirror), exact pre-attestations behavior.
    res = signing.verify_signature(tid, repo_root=str(store))
    assert res["verdict"] == "certified"
    assert res["manifest"][0] == "completion-verifier: PASS"  # last signed wins the mirror


def test_verify_signature_none_certifies_generic_manifest(store: Path) -> None:
    # Back-compat: a generic (non-kind) manifest still verifies via the mirror with no kind.
    tid = rebar.create_ticket("task", "generic", repo_root=str(store))
    signing.sign_manifest(tid, ["step one", "step two"], repo_root=str(store))
    assert signing.verify_signature(tid, repo_root=str(store))["verdict"] == "certified"


def test_verify_attestations_all_kinds(store: Path) -> None:
    tid = _two_kinds(store)
    allv = signing.verify_attestations(tid, repo_root=str(store))
    assert set(allv) == {"plan-review", "completion-verifier"}
    assert all(v["verdict"] == "certified" for v in allv.values())
    # Each entry carries its ticket_id + kind.
    for k, v in allv.items():
        assert v["kind"] == k and v["ticket_id"] == tid
    # No attestations → empty map.
    bare = rebar.create_ticket("task", "bare", repo_root=str(store))
    assert signing.verify_attestations(bare, repo_root=str(store)) == {}


def test_cli_kind_selector(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tid = _two_kinds(store)
    # --kind (space form) → single-gate exit 0 on certified.
    assert signing.verify_signature_cli([tid, "--kind", "plan-review"]) == 0
    capsys.readouterr()
    # --kind=value (equals form) too.
    assert signing.verify_signature_cli([tid, "--kind=completion-verifier"]) == 0
    capsys.readouterr()
    # --kind for an absent kind → exit 1 (unsigned).
    assert signing.verify_signature_cli([tid, "--kind", "nope"]) == 1
    capsys.readouterr()
    # No --kind → the most-recent signature (mirror), single verdict, back-compat exit 0.
    assert signing.verify_signature_cli([tid]) == 0
    assert "SIGNATURE:" in capsys.readouterr().out
