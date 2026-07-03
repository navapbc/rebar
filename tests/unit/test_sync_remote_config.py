"""The tickets sync remote is configurable (``sync.remote``, default ``origin``) —
the remote counterpart to the ``tracker.branch`` migration (task rich-gravel-twain).
Split residency (code reviewed on a ``gerrit`` remote; the ``tickets`` branch's source
of truth on a ``github``/``origin`` remote) needs the store to stop hard-assuming
``origin`` is the ticket remote.

Covers the config layer (default / env / file / precedence / validation + the
``tickets_remote()`` helper) AND the git boundary — that the resolved remote name
actually reaches the ``git push`` subprocess (a captured git-args spy, plus a real
end-to-end push to a bare remote NAMED ``github`` rather than ``origin``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar import config as cfg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REBAR_CONFIG", "XDG_CONFIG_HOME", "REBAR_SYNC_REMOTE", "REBAR_SYNC_PUSH"):
        monkeypatch.delenv(name, raising=False)
    cfg.reset_config_cache()


def _proj(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


# ── config layer ──────────────────────────────────────────────────────────────
def test_default_remote_is_origin(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    assert cfg.load_config(root=p).sync.remote == "origin"
    assert cfg.tickets_remote(p) == "origin"  # helper mirrors the resolved value


def test_env_override_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "gerrit")
    p = _proj(tmp_path)
    assert cfg.load_config(root=p).sync.remote == "gerrit"
    assert cfg.tickets_remote(p) == "gerrit"


def test_config_file_remote(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\nremote = 'github'\n", encoding="utf-8")
    assert cfg.tickets_remote(p) == "github"


def test_env_beats_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[sync]\nremote = 'github'\n", encoding="utf-8")
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "gerrit")  # env layer beats project file
    assert cfg.tickets_remote(p) == "gerrit"


@pytest.mark.parametrize("bad", ["bad name", "-x", "a/b", "a..b", "with:colon", "t~ilde", ""])
def test_invalid_remote_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("REBAR_SYNC_REMOTE", bad)
    with pytest.raises(cfg.ConfigError):
        cfg.load_config(root=_proj(tmp_path))


@pytest.mark.parametrize("ok", ["origin", "gerrit", "github", "my-remote", "gerrit.example"])
def test_valid_remote_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: str) -> None:
    monkeypatch.setenv("REBAR_SYNC_REMOTE", ok)
    assert cfg.tickets_remote(_proj(tmp_path)) == ok


# ── git boundary: the resolved remote reaches `git push` ──────────────────────
def test_push_targets_configured_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy on push.py's git wrapper: the presence guard and the push must both name the
    CONFIGURED remote (not a hardcoded 'origin')."""
    from rebar._store import push

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    cfg.reset_config_cache()
    base_path = str(_proj(tmp_path) / ".tickets-tracker")

    calls: list[tuple[str, ...]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_git(_base: str, *args: str, **_kw: object) -> _R:
        calls.append(args)
        return _R()

    monkeypatch.setattr(push, "_git", _fake_git)
    push.push_tickets_branch(base_path)  # best-effort; never raises

    assert ("remote", "get-url", "github") in calls  # guard checks the configured remote
    push_calls = [a for a in calls if a and a[0] == "push"]
    assert push_calls, "no git push was attempted"
    assert push_calls[0][1] == "github"  # push target is the configured remote
    assert push_calls[0][2] == "HEAD:tickets"


def _git(d: Path, *a: str) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def test_push_reaches_nonorigin_remote_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real push to a bare remote NAMED ``github`` (no ``origin`` at all): with
    ``REBAR_SYNC_REMOTE=github`` the commit must land on that remote's ``tickets`` branch."""
    from rebar._store import push

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    cfg.reset_config_cache()

    github = tmp_path / "github.git"
    tracker = tmp_path / "tracker"
    subprocess.run(
        ["git", "init", "--bare", "-b", "tickets", str(github)], check=True, capture_output=True
    )
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    _git(tracker, "remote", "add", "github", str(github))  # NOTE: not 'origin'
    (tracker / "evt.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed")
    head = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    # The bare remote has no 'tickets' commit yet; push must advance it via the configured remote.
    push.push_tickets_branch(str(tracker))  # best-effort; never raises

    landed = _git(github, "rev-parse", "tickets").stdout.strip()
    assert landed == head, "push did not reach the configured (non-origin) 'github' remote"


def _bare_tickets_remote(tmp_path: Path, name: str = "github.git") -> Path:
    bare = tmp_path / name
    subprocess.run(
        ["git", "init", "--bare", "-b", "tickets", str(bare)], check=True, capture_output=True
    )
    return bare


def _tracker_with_remote(tmp_path: Path, bare: Path, *, remote: str, under: str) -> Path:
    """A tracker git dir on the `tickets` branch with one seed commit and a remote named
    `remote` → `bare`. `under` is the parent dir (config root; gets a .git so repo_root
    resolves)."""
    parent = tmp_path / under
    parent.mkdir()
    _git(parent, "init", "-q")  # config root
    tracker = parent / ".tickets-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "t@e.com")
    _git(tracker, "config", "user.name", "T")
    _git(tracker, "remote", "add", remote, str(bare))
    (tracker / "seed.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "seed")
    return tracker


def test_push_config_file_remote_end_to_end(tmp_path: Path) -> None:
    """The remote via a config FILE (`[sync] remote`) — not env — still reaches git push."""
    from rebar._store import push

    bare = _bare_tickets_remote(tmp_path)
    tracker = _tracker_with_remote(tmp_path, bare, remote="github", under="repo")
    (tracker.parent / "rebar.toml").write_text("[sync]\nremote = 'github'\n", encoding="utf-8")
    head = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    cfg.reset_config_cache()

    push.push_tickets_branch(str(tracker))  # best-effort; never raises
    assert _git(bare, "rev-parse", "tickets").stdout.strip() == head


def test_fsck_push_pending_names_configured_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsck's PUSH_PENDING notice measures 'ahead' against the CONFIGURED remote and names it."""
    from rebar._commands import fsck

    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    cfg.reset_config_cache()
    bare = _bare_tickets_remote(tmp_path)
    tracker = _tracker_with_remote(tmp_path, bare, remote="github", under="repo")
    _git(tracker, "push", "-q", "github", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "github")  # populate refs/remotes/github/tickets
    (tracker / "ahead.json").write_text("{}\n", encoding="utf-8")  # local now ahead by 1
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "-q", "-m", "ahead")

    notice = fsck._push_pending(str(tracker))
    assert notice is not None
    assert "github/tickets" in notice and "ahead" in notice


def test_reconverge_pulls_from_configured_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reconverge fetches + adopts new commits from the CONFIGURED remote (not origin)."""
    from rebar._store import sync

    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    monkeypatch.setenv("REBAR_SYNC_PULL", "on")
    cfg.reset_config_cache()
    bare = _bare_tickets_remote(tmp_path)
    tracker = _tracker_with_remote(tmp_path, bare, remote="github", under="repo")
    _git(tracker, "push", "-q", "github", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "github")

    # A second clone advances the shared remote.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@e.com")
    _git(other, "config", "user.name", "T")
    _git(other, "checkout", "-q", "tickets")
    (other / "remote_evt.json").write_text("{}\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote advance")
    _git(other, "push", "-q", "origin", "HEAD:tickets")
    remote_sha = _git(other, "rev-parse", "HEAD").stdout.strip()

    sync.reconverge(str(tracker))  # best-effort; fetches github + ff-adopts
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == remote_sha


def test_init_mount_uses_configured_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init's mount-or-create attaches to `<configured-remote>/tickets`, not origin/tickets."""
    from rebar._commands.init import _mount_or_create_branch

    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    cfg.reset_config_cache()
    bare = _bare_tickets_remote(tmp_path)
    # Seed the shared remote's `tickets` branch via a scratch clone.
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@e.com")
    _git(seed, "config", "user.name", "T")
    _git(seed, "checkout", "-q", "-b", "tickets")
    (seed / "evt.json").write_text("{}\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "HEAD:tickets")
    seed_sha = _git(seed, "rev-parse", "HEAD").stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "remote", "add", "github", str(bare))  # NOT origin
    _git(repo, "fetch", "-q", "github")  # populates github/tickets; no LOCAL tickets branch
    tracker = str(repo / "tracker")

    assert _mount_or_create_branch(str(repo), tracker) == 0
    assert _git(Path(tracker), "rev-parse", "HEAD").stdout.strip() == seed_sha


def test_materialize_tickets_resolves_configured_remote_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attested ticket-store materialization pins `<configured-remote>/tickets`."""
    from rebar._snapshot import repo_snapshot

    monkeypatch.setenv("REBAR_SYNC_REMOTE", "github")
    cfg.reset_config_cache()

    class _Stop(Exception):
        pass

    seen: dict[str, str] = {}

    def _fake_resolve(
        ref: str, repo_root: object = None, *, fetch: bool = True, remote: str = "origin"
    ):
        seen["ref"] = ref
        seen["remote"] = remote
        raise _Stop  # short-circuit before real FS materialization

    monkeypatch.setattr(repo_snapshot, "_has_remote", lambda root, remote="origin": True)
    monkeypatch.setattr(repo_snapshot, "resolve_ref", _fake_resolve)

    with pytest.raises(_Stop):
        repo_snapshot.materialize_tickets(repo_root=str(_proj(tmp_path)), fetch=True)
    assert seen["ref"] == "github/tickets"
    assert seen["remote"] == "github"
