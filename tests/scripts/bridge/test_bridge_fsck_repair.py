"""``bridge fsck --repair`` — the supported prune for orphaned reverse bindings.

Bug 874a (vinifera-farflung-nyala). Before this verb, a ``reverse`` key with no
forward entry was reported by ``bridge fsck`` as ``store_integrity`` / kind
``reverse_missing_forward`` forever and no supported surface could remove it, so
the binding-drift canary alerted indefinitely on a benign fault. The 13
REB-410..REB-422 orphans repaired under nonliteral-spangly-fly had to reach into
``BindingStore._data``.

These cells pin the four pre-write guards: the prune acts on EXACTLY the audited
finding set, refuses on any other integrity kind, refuses on a retired key, and
leaves the forward map and the retired file untouched.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.scripts]


def _write_bindings(tracker: Path, bindings: dict, reverse: dict) -> None:
    state = tracker / ".bridge_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "bindings.json").write_text(
        json.dumps({"version": 2, "bindings": bindings, "reverse": reverse})
    )


def _confirmed(jira_key: str) -> dict:
    return {"jira_key": jira_key, "state": "confirmed"}


def _init_tickets_repo(tracker: Path) -> None:
    tracker.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(tracker), "init", "-q", "-b", "tickets"], check=True)
    subprocess.run(
        ["git", "-C", str(tracker), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tracker), "config", "user.name", "Test"], check=True)


def _commit_known_event(tracker: Path) -> None:
    """Give the tracker a committed ``tickets`` branch.

    ``bridge fsck``'s unknown-event scan greps ``refs/heads/tickets``; without a
    commit it fails operationally (exit 2), which would mask the exit code the
    audit-only cell is actually asserting.
    """
    ticket_dir = tracker / "fixture-ticket"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / "1-known-CREATE.json").write_text(
        json.dumps(
            {
                "event_type": "CREATE",
                "uuid": "11111111-1111-4111-8111-111111111111",
                "timestamp": 1,
                "author": "test",
                "env_id": "22222222-2222-4222-8222-222222222222",
                "data": {"ticket_type": "task", "title": "fixture", "parent_id": None},
            }
        )
    )
    subprocess.run(["git", "-C", str(tracker), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tracker), "commit", "-q", "-m", "fixture"], check=True)


def _bindings_path(tracker: Path) -> Path:
    return tracker / ".bridge_state" / "bindings.json"


def _load(tracker: Path) -> dict:
    return json.loads(_bindings_path(tracker).read_text())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prune(tracker: Path) -> int:
    from rebar._commands.bridge_repair import prune_orphan_reverse_bindings

    return prune_orphan_reverse_bindings(tracker, argv=["--repair"])


def _integrity(tracker: Path) -> list[dict]:
    from rebar._engine_support import bridge_fsck

    return bridge_fsck.audit_store_integrity(tracker)


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    path = tmp_path / ".tickets-tracker"
    _init_tickets_repo(path)
    return path


def test_prune_clears_orphaned_reverse_keys_and_leaves_forward_intact(tracker):
    """Happy path: store_integrity N -> 0 with a byte-identical forward map."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1"), "live-2": _confirmed("REB-2")},
        reverse={
            "REB-1": "live-1",
            "REB-2": "live-2",
            "REB-410": "gone-1",
            "REB-411": "gone-2",
        },
    )
    before_forward = json.dumps(_load(tracker)["bindings"], sort_keys=True)
    assert len(_integrity(tracker)) == 2

    assert _prune(tracker) == 0

    assert _integrity(tracker) == []
    after = _load(tracker)
    assert json.dumps(after["bindings"], sort_keys=True) == before_forward
    assert after["reverse"] == {"REB-1": "live-1", "REB-2": "live-2"}


def test_prune_leaves_the_retired_file_byte_identical(tracker):
    """The prune must not touch bindings-retired.json (sha256 compare)."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )
    retired = tracker / ".bridge_state" / "bindings-retired.json"
    retired.write_text(json.dumps({"REB-900": {"local_id": "old-1", "retired_at": "x"}}))
    before = _sha256(retired)

    assert _prune(tracker) == 0

    assert _sha256(retired) == before


def test_prune_refuses_when_another_integrity_kind_is_present(tracker):
    """Guard 1: never repair a store carrying a fault this verb cannot express."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1"), "no-reverse": _confirmed("REB-2")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )
    before = _sha256(_bindings_path(tracker))
    kinds = {f["kind"] for f in _integrity(tracker)}
    assert "forward_missing_reverse" in kinds and "reverse_missing_forward" in kinds

    assert _prune(tracker) == 1

    assert _sha256(_bindings_path(tracker)) == before, "a refusal must not write"


def test_prune_refuses_when_an_orphan_key_is_retired(tracker):
    """Guard 2: a retired key is a tombstone, not an orphan — tombstone wins."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )
    retired = tracker / ".bridge_state" / "bindings-retired.json"
    retired.write_text(json.dumps({"REB-410": {"local_id": "gone-1", "retired_at": "x"}}))
    before = _sha256(_bindings_path(tracker))

    assert _prune(tracker) == 1

    assert _sha256(_bindings_path(tracker)) == before
    assert "REB-410" in _load(tracker)["reverse"]


def test_prune_is_a_no_op_on_a_healthy_store(tracker):
    """Guard: a clean store is left byte-identical and exits 0."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1"},
    )
    before = _sha256(_bindings_path(tracker))
    assert _integrity(tracker) == []

    assert _prune(tracker) == 0

    assert _sha256(_bindings_path(tracker)) == before
    assert _integrity(tracker) == []


def test_prune_handles_several_orphans_sharing_one_local_id(tracker):
    """Two reverse keys pointing at the same absent local_id both go."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1", "REB-411": "gone-1"},
    )

    assert _prune(tracker) == 0

    assert _load(tracker)["reverse"] == {"REB-1": "live-1"}
    assert _integrity(tracker) == []


def test_prune_writes_a_durable_audit_line(tracker):
    """The deleted key set is recorded in the tracker's git dir, not the worktree."""
    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )

    assert _prune(tracker) == 0

    audit = tracker / ".git" / "rebar-bridge-repair-audit.jsonl"
    assert audit.exists(), "the durable audit line must land in the tracker's git dir"
    record = json.loads(audit.read_text().strip().splitlines()[-1])
    assert record["deleted_reverse_keys"] == ["REB-410"]
    assert record["operation"] == "prune_orphan_reverse_bindings"
    assert record["forward_count"] == 1
    # The audit must NOT become store content a later merge could conflict on.
    assert not (tracker / "rebar-bridge-repair-audit.jsonl").exists()


def test_repair_flag_is_reachable_through_the_fsck_entry_point(tracker):
    """`bridge fsck --repair` dispatches to the prune (the wiring, end to end)."""
    from rebar._engine_support import bridge_fsck

    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )

    rc = bridge_fsck.main(["--tickets-tracker", str(tracker), "--repair"])

    assert rc == 0
    assert _load(tracker)["reverse"] == {"REB-1": "live-1"}


def test_fsck_without_repair_never_writes(tracker):
    """The audit keeps its read-only boundary: a dirty store stays dirty."""
    from rebar._engine_support import bridge_fsck

    _write_bindings(
        tracker,
        bindings={"live-1": _confirmed("REB-1")},
        reverse={"REB-1": "live-1", "REB-410": "gone-1"},
    )
    _commit_known_event(tracker)
    before = _sha256(_bindings_path(tracker))

    rc = bridge_fsck.main(["--tickets-tracker", str(tracker)])

    assert rc == 1, "findings present"
    assert _sha256(_bindings_path(tracker)) == before
