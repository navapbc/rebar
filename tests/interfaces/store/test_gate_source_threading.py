"""S3 — thread ref + source through the code-reading gates (epic raze-vet-ditch).

The load-bearing behavioral contract: in ``attested`` mode a gate reads a snapshot
materialized at the client-pinned ref, NEVER the server's checked-out branch (reproducing
+ fixing this epic's motivating wrong-branch false-negative). Plus: ``local`` mode reads the
in-place checkout and is flagged unsigned; defaults resolve via config + env; an absent
snapshot self-heals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm import gate_source
from rebar.llm.runner import FakeRunner


class CapturingRunner(FakeRunner):
    """A FakeRunner that records the read-root (``cfg.repo_path``) the gate ran with and
    the content it would actually read from there — the seam for asserting WHICH tree the
    gate read (snapshot vs checkout)."""

    def __init__(self):
        super().__init__([])
        self.captured_repo_path: str | None = None
        self.captured_sentinel: str | None = None

    def run(self, req):
        self.captured_repo_path = req.config.repo_path
        try:
            self.captured_sentinel = (Path(req.config.repo_path) / "sentinel.txt").read_text()
        except OSError:
            self.captured_sentinel = None
        return super().run(req)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def gate_tmpdir(monkeypatch, tmp_path):
    base = tmp_path / "gate-store"
    base.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    return base


@pytest.fixture
def repo_with_origin(tmp_path, monkeypatch):
    """A rebar repo whose ``origin/main`` holds ``sentinel.txt='from-main'`` while the
    CHECKED-OUT branch holds ``sentinel.txt='from-other'`` — the moving-checkout setup."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    (repo / "sentinel.txt").write_text("from-main\n")
    _git(repo, "add", "sentinel.txt")
    _git(repo, "commit", "-q", "-m", "main content")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")
    main_sha = _git(repo, "rev-parse", "HEAD")

    # Switch the shared checkout to a DIFFERENT branch with different content — the exact
    # condition that produced the false-negative the epic exists to fix.
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "sentinel.txt").write_text("from-other\n")
    _git(repo, "add", "sentinel.txt")
    _git(repo, "commit", "-q", "-m", "other content")
    return repo, main_sha


def _ticket(repo: Path) -> str:
    return rebar.create_ticket("task", "S3 gate-source test ticket", repo_root=str(repo))


# --------------------------------------------------------------------------------------
# AC7 — attested reads the pinned ref's snapshot, NOT the checked-out branch
# --------------------------------------------------------------------------------------
def test_attested_reads_pinned_ref_regardless_of_checkout_branch(repo_with_origin, gate_tmpdir):
    repo, main_sha = repo_with_origin
    tid = _ticket(repo)
    # The shared checkout is on `other` (from-other); attested must read origin/main.
    assert (repo / "sentinel.txt").read_text() == "from-other\n"

    runner = CapturingRunner()
    result = rebar.llm.review_ticket(
        tid,
        "ticket-quality",
        ref="origin/main",
        source="attested",
        runner=runner,
        repo_root=str(repo),
    )
    # The gate ran against the pinned snapshot (verified_at_sha == origin/main), signable.
    assert result["source"] == "attested"
    assert result["verified_at_sha"] == main_sha
    assert result["signable"] is True
    # The read-root the runner actually saw is the snapshot, and it byte-matches origin/main
    # ("from-main") — NOT the checked-out "other" branch ("from-other").
    assert Path(runner.captured_repo_path) != repo
    assert runner.captured_sentinel == "from-main\n"


def test_attested_verdict_identical_across_checkout_branches(repo_with_origin, gate_tmpdir):
    repo, _main_sha = repo_with_origin
    tid = _ticket(repo)

    def run() -> str:
        runner = CapturingRunner()
        rebar.llm.review_ticket(
            tid,
            "ticket-quality",
            ref="origin/main",
            source="attested",
            runner=runner,
            repo_root=str(repo),
        )
        return runner.captured_sentinel

    on_other = run()
    _git(repo, "checkout", "-q", "main")  # move the shared checkout
    on_main = run()
    # The behavioral contract: identical read basis regardless of the checked-out branch.
    assert on_other == on_main == "from-main\n"


# --------------------------------------------------------------------------------------
# local mode reads the in-place checkout, flagged unsigned
# --------------------------------------------------------------------------------------
def test_local_mode_reads_checkout_and_is_unsigned(repo_with_origin, gate_tmpdir):
    repo, _main_sha = repo_with_origin
    tid = _ticket(repo)
    runner = CapturingRunner()
    result = rebar.llm.review_ticket(
        tid,
        "ticket-quality",
        source="local",
        runner=runner,
        repo_root=str(repo),
    )
    assert result["source"] == "local"
    assert result["signable"] is False
    assert result["verified_at_sha"] is None
    # local reads the in-place checkout (the dirty/other branch), NOT a snapshot.
    assert Path(runner.captured_repo_path) == repo
    assert runner.captured_sentinel == "from-other\n"


# --------------------------------------------------------------------------------------
# defaults resolve via config + REBAR_* env (not hardcoded)
# --------------------------------------------------------------------------------------
def test_defaults_resolve_via_env_and_config(monkeypatch, tmp_path):
    monkeypatch.delenv("REBAR_GATE_SOURCE", raising=False)
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)
    assert gate_source.default_ref() == "origin/main"
    assert gate_source.default_source() == "attested"
    monkeypatch.setenv("REBAR_GATE_REF", "release/v2")
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    assert gate_source.default_ref() == "release/v2"
    assert gate_source.default_source() == "local"


def test_defaults_resolve_from_snapshot_config_table(tmp_path, monkeypatch):
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "rebar.toml").write_text("[snapshot]\nref = 'develop'\n")
    assert gate_source.default_ref(repo_root=str(repo)) == "develop"


def test_invalid_configured_source_falls_back_to_attested(monkeypatch):
    monkeypatch.setenv("REBAR_GATE_SOURCE", "bogus")
    assert gate_source.default_source() == "attested"


def test_context_code_root_reroots_deep_from_env(tmp_path, monkeypatch):
    """The load-bearing mechanism the workflow-routed gates rely on: a config rebuilt deep
    in a gate run (e.g. gate_ops citation resolution) via LLMConfig.from_env reads the
    snapshot while the context root is active, and reverts cleanly afterward (AC3)."""
    from rebar.llm.config import LLMConfig, current_code_root, use_code_root

    monkeypatch.delenv("REBAR_LLM_REPO_PATH", raising=False)
    snap = str(tmp_path / "snap")
    assert current_code_root() is None
    with use_code_root(snap):
        assert current_code_root() == snap
        assert LLMConfig.from_env(repo_root=str(tmp_path)).repo_path == snap
    # Reverts: no gate active -> back to the checkout (prior behavior preserved).
    assert current_code_root() is None
    assert LLMConfig.from_env(repo_root=str(tmp_path)).repo_path == str(tmp_path)


# --------------------------------------------------------------------------------------
# the gate tolerates an absent/being-GC'd snapshot by re-materializing (ENOENT -> miss)
# --------------------------------------------------------------------------------------
def test_gate_rematerializes_absent_snapshot(repo_with_origin, gate_tmpdir):
    repo, main_sha = repo_with_origin
    h1 = gate_source.resolve_gate_handle("origin/main", "attested", str(repo))
    assert h1.path.is_dir()
    # Simulate the janitor having evicted the entry between gate runs.
    import shutil

    shutil.rmtree(h1.path)
    assert not h1.path.exists()
    h2 = gate_source.resolve_gate_handle("origin/main", "attested", str(repo))
    assert h2.path.is_dir()  # re-materialized, never a partial/absent read
    assert (h2.path / "sentinel.txt").read_text() == "from-main\n"
