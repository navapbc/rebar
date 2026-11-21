"""The op-cert merge gate reads its trust configuration from the reviewed base branch.

Bug golden-busied-nag: ``verify.trusted_opcert_keys`` is an ordinary ``[verify]`` key, so a
change under review that adds its own key to the committed ``rebar.toml`` widened the trust
list of the very ``rebar verify-opcert`` run that checked it. The gate now composes its
configuration with the project layer read from ``origin/main``, never
from the working tree. These tests try the attack end to end, then pin the resolution rules.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import config, signing
from rebar._opcert_gate_config import base_ref, gate_config
from rebar._operation_config import bind_operation_snapshot
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

ENV_ID = "trusted-ci@rebar.test"
_VERIFY = (sys.executable, "-m", "rebar", "verify-opcert")
_BASE_SOURCE = re.compile(r"refs/remotes/origin/main:rebar\.toml")
KIND = "completion-verifier"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _trust_toml(*pubs: str) -> str:
    entries = ", ".join(f'"{ENV_ID} {pub}"' for pub in pubs)
    return f"[verify]\ntrusted_opcert_keys = [{entries}]\n"


def _repo_with_origin(tmp_path: Path, main_toml: str | None) -> Path:
    """A checkout whose ``origin/main`` carries ``main_toml`` as its committed rebar.toml."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "d@e.test")
    _git(repo, "config", "user.name", "D")
    if main_toml is not None:
        (repo / "rebar.toml").write_text(main_toml, encoding="utf-8")
        _git(repo, "add", "rebar.toml")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return repo


def _propose_change(repo: Path, toml: str) -> None:
    """Commit ``toml`` as rebar.toml on a change branch that has not merged."""
    _git(repo, "checkout", "-q", "-b", "change")
    (repo / "rebar.toml").write_text(toml, encoding="utf-8")
    _git(repo, "add", "rebar.toml")
    _git(repo, "commit", "-q", "-m", "widen trust")


@pytest.fixture
def _clean_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv("REBAR_VERIFY_TRUSTED_OPCERT_KEYS", raising=False)
    monkeypatch.delenv("REBAR_SYNC_REMOTE", raising=False)
    config.reset_config_cache()
    yield
    config.reset_config_cache()


def _self_trusted_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main trusts key A; an unmerged change lists its own key B in rebar.toml and closes a
    ticket under a B-signed completion certificate."""
    _priv_a, pub_a = _keypair(tmp_path, "main-key")
    priv_b, pub_b = _keypair(tmp_path, "change-key")
    repo = _repo_with_origin(tmp_path, _trust_toml(pub_a))
    _propose_change(repo, _trust_toml(pub_a, pub_b))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    from rebar.llm.plan_review.attest import current_material_fingerprint

    tid = rebar.create_ticket("task", "self-trusted work", repo_root=str(repo))
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=current_material_fingerprint(tid, repo_root=str(repo)),
        merged_log_commit=_git(repo, "rev-parse", "HEAD"),
        key_path=priv_b,
        principal=ENV_ID,
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "closed", repo_root=str(repo))
    return repo


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_change_adding_its_own_key_does_not_widen_its_gate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    """The attack: main trusts key A; the change lists its own key B and closes a ticket under
    a B-signed certificate. Its own gate run must still trust only A (exit 1, `untrusted`). Once
    the same rebar.toml has merged to main, the gate trusts B (exit 0)."""
    repo = _self_trusted_change(tmp_path, monkeypatch)

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            [*_VERIFY, "--root", str(repo), "--require-environment", ENV_ID],
            capture_output=True,
            text=True,
            check=False,
        )

    proc = run()
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "untrusted" in proc.stdout, proc.stdout
    assert _BASE_SOURCE.search(proc.stderr), proc.stderr

    # After review: the change merges to main, and the same key now gates.
    _git(repo, "push", "-q", "origin", "change:main")
    _git(repo, "fetch", "-q", "origin")
    proc = run()
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_gate_run_never_needs_the_working_tree_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """Any working-tree project-config read during the gate run raises here. Reads that could
    matter would break the verdict; the gate must still reach main's `untrusted` (exit 1)."""
    from rebar import _config_sources
    from rebar._commands.verify_opcert import cli

    repo = _self_trusted_change(tmp_path, monkeypatch)
    config.reset_config_cache()

    def refuse(_root=None):
        raise AssertionError("the gate read the working-tree project config")

    monkeypatch.setattr(_config_sources, "_discover_project_config", refuse)
    # Defeat the 60 s freshness throttle so each live read would reach its sync.pull lookup.
    from rebar._engine_support import reads as _reads

    monkeypatch.setattr(_reads, "_sync_marker_age", lambda _tracker, _now: ("", 10**9))
    monkeypatch.chdir(repo)
    capsys.readouterr()

    rc = cli(["--root", str(repo), "--require-environment", ENV_ID])

    out = capsys.readouterr()
    assert rc == 1, f"stdout={out.out}\nstderr={out.err}"
    assert "untrusted" in out.out, out.out


def test_unfetchable_blob_on_main_fails_closed(tmp_path: Path, _clean_config: None) -> None:
    """In a blobless clone, a main rebar.toml whose blob cannot be fetched must not read as
    absent (which would drop main's require_environment); it is a ConfigError."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "uploadpack.allowFilter", "true")
    (source / "rebar.toml").write_text('[verify]\nrequire_environment = "box"\n', "utf-8")
    _git(source, "add", "rebar.toml")
    _git(source, "-c", "user.email=d@e.test", "-c", "user.name=D", "commit", "-q", "-m", "m")
    clone = tmp_path / "clone"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--no-checkout",
        "--filter=blob:none",
        f"file://{source}",
        str(clone),
    )
    source.rename(tmp_path / "gone")  # the promisor remote is now unreachable

    with pytest.raises(config.ConfigError, match="could not read"):
        gate_config(str(clone))


def test_gate_config_ignores_the_working_tree_project_config(
    tmp_path: Path, _clean_config: None
) -> None:
    repo = _repo_with_origin(tmp_path, _trust_toml("ssh-ed25519 AAAAmain"))
    _propose_change(repo, _trust_toml("ssh-ed25519 AAAAmain", "ssh-ed25519 AAAAchng"))
    (repo / "rebar.toml").write_text(_trust_toml("ssh-ed25519 AAAAdrty"), encoding="utf-8")

    cfg, source, _snapshot = gate_config(str(repo))

    assert cfg.verify.trusted_opcert_keys == (f"{ENV_ID} ssh-ed25519 AAAAmain",)
    assert _BASE_SOURCE.fullmatch(source), source


def test_change_cannot_drop_main_require_environment(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_with_origin(tmp_path, '[verify]\nrequire_environment = "box"\n')
    _propose_change(repo, "[verify]\n")

    cfg, _source, _snapshot = gate_config(str(repo))

    assert cfg.verify.require_environment == "box"


def test_environment_trust_list_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    """ADR 0125 puts the list in the verifying environment; that layer is the gate's own."""
    repo = _repo_with_origin(tmp_path, None)
    monkeypatch.setenv("REBAR_VERIFY_TRUSTED_OPCERT_KEYS", f"{ENV_ID} ssh-ed25519 AAAAenvv")

    cfg, source, _snapshot = gate_config(str(repo))

    assert cfg.verify.trusted_opcert_keys == (f"{ENV_ID} ssh-ed25519 AAAAenvv",)
    assert source.endswith(" (no committed project config)"), source


def test_pyproject_tool_rebar_on_main_is_the_project_layer(
    tmp_path: Path, _clean_config: None
) -> None:
    repo = _repo_with_origin(tmp_path, None)
    (repo / "pyproject.toml").write_text(
        '[tool.rebar.verify]\nrequire_environment = "box"\n', encoding="utf-8"
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-q", "-m", "config")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")

    cfg, source, _snapshot = gate_config(str(repo))

    assert cfg.verify.require_environment == "box"
    assert source.endswith(":pyproject.toml")


def test_change_cannot_redirect_the_store_it_walks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    """A change pointing ``tracker.dir`` at an empty directory would leave nothing in scope.
    Under the bound gate snapshot every store lookup resolves main's location instead."""
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    repo = _repo_with_origin(tmp_path, None)
    _propose_change(repo, '[tracker]\ndir = "empty-store"\n')
    root = str(repo)
    assert config.tracker_dir(root) == repo.resolve() / "empty-store"  # the attack, unbound

    cfg, _source, snapshot = gate_config(root)

    assert cfg.tracker.dir == ".tickets-tracker"
    with bind_operation_snapshot(snapshot):
        assert config.tracker_dir(root) == repo.resolve() / ".tickets-tracker"


def test_remote_without_a_fetched_base_fails_closed(tmp_path: Path, _clean_config: None) -> None:
    """Falling back to the working tree here would hand the change its own gate."""
    repo = _repo_with_origin(tmp_path, None)
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")

    with pytest.raises(config.ConfigError, match="reviewed base branch"):
        gate_config(str(repo))
    proc = subprocess.run(
        [*_VERIFY, "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "git fetch origin" in proc.stderr


def test_checkout_without_a_remote_keeps_the_working_tree(
    tmp_path: Path, _clean_config: None
) -> None:
    """No remote means no reviewed branch and no change under review (a local-only project)."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "rebar.toml").write_text('[verify]\nrequire_environment = "box"\n', encoding="utf-8")

    cfg, source, _snapshot = gate_config(str(repo))

    assert base_ref(repo) is None
    assert cfg.verify.require_environment == "box"
    assert "no git remote" in source


# ---- base resolution (bug floating-guiltless-oxpecker) ------------------------------------------


def _repo_on_branch(tmp_path: Path, branch: str, toml: str, remote: str = "origin") -> Path:
    """A checkout whose ``<remote>`` has only ``branch``, recorded as ``<remote>/HEAD``."""
    bare = tmp_path / f"{remote}.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", branch, str(bare))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "d@e.test")
    _git(repo, "config", "user.name", "D")
    (repo / "rebar.toml").write_text(toml, encoding="utf-8")
    _git(repo, "add", "rebar.toml")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", remote, str(bare))
    _git(repo, "push", "-q", remote, branch)
    _git(repo, "fetch", "-q", remote)
    _git(repo, "remote", "set-head", remote, branch)
    return repo


_BOX = '[verify]\nrequire_environment = "box"\n'


def _push_branch(repo: Path, branch: str, toml: str, remote: str = "origin") -> None:
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "rebar.toml").write_text(toml, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", branch)
    _git(repo, "push", "-q", remote, branch)
    _git(repo, "fetch", "-q", remote)


def test_master_default_branch_is_the_base(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "master", _BOX)

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/master:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_master_default_branch_cli_does_not_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    repo = _repo_on_branch(tmp_path, "master", "[verify]\n")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    proc = subprocess.run(
        [*_VERIFY, "--root", str(repo)], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, proc.stderr
    assert "refs/remotes/origin/master:rebar.toml" in proc.stderr


def test_environment_sync_remote_selects_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    repo = _repo_on_branch(tmp_path, "trunk", _BOX, remote="upstream")
    _git(repo, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "upstream")

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/upstream/trunk:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_change_controlled_base_selectors_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    """REBAR_GATE_REF=HEAD, a working-tree [snapshot].ref and [sync].remote all point at
    the change; none may move the base."""
    repo = _repo_on_branch(tmp_path, "main", _BOX)
    _git(repo, "remote", "add", "fork", str(tmp_path / "origin.git"))
    _git(repo, "fetch", "-q", "fork")
    _propose_change(repo, '[snapshot]\nref = "HEAD"\n[sync]\nremote = "fork"\n[verify]\n')
    monkeypatch.setenv("REBAR_GATE_REF", "HEAD")

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/main:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_committed_snapshot_ref_is_followed_once(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "main", '[snapshot]\nref = "origin/release"\n')
    _push_branch(repo, "release", f'[snapshot]\nref = "origin/later"\n{_BOX}')
    _push_branch(repo, "later", '[verify]\nrequire_environment = "later"\n')

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/release:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_committed_snapshot_ref_of_head_is_ignored(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "main", f'[snapshot]\nref = "HEAD"\n{_BOX}')

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/main:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_unfetched_hop_target_fails_closed(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "main", '[snapshot]\nref = "origin/release"\n')

    with pytest.raises(config.ConfigError, match="refs/remotes/origin/release, which is not"):
        gate_config(str(repo))


def test_dangling_origin_head_falls_through_to_main(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "main", _BOX)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone")

    _cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/main:rebar.toml"


def test_missing_environment_remote_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_config: None
) -> None:
    repo = _repo_on_branch(tmp_path, "main", _BOX)
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "upstream")

    with pytest.raises(config.ConfigError, match="'upstream' is not a configured git remote"):
        gate_config(str(repo))


def test_no_resolvable_base_names_what_was_tried(tmp_path: Path, _clean_config: None) -> None:
    repo = _repo_on_branch(tmp_path, "trunk", _BOX)
    _git(repo, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")

    with pytest.raises(config.ConfigError) as err:
        gate_config(str(repo))
    assert "refs/remotes/origin/HEAD and refs/remotes/origin/main" in str(err.value)
    assert "git remote set-head origin --auto" in str(err.value)
