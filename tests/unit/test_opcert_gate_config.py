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
_UNREADABLE = "unreadable ticket state (RuntimeError: cannot reduce this ticket)"
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


def _closed_ticket_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A remote-backed store whose main trusts one key, with one closed ticket and one open."""
    _priv, pub = _keypair(tmp_path, "main-key")
    repo = _repo_with_origin(tmp_path, _trust_toml(pub))
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    closed = rebar.create_ticket("task", "closed work", repo_root=str(repo))
    rebar.transition(closed, "open", "closed", repo_root=str(repo))
    still_open = rebar.create_ticket("task", "open work", repo_root=str(repo))
    return repo, closed, still_open


def _break_show_ticket(monkeypatch: pytest.MonkeyPatch, *ticket_ids: str) -> None:
    real = rebar.show_ticket
    broken = set(ticket_ids)

    def show(ticket_id, **kw):
        if ticket_id in broken:
            raise RuntimeError("cannot reduce this ticket")
        return real(ticket_id, **kw)

    monkeypatch.setattr(rebar, "show_ticket", show)


def _gate(capsys: pytest.CaptureFixture[str], repo: Path, *extra: str) -> tuple[int, str]:
    from rebar._commands.verify_opcert import cli

    capsys.readouterr()
    rc = cli(["--root", str(repo), *extra])
    out = capsys.readouterr()
    return rc, out.out + out.err


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_closed_ticket_fails_an_enforcing_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """Bug scarlet-unsocial-limpkin: a closed ticket whose state cannot be reduced used to drop
    out of scope, so an enforcing gate printed OK and exited 0."""
    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{closed}: {_UNREADABLE}" in text, text
    assert "(1 closed ticket(s) in scope)" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_closed_ticket_is_reported_in_advisory_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo)

    assert rc == 0, text
    assert f"{closed}: {_UNREADABLE}" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_closed_ticket_before_the_boundary_is_grandfathered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    rebar.create_ticket("task", "later activity", repo_root=str(repo))
    since = _git(Path(str(config.tracker_dir(str(repo)))), "rev-parse", "HEAD")
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID, "--since", since)

    assert rc == 0, text
    assert f"{closed}: {_UNREADABLE} [grandfathered]" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_ticket_proven_open_stays_out_of_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """The latest STATUS proves the ticket is not closed, so it is not judged."""
    repo, _closed, still_open = _closed_ticket_repo(tmp_path, monkeypatch)
    rebar.transition(still_open, "open", "in_progress", repo_root=str(repo))
    _break_show_ticket(monkeypatch, still_open)

    rc, text = _gate(capsys, repo)

    assert rc == 0, text
    assert still_open not in text, text
    assert "unreadable" not in text, text
    assert "(1 closed ticket(s) in scope)" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_reopened_ticket_stays_out_of_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """Closed then reopened: the latest STATUS proves it is not closed now."""
    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    rebar.reopen(closed, repo_root=str(repo))
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 0, text
    assert "(0 closed ticket(s) in scope)" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_compacted_closed_ticket_fails_an_enforcing_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """After compaction the close STATUS lives only in a SNAPSHOT, which the merged-log walk
    sees as a ledger entry with no event. Unknown status must be judged, not skipped."""
    from rebar._commands import verify_authorship

    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    real_collect = verify_authorship._collect_all

    def compacted(tracker):
        out = []
        for ev in real_collect(tracker):
            if ev.ticket_id == closed:
                ev.event = None  # what a SNAPSHOT ledger entry looks like to the gate
            out.append(ev)
        return out

    monkeypatch.setattr(verify_authorship, "_collect_all", compacted)
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{closed}: unreadable ticket state" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_ticket_with_no_status_event_is_judged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """No STATUS event proves nothing about closure, so the gate judges it (fails closed)."""
    repo, _closed, still_open = _closed_ticket_repo(tmp_path, monkeypatch)
    _break_show_ticket(monkeypatch, still_open)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{still_open}: {_UNREADABLE}" in text, text
