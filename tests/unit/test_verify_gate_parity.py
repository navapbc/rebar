"""Tests for the Verified-gate parity drift-guard (jira-reb-1163).

The Gerrit ``Verified`` vote is cast by ``.github/workflows/gerrit-verify.yaml``'s
``vote`` job, which aggregates the run conclusion of the jobs listed in its ``needs``.
The push/PR "mirror" lanes (``test.yml``, ``optionality.yml``, ``verify-identity.yml``,
``prompt-eval.yml``) each define the unconditional jobs that gate ``main`` post-merge.

If a job gates ``main`` post-merge but is ABSENT from ``vote.needs``, a change that
breaks it earns ``Verified +1`` pre-merge yet reddens ``main`` after it lands
(green-verify / red-main). ``scripts/check_verify_gate_parity.py`` fails the build when
``vote.needs`` is not a superset of every such gating job. These tests exercise that
guard on synthetic workflow pairs (drift + parity-complete) and assert the REAL repo is
in parity.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_verify_gate_parity.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_verify_gate_parity", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _gerrit(needs: list[str]) -> dict:
    """A minimal gerrit-verify workflow whose vote job depends on ``needs``."""
    return {"jobs": {"vote": {"needs": list(needs)}}}


def _source(*job_names: str) -> dict:
    """A minimal push/PR source workflow exposing the given job names."""
    return {"jobs": {name: {"runs-on": "ubuntu-latest"} for name in job_names}}


# ─────────────────────────── guard-logic: DRIFT is caught ──────────────────────


def test_guard_reports_missing_gating_job():
    """A gating job present in a source lane but absent from vote.needs is drift."""
    gerrit = _gerrit(["build-and-test"])
    sources = [_source("build-and-test", "artifact-probe")]
    missing = chk.missing_gating_jobs(gerrit, sources, excluded=set())
    assert missing == {"artifact-probe"}


def test_guard_main_exit_nonzero_on_drift(monkeypatch, capsys):
    """The check's decision helper returns non-zero when drift exists."""
    gerrit = _gerrit(["build-and-test"])
    sources = [_source("build-and-test", "eval-discipline")]
    rc = chk.evaluate(gerrit, sources, excluded=set())
    assert rc != 0


# ─────────────────────────── guard-logic: PARITY passes ────────────────────────


def test_guard_passes_on_parity_complete_pair():
    """When vote.needs covers every gating job, there is no drift."""
    gerrit = _gerrit(["build-and-test", "artifact-probe", "eval-discipline"])
    sources = [_source("build-and-test", "artifact-probe"), _source("eval-discipline")]
    assert chk.missing_gating_jobs(gerrit, sources, excluded=set()) == set()
    assert chk.evaluate(gerrit, sources, excluded=set()) == 0


def test_guard_respects_excluded_jobs():
    """A job on the deliberate exclude-list is not required in vote.needs."""
    gerrit = _gerrit(["build-and-test"])
    sources = [_source("build-and-test", "external", "eval-live")]
    missing = chk.missing_gating_jobs(gerrit, sources, excluded={"external", "eval-live"})
    assert missing == set()


def test_vote_needs_accepts_scalar():
    """A scalar (single-string) needs is normalized to a set."""
    assert chk.vote_needs({"jobs": {"vote": {"needs": "build-and-test"}}}) == {"build-and-test"}


# ─────────────────────────── REAL repo parity ─────────────────────────────────


def test_real_repo_verify_gate_is_in_parity():
    """The committed workflows: vote.needs is a superset of every gating job.

    RED before artifact-probe/eval-discipline are added to gerrit-verify.yaml's
    vote.needs; GREEN after. This is the invariant that prevents green-verify/red-main.
    """
    gerrit, sources = chk.load_real()
    missing = chk.missing_gating_jobs(gerrit, sources, excluded=chk.EXCLUDED_JOBS)
    assert missing == set(), (
        f"vote.needs is missing gating jobs (would green-verify but red-main): {sorted(missing)}"
    )


def test_real_repo_check_main_returns_zero():
    """Running the checker end-to-end against the real repo exits 0."""
    assert chk.main() == 0
