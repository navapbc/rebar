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
def test_unreadable_ticket_with_any_close_in_history_is_judged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """Closed then reopened: the gate cannot tell which branch of a STATUS fork the reducer
    keeps when the reduce itself failed, so any close in the history means it is judged."""
    repo, closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    rebar.reopen(closed, repo_root=str(repo))
    _break_show_ticket(monkeypatch, closed)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{closed}: {_UNREADABLE}" in text, text


def _write_event(repo: Path, ticket_id: str, kind: str, data: dict) -> None:
    """Drop one raw event file into a ticket directory (bypassing the writer on purpose)."""
    import json
    import uuid

    ticket_dir = Path(str(config.tracker_dir(str(repo)))) / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    stamp = 9 * 10**18
    eid = str(uuid.uuid4())
    event = {"event_type": kind, "timestamp": stamp, "uuid": eid, "data": data}
    (ticket_dir / f"{stamp}-{eid}-{kind}.json").write_text(json.dumps(event), encoding="utf-8")


def _in_progress_ticket(repo: Path) -> str:
    tid = rebar.create_ticket("task", "in progress work", repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("SNAPSHOT", {"compiled_state": {"status": "in_progress"}, "source_event_uuids": []}),
        ("EDIT", {"fields": {"status": "closed"}}),
        ("CREATE", {"status": "closed", "ticket_type": "task", "title": "x"}),
    ],
)
def test_unreadable_ticket_that_may_be_closed_is_judged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    data: dict,
    _clean_config: None,
) -> None:
    """A SNAPSHOT (compacted, possibly from unsigned events the walk never sees), an EDIT
    writing status=closed, or a CREATE born closed all leave a chance the ticket is closed."""
    repo, _closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    tid = _in_progress_ticket(repo)
    _write_event(repo, tid, kind, data)
    _break_show_ticket(monkeypatch, tid)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{tid}: {_UNREADABLE}" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_ticket_absent_from_the_walk_is_still_enumerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """A ticket whose only file is a SNAPSHOT with an empty authorship ledger yields no walked
    event; listing the store directory still brings it into scope."""
    repo, _closed, _open = _closed_ticket_repo(tmp_path, monkeypatch)
    ghost = "0000-1111-2222-3333"
    _write_event(repo, ghost, "SNAPSHOT", {"compiled_state": {"status": "closed"}})
    _break_show_ticket(monkeypatch, ghost)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert rc == 1, text
    assert f"{ghost}: {_UNREADABLE}" in text, text


@pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")
def test_unreadable_never_closed_ticket_stays_out_of_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _clean_config: None,
) -> None:
    """Only a CREATE born open and no other event: the raw files prove it never closed."""
    repo, _closed, still_open = _closed_ticket_repo(tmp_path, monkeypatch)
    _break_show_ticket(monkeypatch, still_open)

    rc, text = _gate(capsys, repo, "--require-environment", ENV_ID)

    assert still_open not in text, text
    assert "(1 closed ticket(s) in scope)" in text, text
    assert rc == 1, text  # the genuinely closed ticket still lacks a certificate


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


@pytest.mark.parametrize("value", ["HEAD", "origin/HEAD", "fork/release", "0123456789abcdef"])
def test_committed_snapshot_ref_off_the_trusted_remote_fails_closed(
    tmp_path: Path, value: str, _clean_config: None
) -> None:
    """The hop never leaves the base's own remote: HEAD, a SHA or another remote (a fork)
    in the base's committed [snapshot].ref is a ConfigError, not a silent fallback."""
    repo = _repo_on_branch(tmp_path, "main", f'[snapshot]\nref = "{value}"\n{_BOX}')
    _git(repo, "remote", "add", "fork", str(tmp_path / "origin.git"))
    _git(repo, "fetch", "-q", "fork")

    with pytest.raises(config.ConfigError, match="must name a branch of the gate's remote"):
        gate_config(str(repo))


def test_committed_bare_branch_name_hops_on_the_trusted_remote(
    tmp_path: Path, _clean_config: None
) -> None:
    repo = _repo_on_branch(tmp_path, "main", '[snapshot]\nref = "release"\n')
    _push_branch(repo, "release", _BOX)

    cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/release:rebar.toml"
    assert cfg.verify.require_environment == "box"


def test_committed_ref_naming_the_base_itself_is_no_hop(
    tmp_path: Path, _clean_config: None
) -> None:
    repo = _repo_on_branch(tmp_path, "main", f'[snapshot]\nref = "origin/main"\n{_BOX}')

    _cfg, source, _snapshot = gate_config(str(repo))

    assert source == "refs/remotes/origin/main:rebar.toml"


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
