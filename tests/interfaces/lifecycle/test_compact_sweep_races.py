"""The out-of-band compaction sweep must converge with concurrent writers
(story gaudy-gangrenous-basilisk).

Compaction now runs in a DISPOSABLE CLONE on a schedule rather than inline on every close
(bug choosy-arthrodic-barbet), so its result reaches the shared store through the ordinary
push/merge path instead of under the store write lock. That makes convergence a property of
the MERGE, not of mutual exclusion, and invariant I9 is the claim being tested here: a fold
adds one SNAPSHOT and renames its sources to ``*.retired``, while concurrent sessions only ADD
new event files, so the two merge as a union.

What is asserted:

* **double-snapshot race** — two clones fold the same ticket independently and both land;
  the merged store still reduces to the same ticket state, with no event lost or duplicated;
* **concurrent tail** — events appended while a fold runs are NOT swallowed by the SNAPSHOT:
  they sort after it and replay on top;
* **clone signing parity** — a fold performed from a separate clone of the tickets branch
  produces the same authorship ledger content as one performed in place, so moving compaction
  off the primary checkout does not change what the SNAPSHOT attests.

Every oracle is the reduced ticket state or the SNAPSHOT's own bytes — never a timing, and
never a private call count.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.interface


@pytest.fixture(autouse=True)
def _fold_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tdir(repo: Path, tid: str) -> Path:
    return Path(layout_ticket_dir(_tracker(repo), tid))


def _seed(repo: Path, title: str, comments: int) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _clone_store(src_tracker: Path, dest_root: Path) -> Path:
    """A SEPARATE clone of the tickets store, laid out as a rebar repo root.

    Returns the ROOT (the clone lands at ``<root>/.tickets-tracker``, which is where
    ``config.tracker_dir`` looks) so ``repo_root=`` resolves the same way it does on the CI
    runner. Its own .git, its own index, its own locks — nothing shared with the source
    store, which is exactly the isolation the out-of-band design depends on."""
    dest_root.mkdir(parents=True, exist_ok=True)
    tracker = dest_root / ".tickets-tracker"
    subprocess.run(
        ["git", "clone", "-q", str(src_tracker), str(tracker)], check=True, capture_output=True
    )
    for key, value in (("user.email", "sweep@e.com"), ("user.name", "sweep")):
        _git(tracker, "config", key, value)
    return dest_root


def _comment_bodies(state: dict | None) -> set[str]:
    return {c.get("body", "") for c in ((state or {}).get("comments") or [])}


# ── double-snapshot race: two clones fold the same ticket, then MERGE ────────────────────────
def test_two_independent_folds_of_one_ticket_converge(rebar_repo: Path, tmp_path: Path) -> None:
    """Fold the ticket in two separate clones, then actually MERGE one into the other.

    The merge is the whole point. Two sweeps racing do not see each other's locks — they are
    different machines — so convergence is a property of the union merge (invariant I9), not of
    mutual exclusion. A version of this test that folded in both clones and never merged would
    prove nothing about the race it names.

    What actually happens, and is asserted here: each fold writes a SNAPSHOT under its own
    unique filename and renames the SAME sources to ``*.retired``. Git merges the identical
    renames as one change and both SNAPSHOTs as adds, so the merge is CLEAN and the ticket ends
    up carrying two SNAPSHOTs. The reducer tolerates that and still replays the same state,
    which is what makes the out-of-band sweep safe to run against a store other clones write.
    """
    repo = rebar_repo
    tid = _seed(repo, "folded twice", comments=4)
    before = _comment_bodies(reduce_ticket(str(_tdir(repo, tid))))
    assert len(before) == 4, "precondition: four comments are live"

    tracker = _tracker(repo)
    clone_a = _clone_store(tracker, tmp_path / "clone-a")
    clone_b = _clone_store(tracker, tmp_path / "clone-b")

    # Each clone folds and COMMITS independently, exactly as two scheduled sweeps would.
    for clone in (clone_a, clone_b):
        rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(clone))
        assert rc == 0, f"the fold failed in {clone.name}"
        clone_snapshots = list(
            Path(layout_ticket_dir(clone / ".tickets-tracker", tid)).glob("*-SNAPSHOT.json")
        )
        assert clone_snapshots, f"{clone.name} did not actually fold the ticket"

    # Merge B into A — the delivery path a real sweep's push takes.
    tracker_a = clone_a / ".tickets-tracker"
    tracker_b = clone_b / ".tickets-tracker"
    branch_b = _git(tracker_b, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert _git(tracker_a, "remote", "add", "peer", str(tracker_b)).returncode == 0
    assert _git(tracker_a, "fetch", "-q", "peer").returncode == 0
    merge = _git(tracker_a, "merge", "--no-edit", f"peer/{branch_b}")

    assert merge.returncode == 0, (
        "two independent folds of one ticket did NOT merge cleanly — the sweep cannot be run "
        f"concurrently with another writer.\nstdout: {merge.stdout}\nstderr: {merge.stderr}"
    )

    merged_dir = Path(layout_ticket_dir(tracker_a, tid))
    snapshots = sorted(p.name for p in merged_dir.glob("*-SNAPSHOT.json"))
    assert len(snapshots) == 2, (
        f"expected the race to leave BOTH snapshots after the union merge, got {snapshots}"
    )
    assert list(merged_dir.glob("*.retired")), "the folded sources must survive as *.retired"

    state = reduce_ticket(str(merged_dir))
    assert state is not None, "the merged ticket no longer reduces"
    assert not state.get("error"), f"the merged ticket reduces to an error: {state.get('error')}"
    assert _comment_bodies(state) == before, (
        "the merged store lost or duplicated events — two concurrent folds did not converge"
    )


# ── the concurrent tail is not swallowed ─────────────────────────────────────────────────────
def test_events_appended_after_a_fold_replay_on_top_of_the_snapshot(rebar_repo: Path) -> None:
    """A SNAPSHOT absorbs only the events it folded. Anything appended afterwards sorts after
    it and must still appear in reduced state — the property that lets a sweep run against a
    store other agents are actively writing."""
    repo = rebar_repo
    tid = _seed(repo, "tail survives", comments=3)

    assert _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo)) == 0
    tdir = _tdir(repo, tid)
    assert len(list(tdir.glob("*-SNAPSHOT.json"))) == 1, "precondition: the ticket folded"

    rebar.comment(tid, "arrived after the fold", repo_root=str(repo))

    bodies = _comment_bodies(reduce_ticket(str(tdir)))
    assert "arrived after the fold" in bodies, (
        "an event appended after the fold was swallowed — the snapshot must not shadow its tail"
    )
    assert {"c0", "c1", "c2"} <= bodies, "the folded comments must still replay from the SNAPSHOT"


# ── folding from a clone attests the same thing as folding in place ──────────────────────────
def test_a_fold_in_a_clone_matches_a_fold_in_place(rebar_repo: Path, tmp_path: Path) -> None:
    """Move compaction off the primary checkout and the SNAPSHOT must still say the same
    thing. Compares the compiled state and authorship ledger produced by a clone-side fold
    against an in-place fold of an identical ticket."""
    repo = rebar_repo
    tid = _seed(repo, "signing parity", comments=3)

    clone = _clone_store(_tracker(repo), tmp_path / "clone")
    assert (
        _compact.compact_cli(
            [tid, "--threshold=0", "--skip-sync", "--no-commit"], repo_root=str(clone)
        )
        == 0
    )
    assert _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo)) == 0

    clone_snaps = sorted(
        Path(layout_ticket_dir(clone / ".tickets-tracker", tid)).glob("*-SNAPSHOT.json")
    )
    local_snaps = sorted(_tdir(repo, tid).glob("*-SNAPSHOT.json"))
    assert clone_snaps and local_snaps, "both folds must have written a SNAPSHOT"

    clone_state = json.loads(clone_snaps[0].read_text())["data"]["compiled_state"]
    local_state = json.loads(local_snaps[0].read_text())["data"]["compiled_state"]

    assert clone_state.get("authorship_ledger") == local_state.get("authorship_ledger"), (
        "a clone-side fold produced a different authorship ledger — the sweep would attest "
        "something the in-place path does not"
    )
    for key in ("status", "title", "ticket_type"):
        assert clone_state.get(key) == local_state.get(key), (
            f"clone-side fold disagrees with the in-place fold on {key}"
        )
