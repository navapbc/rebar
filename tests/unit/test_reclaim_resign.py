"""Re-signing failing events during the reclaim collapse (story minimum-seedy-lowchen).

Each test builds a disposable shadow tickets repository under ``tmp_path``. The identity
holds two keys added at different positions; the failing events are whatever enforced
``verify-identity --all`` reports on the fixture before the collapse, exactly as the
publication runbook produces them. Keys are generated in ``tmp_path`` and served by an
ssh-agent the fixture starts and kills, so signing goes through the agent the way the
runbook does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar.attest import authorship, dsse, registry, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = shutil.which("ssh-agent") is not None and shutil.which("ssh-add") is not None
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = [
    pytest.mark.skipif(not _SSH_OK, reason="ssh-agent and ssh-keygen >= 8.9 required"),
    pytest.mark.skipif(sys.platform == "win32", reason="needs a unix-socket ssh-agent"),
]

IDENTITY = "1d1d-2e2e-3f3f-4a4a"
TASK_X = "aaaa-bbbb-cccc-dddd"
TASK_Y = "eeee-ffff-0000-1111"
TASK_Z = "2222-3333-4444-5555"
# Same-length timestamps: positions are compared as strings, as in the real store.
T = 1_700_000_000_000_000_000


def _ts(offset: int) -> int:
    return T + offset


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _commit(tracker: Path, message: str, when: datetime) -> str:
    env = subprocess_env(
        {"GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
    )
    subprocess.run(["git", "-C", str(tracker), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(tracker), "commit", "-q", "-m", message], check=True, env=env)
    return _git(tracker, "rev-parse", "HEAD")


def _event(event_type: str, offset: int, uuid: str, data: dict, author_id: str | None) -> dict:
    event = {
        "event_type": event_type,
        "timestamp": _ts(offset),
        "uuid": uuid,
        "author": "Test Bot",
        "env_id": "test-env",
        "data": data,
    }
    if author_id is not None:
        event["author_id"] = author_id
    return event


def _write(tracker: Path, ticket: str, event: dict) -> Path:
    d = tracker / ticket
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{event['timestamp']}-{event['uuid']}-{event['event_type']}.json"
    path.write_text(json.dumps(event, sort_keys=True, separators=(",", ":")))
    return path


def _keypair(tmp_path: Path, name: str) -> tuple[Path, str]:
    key = tmp_path / "keys" / name
    key.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    return key, Path(f"{key}.pub").read_text().strip()


def _foreign_sig(event: dict, key: Path) -> str:
    envelope = authorship.sign_event_authorship(event, str(key), principal=IDENTITY)
    return dsse.encode(
        envelope.payload_type,
        envelope.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in envelope.signatures],
    )


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An ssh-agent private to the test, killed on teardown."""
    sock = Path(f"/tmp/rs-{os.getpid()}-{time.monotonic_ns()}.sock")
    proc = subprocess.Popen(
        ["ssh-agent", "-D", "-a", str(sock)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.05)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))

        def add(key: Path) -> None:
            subprocess.run(["ssh-add", "-q", str(key)], check=True, capture_output=True)

        yield add
    finally:
        proc.kill()
        proc.wait()
        sock.unlink(missing_ok=True)


class Store:
    def __init__(self, tracker: Path, boundary: str, keys: dict[str, tuple[Path, str]]):
        self.tracker = tracker
        self.boundary = boundary
        self.keys = keys


def _build(tmp_path: Path, *, early_target: bool = False, side_branch: bool = False) -> Store:
    tracker = tmp_path / "shadow"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "dev@example.com")
    _git(tracker, "config", "user.name", "Dev")
    (tracker / ".rebar").mkdir()
    (tracker / ".rebar" / "reclaim-shadow-clone").write_text("disposable test clone\n")
    key_a, pub_a = _keypair(tmp_path, "key-a")
    key_b, pub_b = _keypair(tmp_path, "key-b")
    foreign, _pub_f = _keypair(tmp_path, "foreign")
    old = datetime.now(UTC) - timedelta(days=45)
    recent = datetime.now(UTC) - timedelta(days=1)
    base = _commit(tracker, "base", old - timedelta(seconds=10))

    _write(
        tracker,
        IDENTITY,
        _event(
            "CREATE",
            0,
            "00000000-0000-4000-8000-000000000001",
            {"ticket_type": "identity", "title": "bot"},
            None,
        ),
    )
    _write(
        tracker,
        IDENTITY,
        _event(
            "KEY_ADD", 100, "00000000-0000-4000-8000-000000000002", {"public_key": pub_a}, IDENTITY
        ),
    )
    _commit(tracker, "identity with key A", old)

    x_create = _event(
        "CREATE",
        150 if not early_target else 50,
        "10000000-0000-4000-8000-000000000001",
        {"ticket_type": "task", "title": "x", "description": ""},
        IDENTITY,
    )
    _write(tracker, TASK_X, x_create)
    folded = _event(
        "COMMENT",
        200,
        "10000000-0000-4000-8000-000000000002",
        {"body": "folded, foreign-signed"},
        IDENTITY,
    )
    folded["author_sig"] = _foreign_sig(folded, foreign)
    folded_path = _write(tracker, TASK_X, folded)
    unsigned_below = _event(
        "COMMENT",
        500,
        "10000000-0000-4000-8000-000000000003",
        {"body": "unsigned below the boundary"},
        IDENTITY,
    )
    _write(tracker, TASK_X, unsigned_below)
    _commit(tracker, "task x", old + timedelta(seconds=1))
    if side_branch:
        _side_branch_task(tracker, base, old + timedelta(seconds=1))

    snapshot = _event(
        "SNAPSHOT",
        600,
        "10000000-0000-4000-8000-000000000004",
        {
            "compiled_state": {
                "ticket_id": TASK_X,
                "ticket_type": "task",
                "title": "x",
                "status": "open",
                "description": "",
                "tags": [],
                "comments": [],
                "authorship_ledger": [
                    {
                        "event_uuid": folded["uuid"],
                        "content_hash": authorship.authorship_content_hash(folded),
                        "signature": folded["author_sig"],
                        "signer_pubkey": None,
                        "position": {
                            "commit_sha": None,
                            "position": f"{folded['timestamp']}-{folded['uuid']}",
                        },
                    }
                ],
            },
            "source_event_uuids": [folded["uuid"]],
            "compacted_at": _ts(600),
        },
        None,
    )
    _write(tracker, TASK_X, snapshot)
    folded_path.rename(folded_path.with_suffix(".json.retired"))
    boundary = _commit(tracker, "snapshot x", old + timedelta(seconds=2))

    _write(
        tracker,
        IDENTITY,
        _event(
            "KEY_ADD", 3000, "00000000-0000-4000-8000-000000000003", {"public_key": pub_b}, IDENTITY
        ),
    )
    _commit(tracker, "identity adds key B", recent)
    _write(
        tracker,
        TASK_Y,
        _event(
            "CREATE",
            3400,
            "20000000-0000-4000-8000-000000000001",
            {"ticket_type": "task", "title": "y", "description": ""},
            IDENTITY,
        ),
    )
    foreign_raw = _event(
        "COMMENT",
        3500,
        "20000000-0000-4000-8000-000000000002",
        {"body": "foreign-signed raw"},
        IDENTITY,
    )
    foreign_raw["author_sig"] = _foreign_sig(foreign_raw, foreign)
    _write(tracker, TASK_Y, foreign_raw)
    _commit(tracker, "task y", recent + timedelta(seconds=1))
    _write(
        tracker,
        TASK_Y,
        _event(
            "COMMENT", 3600, "20000000-0000-4000-8000-000000000003", {"body": "no author id"}, None
        ),
    )
    tip = _commit(tracker, "comment without author", recent + timedelta(seconds=2))
    _git(tracker, "update-ref", "refs/remotes/origin/tickets", tip)
    return Store(tracker, boundary, {"a": (key_a, pub_a), "b": (key_b, pub_b)})


def _side_branch_task(tracker: Path, base: str, old: datetime) -> None:
    """A task written on a branch forked before the identity existed, merged later.

    On the original graph no identity key is an ancestor of its commit. After the
    collapse it shares the checkpoint with the KEY_ADD and is ordered by position.
    """
    _git(tracker, "checkout", "-q", "-b", "side", base)
    _write(
        tracker,
        TASK_Z,
        _event(
            "CREATE",
            160,
            "30000000-0000-4000-8000-000000000001",
            {"ticket_type": "task", "title": "z", "description": ""},
            IDENTITY,
        ),
    )
    _commit(tracker, "task z on a side branch", old - timedelta(seconds=5))
    _git(tracker, "checkout", "-q", "tickets")
    env = subprocess_env(
        {"GIT_AUTHOR_DATE": old.isoformat(), "GIT_COMMITTER_DATE": old.isoformat()}
    )
    subprocess.run(
        ["git", "-C", str(tracker), "merge", "-q", "--no-ff", "-m", "merge side", "side"],
        check=True,
        env=env,
    )
    _git(tracker, "branch", "-D", "side")


def _verify(store: Store, tmp_path: Path) -> subprocess.CompletedProcess:
    env = subprocess_env(
        {
            "REBAR_ROOT": str(tmp_path),
            "REBAR_TRACKER_DIR": str(store.tracker),
            "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
        }
    )
    env.pop("REBAR_IDENTITY_ENFORCE_SINCE", None)
    return subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all", "--format", "json"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _failing(store: Store, tmp_path: Path) -> list[dict]:
    return json.loads(_verify(store, tmp_path).stdout)


def _key_files(store: Store, *names: str) -> dict[str, str]:
    return {store.keys[n][1]: f"{store.keys[n][0]}.pub" for n in names}


def _resign():
    from rebar._commands import reclaim_resign

    return reclaim_resign


def _collapse():
    from rebar._commands import reclaim_collapse

    return reclaim_collapse


def _signed_by(sig: str, pub: str) -> bool:
    envelope = dsse.decode(sig)
    root = authorship.allowed_signers_from_keys([pub], principal=IDENTITY)
    return registry.verify(authorship.AUTHORSHIP_KIND, envelope, root).verified


def _read(tracker: Path, ticket: str, offset: int) -> dict:
    [path] = list((tracker / ticket).glob(f"{_ts(offset)}-*.json"))
    return json.loads(path.read_text())


def _reduced(tracker: Path) -> dict:
    from rebar._commands.reclaim_collapse import reduce_shadow_tracker

    def scrub(value):
        if isinstance(value, dict):
            return {
                k: scrub(v)
                for k, v in value.items()
                if k not in {"authorship", "authorship_ledger", "author_id"}
            }
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    return scrub(reduce_shadow_tracker(tracker))


def test_resign_makes_enforced_verify_pass_with_era_keys(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    failing = _failing(store, tmp_path)
    # The foreign key signs soundly but is not on the keyring, so it is not a forgery.
    assert {f["verdict"] for f in failing} == {"unsigned", "signer-not-on-keyring"}
    before = _reduced(store.tracker)

    plan = _resign().build_resign_plan(
        store.tracker,
        failing,
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
    )
    result = _collapse().collapse_shadow_tracker(
        store.tracker, boundary_commit=store.boundary, apply=True, resign=plan
    )

    done = _verify(store, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == []
    assert result.events_resigned == 5  # X create + below, Y create + foreign + no-author
    assert result.ledger_entries_resigned == 1
    _, pub_a = store.keys["a"]
    _, pub_b = store.keys["b"]
    assert _signed_by(_read(store.tracker, TASK_X, 500)["author_sig"], pub_a)
    assert _signed_by(_read(store.tracker, TASK_Y, 3500)["author_sig"], pub_b)
    no_author = _read(store.tracker, TASK_Y, 3600)
    assert no_author["author_id"] == IDENTITY
    assert _signed_by(no_author["author_sig"], pub_b)
    [ledger] = _read(store.tracker, TASK_X, 600)["data"]["compiled_state"]["authorship_ledger"]
    assert ledger["signer_pubkey"] == pub_a
    assert _signed_by(ledger["signature"], pub_a)
    assert _reduced(store.tracker) == before


def test_below_boundary_raw_target_survives_the_ledger_anchor(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    plan = _resign().build_resign_plan(
        store.tracker,
        _failing(store, tmp_path),
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
    )
    result = _collapse().collapse_shadow_tracker(
        store.tracker, boundary_commit=store.boundary, apply=True, resign=plan
    )
    path = next((store.tracker / TASK_X).glob(f"{_ts(500)}-*.json")).relative_to(store.tracker)
    for commit in (result.checkpoint_sha, result.ledger_anchor_sha):
        event = json.loads(_git(store.tracker, "show", f"{commit}:{path}"))
        assert "author_sig" in event, commit


def test_target_older_than_every_key_raises_before_writing(tmp_path: Path, agent) -> None:
    store = _build(tmp_path, early_target=True)
    for key, _pub in store.keys.values():
        agent(key)
    tip = _git(store.tracker, "rev-parse", "HEAD")
    mod = _resign()
    with pytest.raises(mod.ResignError, match="1 event"):
        mod.build_resign_plan(
            store.tracker,
            _failing(store, tmp_path),
            _key_files(store, "a", "b"),
            identity_id=IDENTITY,
            boundary=store.boundary,
        )
    assert _git(store.tracker, "rev-parse", "HEAD") == tip


def test_unmatched_failing_uuid_is_named(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    failing = [
        *_failing(store, tmp_path),
        {"event_uuid": "99999999-0000-4000-8000-000000000009", "ticket_id": TASK_X},
    ]
    mod = _resign()
    with pytest.raises(mod.ResignError, match="99999999-0000-4000-8000-000000000009"):
        mod.build_resign_plan(
            store.tracker,
            failing,
            _key_files(store, "a", "b"),
            identity_id=IDENTITY,
            boundary=store.boundary,
        )


def test_key_missing_from_agent_is_refused(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    agent(store.keys["a"][0])  # key B is supplied but never loaded
    mod = _resign()
    with pytest.raises(mod.ResignError, match="ssh-agent"):
        mod.build_resign_plan(
            store.tracker,
            _failing(store, tmp_path),
            _key_files(store, "a", "b"),
            identity_id=IDENTITY,
            boundary=store.boundary,
        )


def test_plan_from_another_tip_is_refused(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    plan = _resign().build_resign_plan(
        store.tracker,
        _failing(store, tmp_path),
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
    )
    (store.tracker / TASK_Y / "extra.txt").write_text("moves the tip\n")
    tip = _commit(store.tracker, "later write", datetime.now(UTC))
    _git(store.tracker, "update-ref", "refs/remotes/origin/tickets", tip)
    with pytest.raises(_resign().ResignError, match="tip"):
        _collapse().collapse_shadow_tracker(
            store.tracker, boundary_commit=store.boundary, apply=True, resign=plan
        )
    assert _git(store.tracker, "rev-parse", "HEAD") == tip


def test_empty_failing_list_changes_nothing(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    plain = tmp_path / "plain"
    shutil.copytree(store.tracker, plain)  # independent .git dir, so a separate repo
    plan = _resign().build_resign_plan(
        store.tracker,
        [],
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
    )
    with_plan = _collapse().collapse_shadow_tracker(
        store.tracker, boundary_commit=store.boundary, apply=True, resign=plan
    )
    without = _collapse().collapse_shadow_tracker(plain, boundary_commit=store.boundary, apply=True)
    tree = _git(store.tracker, "rev-parse", f"{with_plan.rewritten_tip}^{{tree}}")
    assert tree == _git(plain, "rev-parse", f"{without.rewritten_tip}^{{tree}}")
    assert with_plan.events_resigned == 0 and with_plan.ledger_entries_resigned == 0


def test_each_target_is_signed_once(tmp_path: Path, agent) -> None:
    store = _build(tmp_path)
    for key, _pub in store.keys.values():
        agent(key)
    calls: list[str] = []

    def counting(payload: bytes, key_path: str, principal: str) -> dsse.Envelope:
        calls.append(key_path)
        return authorship.sign_authorship(payload, key_path, principal)

    failing = _failing(store, tmp_path)
    _resign().build_resign_plan(
        store.tracker,
        failing,
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
        signer=counting,
    )
    assert len(calls) == len(failing)


def test_side_branch_event_uses_the_rewritten_graph_era(tmp_path: Path, agent) -> None:
    store = _build(tmp_path, side_branch=True)
    for key, _pub in store.keys.values():
        agent(key)
    failing = _failing(store, tmp_path)
    assert "30000000-0000-4000-8000-000000000001" in {f["event_uuid"] for f in failing}
    plan = _resign().build_resign_plan(
        store.tracker,
        failing,
        _key_files(store, "a", "b"),
        identity_id=IDENTITY,
        boundary=store.boundary,
    )
    _collapse().collapse_shadow_tracker(
        store.tracker, boundary_commit=store.boundary, apply=True, resign=plan
    )
    done = _verify(store, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    _, pub_a = store.keys["a"]
    assert _signed_by(_read(store.tracker, TASK_Z, 160)["author_sig"], pub_a)
