"""HELD-OUT oracle for TS2 (09f9) — the implementation MUST NOT see this file.

The security-critical git-commit-ancestry key-validity behaviour (replacing HLC-epoch):
- the resolved key record binds real tickets-branch commit SHAs (added/revoked);
- an unsigned NON-genesis key-add is refused;
- validity respects the revocation boundary via commit ancestry (pass before, fail after);
- a BACKDATED filename timestamp does NOT change the governing commit — the killer test
  that commit-ancestry beats HLC backdating;
- intra-commit refinement orders a KEY event vs an event in the SAME commit by position;
- git failures are fail-closed (non-verified, never raise);
- replay never rejects and the HLC-epoch surface is gone; the reducer stays pure.
Observable behaviour only.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid as _uuid
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)  # keep events unfolded
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _tracker(store: Path) -> str:
    return str(tracker_dir(str(store)))


def _head(store: Path) -> str:
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_tracker(store),
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout.strip()


def _revoke_sig(ident: str, pub: str, priv: str):
    """A KEY_REVOKE signature by the (currently-valid) key being revoked."""
    return authorship.sign_authorship(
        authorship.keyop_payload("KEY_REVOKE", ident, pub), priv, principal=ident
    )


def _ticket_dir(store: Path, ticket_id: str) -> str:
    return os.path.join(_tracker(store), ticket_id)


# ── AC1: resolved record binds real commit SHAs ───────────────────────────────
def test_resolved_record_binds_commit_shas(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    add_commit = _head(store)
    rebar.revoke_identity_key(
        ident, pub, signature=_revoke_sig(ident, pub, priv), repo_root=str(store)
    )
    revoke_commit = _head(store)

    state = rebar.show_ticket(ident, repo_root=str(store))
    rec = next(r for r in state["keyring"] if r["public_key"] == pub)
    td = _ticket_dir(store, ident)
    assert authorship.resolve_event_commit(rec["added_at"], td, repo_root=str(store)) == add_commit
    assert (
        authorship.resolve_event_commit(rec["revoked_at"], td, repo_root=str(store))
        == revoke_commit
    )
    assert add_commit != revoke_commit


# ── AC2: unsigned non-genesis key-add refused ─────────────────────────────────
def test_unsigned_non_genesis_key_add_refused(store: Path, tmp_path: Path) -> None:
    _p1, pub1 = _keypair(tmp_path, "k1")
    _p2, pub2 = _keypair(tmp_path, "k2")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub1, signature=None, repo_root=str(store))  # genesis OK
    with pytest.raises(Exception):  # noqa: B017,PT011 — non-genesis add w/o signature must be refused
        rebar.add_identity_key(ident, pub2, signature=None, repo_root=str(store))


# ── AC3: commit-ancestry validity across the revocation boundary ──────────────
def test_commit_ancestry_boundary(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    rebar.create_ticket("task", "before revoke", repo_root=str(store))
    commit_before = _head(store)

    rebar.revoke_identity_key(
        ident, pub, signature=_revoke_sig(ident, pub, priv), repo_root=str(store)
    )

    rebar.create_ticket("task", "after revoke", repo_root=str(store))
    commit_after = _head(store)

    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    v_before = authorship.verify_authorship_at_commit(
        env, ident, commit_before, None, repo_root=str(store)
    )
    v_after = authorship.verify_authorship_at_commit(
        env, ident, commit_after, None, repo_root=str(store)
    )
    assert v_before.verified is True  # descends add, not revoke
    assert v_after.verified is False  # descends revoke


# ── AC4: backdated timestamp does not change the governing commit (KILLER) ─────
def test_backdated_timestamp_still_fails(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    rebar.revoke_identity_key(
        ident, pub, signature=_revoke_sig(ident, pub, priv), repo_root=str(store)
    )
    revoke_commit = _head(store)

    # Attacker forges an event file with a BACKDATED position, committed AFTER the revoke.
    tgt = rebar.create_ticket("task", "host", repo_root=str(store))
    td = _ticket_dir(store, tgt)
    backdated_pos = "000000000000000000-" + str(_uuid.uuid4())  # tiny HLC ts, sorts first
    fname = f"{backdated_pos}-COMMENT.json"
    ev = {
        "timestamp": "000000000000000000",
        "uuid": backdated_pos.split("-", 1)[1],
        "event_type": "COMMENT",
        "author": "attacker",
        "data": {"body": "forged"},
    }
    Path(td, fname).write_text(json.dumps(ev), encoding="utf-8")
    tracker = _tracker(store)
    subprocess.run(
        ["git", "-C", tracker, "add", "--", os.path.relpath(Path(td, fname), tracker)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", "forged"],
        check=True,
        capture_output=True,
    )
    forged_commit = _head(store)

    # The resolver maps the FILE to its real introducing commit, NOT the backdated ts.
    resolved = authorship.resolve_event_commit(backdated_pos, td, repo_root=str(store))
    assert resolved == forged_commit
    # forged_commit descends the revocation → the backdated event fails.
    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    v = authorship.verify_authorship_at_commit(
        env, ident, forged_commit, backdated_pos, repo_root=str(store)
    )
    assert v.verified is False
    assert forged_commit != revoke_commit


# ── AC5: intra-commit refinement (same commit, ordered by position) ───────────
def _batched_keyadd_and_event(
    store: Path, ident: str, pub: str, *, key_first: bool
) -> tuple[str, str]:
    """Commit a KEY_ADD (for pub) and a marker event in ONE commit. Returns
    (commit_sha, marker_position). key_first controls intra-commit ordering."""
    from rebar._commands import _seam
    from rebar._store.event_append import batch_stage_and_commit

    buf: list = []
    with _seam.batch_sink(buf):
        if key_first:
            rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
            marker = rebar.create_ticket("task", "marker", repo_root=str(store))
        else:
            marker = rebar.create_ticket("task", "marker", repo_root=str(store))
            rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    batch_stage_and_commit(_tracker(store), buf)
    commit = _head(store)
    # marker position = the CREATE event of the marker ticket
    marker_pos = next(
        f[: f.rindex("-")]
        for f in os.listdir(_ticket_dir(store, marker))
        if f.endswith("-CREATE.json")
    )
    return commit, marker_pos


def test_intra_commit_key_added_before_event_valid(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    commit, marker_pos = _batched_keyadd_and_event(store, ident, pub, key_first=True)
    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    v = authorship.verify_authorship_at_commit(env, ident, commit, marker_pos, repo_root=str(store))
    assert v.verified is True  # KEY_ADD position < event position in same commit


def test_intra_commit_key_added_after_event_invalid(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    commit, marker_pos = _batched_keyadd_and_event(store, ident, pub, key_first=False)
    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    v = authorship.verify_authorship_at_commit(env, ident, commit, marker_pos, repo_root=str(store))
    assert v.verified is False  # KEY_ADD position > event position in same commit


def test_intra_commit_key_revoked_before_event_invalid(store: Path, tmp_path: Path) -> None:
    """Symmetric revoke side: a KEY_REVOKE and an event in the SAME commit, with the
    revoke at an EARLIER intra-commit position than the event, invalidates the event."""
    from rebar._commands import _seam
    from rebar._store.event_append import batch_stage_and_commit

    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    # Add the key in an earlier commit so it is valid up to the revoke.
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    # ONE commit containing KEY_REVOKE (earlier position) then a marker event (later).
    revoke_sig = _revoke_sig(ident, pub, priv)
    buf: list = []
    with _seam.batch_sink(buf):
        rebar.revoke_identity_key(ident, pub, signature=revoke_sig, repo_root=str(store))
        marker = rebar.create_ticket("task", "marker", repo_root=str(store))
    batch_stage_and_commit(_tracker(store), buf)
    commit = _head(store)
    marker_pos = next(
        f[: f.rindex("-")]
        for f in os.listdir(_ticket_dir(store, marker))
        if f.endswith("-CREATE.json")
    )

    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    v = authorship.verify_authorship_at_commit(env, ident, commit, marker_pos, repo_root=str(store))
    assert v.verified is False  # KEY_REVOKE position <= event position in same commit


# ── AC6: fail-closed git handling ─────────────────────────────────────────────
def test_fail_closed_on_unresolvable_commit(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    env = authorship.sign_authorship(b'{"uuid":"e","data":{}}', priv, principal=ident)
    bogus = "0" * 40  # a commit that does not exist → merge-base fails
    v = authorship.verify_authorship_at_commit(env, ident, bogus, None, repo_root=str(store))
    assert v.verified is False  # non-verified, and (implicitly) no exception raised


# ── AC7: replay never rejects + HLC-epoch surface gone ────────────────────────
def test_replay_never_rejects(store: Path, tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "k")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    rebar.revoke_identity_key(
        ident, pub, signature=_revoke_sig(ident, pub, priv), repo_root=str(store)
    )
    # Reduction succeeds and yields a well-formed state despite lifecycle churn.
    state = rebar.show_ticket(ident, repo_root=str(store))
    assert isinstance(state, dict) and state["ticket_type"] == "identity"
    assert isinstance(state.get("keyring"), list)


def _src_root() -> Path:
    return Path(rebar.__file__).resolve().parent


def test_hlc_epoch_surface_absent() -> None:
    """The HLC-epoch API (functions) and the epoch record FIELDS written into a keyring
    are gone. Migration code that READS/pops a stale `keyring_epoch` and changelog
    comments that name the old model in prose are tolerated — the ban targets live
    epoch logic, not history."""
    root = _src_root()
    # Removed functions: must not appear anywhere as live symbols.
    banned_functions = ["verify_authorship_at_epoch", "epoch_for_position", "keys_valid_at_epoch"]
    # Epoch record fields written as dict keys (the fold building an epoch-based record).
    banned_record_fields = ['"added_epoch"', '"revoked_epoch"']
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for sym in banned_functions + banned_record_fields:
            if sym in text:
                hits.append(f"{path}: {sym}")
    assert not hits, f"HLC-epoch surface still present: {hits}"


# ── AC8: reducer stays pure (no git/subprocess) ───────────────────────────────
def test_reducer_stays_pure() -> None:
    reducer_dir = _src_root() / "reducer"
    offenders: list[str] = []
    for path in reducer_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import subprocess" in text or "merge-base" in text or "--diff-filter" in text:
            offenders.append(str(path))
    assert not offenders, f"reducer must stay pure (no git/subprocess): {offenders}"
