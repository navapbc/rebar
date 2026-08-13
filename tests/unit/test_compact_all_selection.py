"""`compact-all` must RECUR, not just backfill (story gaudy-gangrenous-basilisk).

THE DEFECT. `_scan_snapshot_state` split ticket dirs into "lacking a `-SNAPSHOT.json`" and
"already has one", and compacted only the former. A ticket folded once and since grown by
hundreds of live events was counted as `already` and never folded again — so `compact-all` was
a one-time BACKFILL. That was tolerable while every close compacted inline; once compaction
left the close path (bug choosy-arthrodic-barbet) this sweep became the store's ONLY standing
trigger, and a trigger that never re-fires is no trigger.

THE FIX (asserted here): selection is by FOLDABLE event count against `compact.threshold`,
whatever a ticket's snapshot state. Two properties matter and are tested separately:

* it WIDENS — an already-snapshotted ticket over threshold is now selected, while
  under-threshold tickets and the historical backfill case behave as before;
* it CONVERGES — selection counts only events the fold would actually squash (older than the
  compaction horizon), so a ticket whose excess events are all inside the horizon is not
  selected, and a pass that folds nothing is not reported as work.

The convergence half is the subtle one: counting merely LIVE events would select such a
ticket, the fold would write nothing, the driver would count it as compacted, and the next
sweep would select it again — churn plus a false tally.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact

pytestmark = pytest.mark.unit

# One hour in HLC ns — comfortably outside the 30-minute default compaction horizon.
_HOUR_NS = 3_600_000_000_000


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tdir(repo: Path, tid: str) -> Path:
    return _tracker(repo) / tid


def _age_events(tdir: Path, by_ns: int) -> None:
    """Rewrite every live event's timestamp to be `by_ns` older, so the fold sees it as
    outside the horizon. Renames the file too — the filename carries the timestamp prefix."""
    for path in sorted(tdir.glob("*.json")):
        if path.name.startswith("."):
            continue
        event = json.loads(path.read_text())
        ts = event.get("timestamp")
        if not isinstance(ts, int):
            continue
        event["timestamp"] = ts - by_ns
        rest = path.name.split("-", 1)[1]
        path.write_text(json.dumps(event))
        path.rename(path.parent / f"{event['timestamp']}-{rest}")


def _seed(repo: Path, title: str, comments: int) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _selected(repo: Path, threshold: int, horizon: int) -> list[str]:
    needs, _rest = _compact._scan_snapshot_state(str(_tracker(repo)), threshold, horizon)
    return needs


# ── the widening: an already-snapshotted ticket over threshold is selected again ─────────────
def test_already_snapshotted_ticket_over_threshold_is_selected(store: Path) -> None:
    """RED on the pre-fix code: the ticket has a SNAPSHOT, so it counted as `already` and was
    never returned — `compact-all` could not re-fold a grown ticket."""
    repo = store
    tid = _seed(repo, "grown after folding", comments=3)
    assert _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo)) == 0
    tdir = _tdir(repo, tid)
    assert list(tdir.glob("*-SNAPSHOT.json")), "precondition: the ticket has been folded once"

    for i in range(5):  # it grows again
        rebar.comment(tid, f"post-fold {i}", repo_root=str(repo))
    _age_events(tdir, _HOUR_NS)

    assert tid in _selected(repo, threshold=2, horizon=_HOUR_NS // 2), (
        "an already-snapshotted ticket whose foldable events exceed the threshold must be "
        "selected — otherwise the sweep never re-folds a growing ticket"
    )


# ── the widening does not become a replacement ───────────────────────────────────────────────
def test_under_threshold_already_snapshotted_ticket_is_not_selected(store: Path) -> None:
    """The quiet case: already folded, and nothing has accumulated since. Neither arm applies,
    so the sweep leaves it alone."""
    repo = store
    tid = _seed(repo, "small and settled", comments=1)
    assert _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo)) == 0
    tdir = _tdir(repo, tid)
    assert list(tdir.glob("*-SNAPSHOT.json")), "precondition: folded once"
    _age_events(tdir, _HOUR_NS)

    assert tid not in _selected(repo, threshold=50, horizon=_HOUR_NS // 2)


def test_small_snapshotless_ticket_is_still_backfilled(store: Path) -> None:
    """The historical BACKFILL arm, preserved. A ticket with fewer events than the threshold
    still earns its first SNAPSHOT — selecting on the threshold alone would have silently
    stopped backfilling small tickets, which is a regression in the other direction from the
    one this story fixes."""
    repo = store
    tid = _seed(repo, "tiny and unfolded", comments=1)
    tdir = _tdir(repo, tid)
    assert not list(tdir.glob("*-SNAPSHOT.json")), "precondition: never folded"
    _age_events(tdir, _HOUR_NS)

    assert tid in _selected(repo, threshold=50, horizon=_HOUR_NS // 2), (
        "a snapshot-less ticket must still be backfilled even when it is under the threshold"
    )


def test_the_backfill_arm_converges(store: Path) -> None:
    """After a backfill the ticket has a SNAPSHOT and its live count is back under the
    threshold, so neither arm re-selects it. Without this the backfill arm would churn."""
    repo = store
    tid = _seed(repo, "backfill once", comments=1)
    _age_events(_tdir(repo, tid), _HOUR_NS)
    assert tid in _selected(repo, threshold=50, horizon=_HOUR_NS // 2)

    assert _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo)) == 0
    _age_events(_tdir(repo, tid), _HOUR_NS)

    assert tid not in _selected(repo, threshold=50, horizon=_HOUR_NS // 2), (
        "the backfill arm re-selected an already-backfilled ticket — it does not converge"
    )


def test_snapshotless_ticket_over_threshold_still_backfills(store: Path) -> None:
    """The historical backfill case is the same rule applied to a whole log."""
    repo = store
    tid = _seed(repo, "never folded", comments=4)
    tdir = _tdir(repo, tid)
    assert not list(tdir.glob("*-SNAPSHOT.json")), "precondition: never folded"
    _age_events(tdir, _HOUR_NS)

    assert tid in _selected(repo, threshold=2, horizon=_HOUR_NS // 2)


# ── convergence: selection counts only what the fold would actually squash ───────────────────
def test_ticket_whose_excess_events_are_inside_the_horizon_is_not_selected(store: Path) -> None:
    """The churn case. The ticket is over threshold by raw live count, but every event is
    younger than the horizon, so the fold would squash nothing.

    RED on a raw-live-count selection: the ticket is selected, the fold writes nothing, and the
    next sweep selects it again forever."""
    repo = store
    tid = _seed(repo, "all events are young", comments=5)

    # No _age_events call: every event was just written, so all are inside the horizon.
    assert tid not in _selected(repo, threshold=2, horizon=_HOUR_NS), (
        "a ticket whose events are all within the compaction horizon must NOT be selected — "
        "the fold would write nothing and the sweep would churn on it every run"
    )


def test_sweep_converges_across_two_consecutive_runs(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """End-to-end convergence: sweep, then sweep again. The second run must report nothing
    compacted, because the first run folded everything foldable."""
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")  # the packaged default (10) skips these
    for n in (3, 4):
        tid = _seed(repo, f"sweep me {n}", comments=n)
        _age_events(_tdir(repo, tid), _HOUR_NS)

    assert _compact.compact_all_cli([], repo_root=str(repo)) in (0, 2)
    first = capsys.readouterr().out
    assert "Done: " in first

    assert _compact.compact_all_cli([], repo_root=str(repo)) in (0, 2)
    second = capsys.readouterr().out

    assert "0 compacted" in second or "Nothing to do." in second, (
        f"the second sweep must fold nothing — it is not converging.\n"
        f"first run:\n{first}\nsecond run:\n{second}"
    )


# ── honest reporting: a pass that folds nothing is not counted as work ───────────────────────
def test_a_pass_that_folds_nothing_is_not_counted_as_compacted(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`_compact_locked` returns 0 for a successful fold AND for every no-op branch, so the
    driver cannot tell them apart from the return code — it observes whether a new SNAPSHOT
    appeared instead.

    RED on the pre-fix code: `rc == 0` counted the no-op as compacted."""
    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")  # the packaged default (10) skips these
    tid = _seed(repo, "fold is a no-op", comments=4)
    _age_events(_tdir(repo, tid), _HOUR_NS)

    # A fold that succeeds (rc 0) but writes nothing — exactly the shape of the below-threshold
    # / nothing-older-than-horizon / no-safe-gap branches.
    monkeypatch.setattr(_compact, "compact_cli", lambda *a, **k: 0)

    _compact.compact_all_cli([], repo_root=str(repo))
    out = capsys.readouterr().out

    assert "0 compacted" in out, (
        f"a fold that wrote no SNAPSHOT must not be counted as compacted; got:\n{out}"
    )
    assert "nothing to fold" in out, "the honest outcome must be reported, not silently dropped"


# ── the push contract is independent of whether anything was folded ──────────────────────────
def test_a_sweep_that_folds_nothing_still_pushes(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that motivated `_best_effort_push`.

    Before honest counting, every selected ticket incremented `compacted` — even a fold that
    wrote nothing — so `compacted > 0` really meant "we selected something" and the commit+push
    tail behind it effectively always ran. Making the count honest silently disabled the push on
    any sweep that folded nothing. The push exists so an earlier unpushed write in the session
    is not stranded, which has nothing to do with what THIS sweep folded, so the two are
    decoupled — and that decoupling needs a test, or the next honest-counting-style change
    re-breaks it invisibly."""
    from rebar._store import push as _push

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    tid = _seed(repo, "nothing will fold", comments=4)
    _age_events(_tdir(repo, tid), _HOUR_NS)

    calls: list[object] = []
    monkeypatch.setattr(_push, "push_after_commit", lambda *a, **k: calls.append(a))
    # A fold that succeeds but writes nothing — the below-threshold / nothing-older-than-horizon
    # / no-safe-gap shape, all of which return 0.
    monkeypatch.setattr(_compact, "compact_cli", lambda *a, **k: 0)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert calls, "a sweep that folded nothing must still honour the best-effort push contract"


def test_a_sweep_with_nothing_selected_still_pushes(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `Nothing to do.` early return could not push at all before this change."""
    from rebar._store import push as _push

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "9999")  # select nothing
    _seed(repo, "well under threshold", comments=1)

    calls: list[object] = []
    monkeypatch.setattr(_push, "push_after_commit", lambda *a, **k: calls.append(a))

    _compact.compact_all_cli([], repo_root=str(repo))

    assert calls, "the 'nothing selected' path must still push a pending earlier write"


def test_dry_run_never_pushes(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run must have NO side effects. Pushing is a side effect even when this invocation
    committed nothing of its own — it delivers whatever was already pending."""
    from rebar._store import push as _push

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "9999")  # nothing selected: the early return
    _seed(repo, "dry run me", comments=1)

    calls: list[object] = []
    monkeypatch.setattr(_push, "push_after_commit", lambda *a, **k: calls.append(a))

    _compact.compact_all_cli(["--dry-run"], repo_root=str(repo))

    assert not calls, "--dry-run pushed; a dry run must not touch the remote"


def test_no_commit_never_pushes(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._store import push as _push

    repo = store
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "9999")
    _seed(repo, "no commit", comments=1)

    calls: list[object] = []
    monkeypatch.setattr(_push, "push_after_commit", lambda *a, **k: calls.append(a))

    _compact.compact_all_cli(["--no-commit"], repo_root=str(repo))

    assert not calls, "--no-commit pushed; it opts out of the commit and its delivery alike"
