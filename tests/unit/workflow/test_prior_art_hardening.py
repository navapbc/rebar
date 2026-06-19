"""Regression tests for the post-epic prior-art hardening pass (epic a88f
follow-up, ticket crude-hook-stomp).

Each test pins a verified gotcha drawn from comparing the workflow engine to its
prior art (GitHub Actions, Argo Workflows, the CPython tarfile CVE-2007-4559
remediation):

  * a bare ``if:`` (no ``${{ }}``) must NOT be silently always-true (the GHA trap);
  * ``secrets.*`` must not drive control flow in an ``if:`` guard;
  * duplicate YAML mapping keys must be rejected (YAML 1.2 Core; Argo/GHA reject);
  * the git-archive snapshot must guard the byte AND file-count caps, refuse to
    extract on a Python lacking ``tarfile.data_filter``, and the TTL sweep must
    actually remove read-only snapshot trees (not silently leak them).

Pure stdlib + a real git snapshot of HEAD; no network/model.
"""

from __future__ import annotations

import tarfile

import pytest

from rebar.llm.workflow import executor as ex
from rebar.llm.workflow import lint as L
from rebar.llm.workflow import schema as S
from rebar.llm.workflow import snapshot as SNAP


def _wf(if_value: str) -> str:
    return (
        'schema_version: "1"\n'
        "name: t\n"
        "steps:\n"
        "  - id: a\n"
        "    uses: fetch_ticket\n"
        "  - id: b\n"
        "    uses: tag_step\n"
        "    needs: [a]\n"
        f"    if: {if_value}\n"
    )


# ── #1 bare `if:` always-true ──────────────────────────────────────────────────


def test_bare_if_is_rejected_by_lint() -> None:
    findings = L.lint_workflow(_wf('"steps.a.outputs.ok"'))
    msgs = [str(f) for f in findings]
    assert any("must be a `${{" in m and ".if" in m for m in msgs), msgs


def test_wrapped_if_is_accepted_by_lint() -> None:
    findings = [
        f for f in L.lint_workflow(_wf('"${{ steps.a.outputs.ok }}"')) if f.severity != "warning"
    ]
    assert findings == [], [str(f) for f in findings]


def test_guard_fails_closed_on_bare_string() -> None:
    # Defense in depth: even if internals are driven past the lint, a bare guard
    # skips (fail-closed) rather than the old silently-truthy behavior.
    state = ex.RunState(inputs={}, outputs={}, statuses={})
    assert ex._guard_passes({"if": "steps.a.outputs.ok"}, state, {}) is False
    assert ex._guard_passes({}, state, {}) is True  # no guard -> runs


# ── #3 secrets in `if:` ─────────────────────────────────────────────────────────


def test_secrets_in_if_guard_rejected() -> None:
    findings = L.lint_workflow(_wf('"${{ secrets.TOKEN }}"'))
    msgs = [str(f) for f in findings]
    assert any("secrets may not be referenced in an `if:`" in m for m in msgs), msgs


# ── #4 duplicate YAML keys ──────────────────────────────────────────────────────


def test_duplicate_top_level_key_rejected() -> None:
    with pytest.raises(S.WorkflowParseError, match="duplicate key 'name'"):
        S.parse_workflow('schema_version: "1"\nname: x\nname: y\nsteps:\n  - id: a\n    uses: u\n')


def test_duplicate_step_key_rejected() -> None:
    src = 'schema_version: "1"\nname: x\nsteps:\n  - id: a\n    uses: first\n    uses: second\n'
    with pytest.raises(S.WorkflowParseError, match="duplicate key 'uses'"):
        S.parse_workflow(src)


# ── #19 / #21 snapshot extraction guards ────────────────────────────────────────


def test_snapshot_file_count_cap_fires() -> None:
    flt = SNAP._hardened_filter(10**12, max_files=2)
    flt(tarfile.TarInfo(name="a.txt"), ".")
    flt(tarfile.TarInfo(name="b.txt"), ".")
    with pytest.raises(SNAP.SnapshotError, match="file cap"):
        flt(tarfile.TarInfo(name="c.txt"), ".")


def test_snapshot_byte_cap_fires() -> None:
    flt = SNAP._hardened_filter(max_bytes=10)
    big = tarfile.TarInfo(name="big.bin")
    big.size = 100
    with pytest.raises(SNAP.SnapshotError, match="byte cap"):
        flt(big, ".")


def test_require_safe_extraction_guard(monkeypatch) -> None:
    # On a Python lacking tarfile.data_filter (3.11.0–3.11.3), extraction must
    # fail closed with a clear message rather than extracting unfiltered.
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    with pytest.raises(SNAP.SnapshotError, match="data_filter"):
        SNAP._require_safe_extraction()


def test_sweep_removes_readonly_snapshot_tree(tmp_path) -> None:
    # Snapshot a throwaway git repo UNDER tmp (with an explicit repo_root), never the
    # real checkout: snapshot_at_ref/sweep default the .rebar/run_snapshots location to
    # cwd, so a bare snapshot_at_ref("HEAD") here would write .rebar into REPO_ROOT and
    # trip the repo-isolation guard in CI (it only slips by locally because dogfooding
    # already created a .rebar, so the new-entry guard sees nothing added).
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)

    # A published snapshot tree is chmod'd read-only; the TTL sweep must restore
    # write bits and actually remove it (a plain rmtree would silently leak it).
    path = SNAP.snapshot_at_ref("HEAD", str(repo))
    assert path.is_dir()
    removed = ex.sweep_orphan_snapshots(str(repo), ttl_seconds=-1)
    assert str(path) in removed
    assert not path.exists()
