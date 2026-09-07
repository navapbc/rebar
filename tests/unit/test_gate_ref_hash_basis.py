from __future__ import annotations

from pathlib import Path

import pytest

from rebar._snapshot.git_fetch import SnapshotFetchError, SnapshotRefError
from rebar.llm import gate_source
from rebar.llm.plan_review.manifest import gate_ref_hash_basis


def test_gate_ref_hash_basis_reports_unresolvable_ref_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(gate_source, "default_source", lambda _root: gate_source.SOURCE_ATTESTED)
    monkeypatch.setattr(gate_source, "default_ref", lambda _root: "refs/heads/missing")

    def fail_resolve(ref: str, _root: str, *, fetch: bool) -> str:
        raise SnapshotRefError(f"cannot resolve {ref}")

    monkeypatch.setattr("rebar._snapshot.repo_snapshot.resolve_ref", fail_resolve)

    basis = gate_ref_hash_basis(str(tmp_path))

    assert basis.path == str(tmp_path)
    assert basis.ref == "refs/heads/missing"
    assert basis.degraded is True
    assert "gate ref 'refs/heads/missing' could not be resolved" in caplog.text
    assert "could not be materialized" not in caplog.text


def test_gate_ref_hash_basis_reports_materialization_failure_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sha = "a" * 40
    monkeypatch.setattr(gate_source, "default_source", lambda _root: gate_source.SOURCE_ATTESTED)
    monkeypatch.setattr(gate_source, "default_ref", lambda _root: "origin/main")
    monkeypatch.setattr(
        "rebar._snapshot.repo_snapshot.resolve_ref",
        lambda _ref, _root, *, fetch: sha,
    )

    def fail_acquire(resolved_sha: str, *, source_mode: str, repo_root: str, fetch: bool) -> object:
        raise SnapshotFetchError(f"cannot materialize {resolved_sha}")

    monkeypatch.setattr("rebar._snapshot.cache.acquire", fail_acquire)

    basis = gate_ref_hash_basis(str(tmp_path))

    assert basis.path == str(tmp_path)
    assert basis.ref == "origin/main"
    assert basis.degraded is True
    assert f"gate ref 'origin/main' resolved to {sha} but could not be materialized" in caplog.text
    assert "could not be resolved" not in caplog.text


def test_gate_ref_hash_basis_local_source_is_not_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate_source, "default_source", lambda _root: gate_source.SOURCE_LOCAL)

    basis = gate_ref_hash_basis(str(tmp_path))

    assert basis.path == str(tmp_path)
    assert basis.ref is None
    assert basis.degraded is False
