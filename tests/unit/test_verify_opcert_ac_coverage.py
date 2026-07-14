"""Acceptance-test coverage the completion verifier flagged as MISSING for the op-cert merge-gate
(story 4214 / Option B, era-at-storage-anchor).

Every test asserts OBSERVABLE behaviour — `rebar verify-opcert` subprocess exit codes / verdict
strings, or a public verifier verdict — never internals. The harness (real ssh-keygen, a real rebar
store, the real `rebar verify-opcert` subprocess) mirrors the sibling op-cert suites.

Coverage added here:
* the `merged_log_commit` binding constraint (off-history commit rejected; plaintext-mirror
  invariance);
* rollback sub-cases (grandfathered rotation PASSES; kill-switch revocation FAILS all certs;
  fail-closed on an unresolvable storage anchor);
* the `rebar trusted-env add|revoke` helper stamping the tickets-branch tip log position.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.attest import opcert, sshsig
from rebar.llm.plan_review.attest import current_material_fingerprint

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
KIND = "completion-verifier"
MATERIAL = "0123456789abcdef"
MERGED = "0" * 40


# ---- harness (identical to the sibling op-cert suites) ----------------------------------------


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _tracker(repo: Path) -> str:
    from rebar._commands._seam import tracker_dir

    return str(tracker_dir(str(repo)))


def _tip_position(repo: Path) -> str:
    """Greatest active-event log position on the tickets branch ({ts}-{uuid}); the same rule the
    gate and `trusted-env` use to stamp positions."""
    from rebar.reducer._cache import is_active_event

    tracker = _tracker(repo)
    best = ""
    for d in os.listdir(tracker):
        dp = os.path.join(tracker, d)
        if d.startswith(".") or not os.path.isdir(dp):
            continue
        for fn in os.listdir(dp):
            if not fn.endswith(".json") or fn.startswith(".") or not is_active_event(fn):
                continue
            pos = fn[:-5].rsplit("-", 1)[0]
            if pos > best:
                best = pos
    return best


def _write_env(repo: Path, pub: str, added_at: str, revoked_at: str | None = None) -> None:
    d = repo / ".rebar"
    d.mkdir(exist_ok=True)
    rev = "null" if revoked_at is None else revoked_at
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {ENV_ID}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at}\n"
        f"        revoked_at_log_position: {rev}\n",
        encoding="utf-8",
    )


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "verify-opcert", "--root", str(repo), *extra], capture_output=True, text=True
    )


def _sign(repo: Path, tid: str, priv: str, *, material: str, merged_log_commit: str) -> None:
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=material,
        merged_log_commit=merged_log_commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(repo),
    )


def _sig_event_file(repo: Path) -> Path:
    """The stored terminal SIGNATURE event JSON file under the tracker."""
    tracker = _tracker(repo)
    found: Path | None = None
    for root, _dirs, files in os.walk(tracker):
        if ".git" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith("SIGNATURE.json"):
                found = Path(root) / f
    assert found is not None, "no SIGNATURE event file found under the tracker"
    return found


def _commit_tracker(repo: Path, msg: str) -> None:
    """Commit the current tracker working tree on the tickets branch (verify-opcert reads committed
    state), replicating the minimal `git add`+`commit` the store's write path performs."""
    tracker = _tracker(repo)
    subprocess.run(["git", "-C", tracker, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", tracker, "commit", "-q", "--no-verify", "-m", msg],
        check=True,
        capture_output=True,
    )


# ==== 1. merged_log_commit constraint ==========================================================


def test_merged_log_commit_off_history_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(1a) A cert whose bound `merged_log_commit` is NOT an ancestor of the gated main history (a
    fabricated 40-hex SHA) FAILS closed (exit 1). Material is the ticket's AUTHORITATIVE fingerprint
    so the failure is specifically the commit constraint (`_commit_in_gated_history`), observable in
    the verdict string — not a material mismatch."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    _write_env(repo, pub, _tip_position(repo))
    material = current_material_fingerprint(tid, repo_root=str(repo))  # authoritative
    fabricated = "a1b2c3d4" * 5  # a random 40-hex SHA that is not a commit in the history
    _sign(repo, tid, priv, material=material, merged_log_commit=fabricated)
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # Observable reason: the merged-log commit constraint, not a material/era problem.
    assert "not in the gated main history" in proc.stdout, proc.stdout


def test_plaintext_mirror_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(1b) A VALID cert (envelope binds the correct material + a valid ancestor merged_log_commit)
    must remain valid (exit 0) even after the stored record's PLAINTEXT `material_fingerprint` and
    `merged_log_commit` mirror fields are corrupted, because the gate recomputes material and should
    read the commit from the VERIFIED envelope payload — never trusting the (tickets-branch,
    non-Gerrit-gated) plaintext mirror."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    head = _head(repo)
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    _write_env(repo, pub, _tip_position(repo))
    material = current_material_fingerprint(tid, repo_root=str(repo))
    _sign(repo, tid, priv, material=material, merged_log_commit=head)  # valid ancestor commit
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    before = _run(repo, "--require-environment", ENV_ID)
    assert before.returncode == 0, f"precondition (valid cert): {before.stdout}\n{before.stderr}"

    # Corrupt ONLY the plaintext mirror fields; leave the DSSE envelope byte-intact.
    sig = _sig_event_file(repo)
    obj = json.loads(sig.read_text(encoding="utf-8"))
    orig_mf = obj["data"]["material_fingerprint"]
    orig_mc = obj["data"]["merged_log_commit"]
    orig_env = obj["data"]["envelope"]
    obj["data"]["material_fingerprint"] = "deadbeef" * 2
    obj["data"]["merged_log_commit"] = "f" * 40
    sig.write_text(json.dumps(obj), encoding="utf-8")
    _commit_tracker(repo, "corrupt plaintext op-cert mirror fields (envelope untouched)")

    # Prove the precondition: the plaintext really changed on disk, the envelope did not — so a
    # pass below cannot be vacuous (reading a still-pristine record).
    reread = json.loads(_sig_event_file(repo).read_text(encoding="utf-8"))["data"]
    assert reread["material_fingerprint"] != orig_mf
    assert reread["merged_log_commit"] != orig_mc
    assert reread["envelope"] == orig_env

    after = _run(repo, "--require-environment", ENV_ID)
    assert after.returncode == 0, (
        "plaintext-mirror invariance violated: corrupting the plaintext merged_log_commit / "
        f"material_fingerprint changed the verdict. stdout={after.stdout}\nstderr={after.stderr}"
    )


# ==== 2. rollback sub-cases ====================================================================


def test_revocation_after_storage_anchor_grandfathers_cert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(2a) Routine rotation: a key REVOKED at a log position AFTER the cert's storage anchor S
    (revocation stamped later than the SIGNATURE event) leaves the earlier cert VALID at S → the
    cert PASSES (exit 0). Earlier certs are grandfathered across a rotation."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    added_pos = _tip_position(repo)
    material = current_material_fingerprint(tid, repo_root=str(repo))
    # Sign+close FIRST so the storage anchor S is stamped now, BEFORE the revocation position.
    _sign(repo, tid, priv, material=material, merged_log_commit=_head(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))
    # Advance the tickets log, then revoke at a LATER position (a descendant of S).
    rebar.create_ticket("task", "later activity advancing the log", repo_root=str(repo))
    revoke_pos = _tip_position(repo)
    assert revoke_pos > added_pos
    _write_env(repo, pub, added_pos, revoked_at=revoke_pos)

    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_kill_switch_revocation_fails_all_certs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(2b) Compromise kill-switch: a key whose `revoked_at_log_position` is at/before its own
    `added_at_log_position` (here both are the earliest position, an ancestor of every storage
    anchor) is invalid at EVERY anchor → every cert it signed FAILS (exit 1)."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "gated", repo_root=str(repo))
    earliest = _tip_position(repo)  # ancestor of the later SIGNATURE storage anchor
    material = current_material_fingerprint(tid, repo_root=str(repo))
    _sign(repo, tid, priv, material=material, merged_log_commit=_head(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))
    # Revocation stamped at (== ) the add position → an ancestor of S → key invalid at S.
    _write_env(repo, pub, earliest, revoked_at=earliest)

    proc = _run(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_unresolvable_storage_anchor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(2c) Fail-closed on an unresolvable storage anchor. Forcing the CLI's s_commit=None end to
    end needs compaction of the TERMINAL SIGNATURE event, which is env/threshold-dependent and
    flaky (see the NOTE in test_verify_opcert_heldout.py). So we unit-test the smallest tier that
    truly exercises the fail-closed path: `opcert.verify_opcert` with a storage anchor that does not
    resolve in the tracker must NOT certify, while the SAME inputs with the real anchor DO — proving
    the unresolvable anchor is the sole cause."""
    from _opcert_helpers import keypair, store_with_chain

    repo, _tr, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    envelope = opcert.sign_opcert(
        "abcd-1234", MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID
    )

    # Control: with the REAL storage anchor (a resolvable descendant of the add position) → certify.
    control = opcert.verify_opcert(
        envelope,
        "abcd-1234",
        MATERIAL,
        MERGED,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert control.verified is True and control.verdict == "certified", control.reason

    # Fail-closed: an unresolvable anchor commit (not an object in the tracker) → do NOT certify.
    verdict = opcert.verify_opcert(
        envelope,
        "abcd-1234",
        MATERIAL,
        MERGED,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit="0" * 40,  # resolves to nothing in the tickets branch
        storage_anchor_position=None,
        repo_root=str(repo),
    )
    assert verdict.verified is False, verdict.reason


# ==== 3. trusted-env helper ====================================================================


def test_trusted_env_add_and_revoke_stamp_tip_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(3) `rebar trusted-env add|revoke` stamp the current tickets-branch tip log position. `add`
    writes added_at_log_position == tip-at-add (revoked null); `revoke` stamps
    revoked_at_log_position == tip-at-revoke (advanced between the two). Verified by loading the
    config back through `trusted_env.trusted_env_keyring`."""
    from rebar.attest import trusted_env

    repo = _store(tmp_path, monkeypatch)
    _priv, pub = _keypair(tmp_path, "env")
    rebar.create_ticket("task", "seed activity", repo_root=str(repo))

    tip_at_add = _tip_position(repo)
    add = subprocess.run(
        ["rebar", "trusted-env", "add", ENV_ID, pub, "--root", str(repo)],
        capture_output=True,
        text=True,
    )
    assert add.returncode == 0, f"stdout={add.stdout}\nstderr={add.stderr}"

    keyring = trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo))
    assert keyring is not None and len(keyring) == 1
    rec = keyring[0]
    assert rec["public_key"] == pub
    assert rec["added_at_log_position"] == tip_at_add
    assert rec["revoked_at_log_position"] is None

    # Advance the tickets log so revoke stamps a strictly-later tip than add did.
    rebar.create_ticket("task", "activity between add and revoke", repo_root=str(repo))
    tip_at_revoke = _tip_position(repo)
    assert tip_at_revoke > tip_at_add

    revoke = subprocess.run(
        ["rebar", "trusted-env", "revoke", ENV_ID, pub, "--root", str(repo)],
        capture_output=True,
        text=True,
    )
    assert revoke.returncode == 0, f"stdout={revoke.stdout}\nstderr={revoke.stderr}"

    rec = trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo))[0]
    assert rec["added_at_log_position"] == tip_at_add  # unchanged
    assert rec["revoked_at_log_position"] == tip_at_revoke
