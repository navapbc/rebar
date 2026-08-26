"""One compaction planner (story 3436-71db-ceff-4ac0).

The normal fold, the fsck rebuild and crash recovery each re-derived the same decisions in
their own code, which is how the compaction family arrived as six separate bug tickets: a fix
landed in one copy and its twin stayed broken. ``aea0`` is the cautionary one — the rebuild
path skipped a folded prior SNAPSHOT when listing sources from day one, and the fold had to be
taught the same rule years later, after fsck spent that time reporting six healthy tickets as
damaged.

These tests pin the consolidation the way ``tests/unit/test_spawn_detached.py`` and
``tests/unit/store/test_store_paths.py`` pin theirs: the behaviour is correct on BOTH engines,
AND the construct exists in exactly one place, so a third copy cannot re-enter by imitation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

import rebar
from rebar._commands import compact as _compact
from rebar._commands import compact_plan
from rebar._commands import compact_rebuild as _rebuild

pytestmark = pytest.mark.unit

_SRC_REBAR = Path(rebar.__file__).resolve().parent
_OWNER = _SRC_REBAR / "_commands" / "compact_plan.py"


# ======================================================================================
# Fixtures — a real store, real folds, real rebuilds. No seam is injected: a test that
# only exercised an injected planner would pass for any wiring, including none.
# ======================================================================================
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
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tdir(repo: Path, tid: str) -> Path:
    return repo / ".tickets-tracker" / tid


def _seed(repo: Path, title: str, comments: int = 3) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _fold(repo: Path, tid: str) -> int:
    return _compact.compact_cli(
        [tid, "--threshold=0", "--horizon=0", "--skip-sync"], repo_root=str(repo)
    )


def _snapshot(repo: Path, tid: str) -> dict:
    snaps = sorted(_tdir(repo, tid).glob("*-SNAPSHOT.json"))
    assert len(snaps) == 1, f"expected exactly one live SNAPSHOT, got {snaps}"
    return json.loads(snaps[0].read_text(encoding="utf-8"))


def _uuid_of(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["uuid"]


def _raw_uuids(repo: Path, tid: str, *, active_only: bool = False) -> set[str]:
    """Every NON-snapshot event uuid in the ticket dir; ``active_only`` drops ``*.retired``
    ones — the difference between what a fold sees and what a rebuild sees."""
    return {
        _uuid_of(p)
        for p in _tdir(repo, tid).iterdir()
        if not p.name.startswith(".")
        and not p.name.endswith("-SYNC.json")
        and p.name.removesuffix(".retired").endswith(".json")
        and not compact_plan.is_snapshot_event_file(p.name)
        and (not active_only or not p.name.endswith(".retired"))
    }


def _snapshot_uuids(repo: Path, tid: str) -> set[str]:
    return {
        _uuid_of(p)
        for p in _tdir(repo, tid).iterdir()
        if compact_plan.is_snapshot_event_file(p.name) and not p.name.startswith(".")
    }


# ======================================================================================
# HAPPY PATH — the two engines agree on source_event_uuids and on the envelope
# ======================================================================================
def test_fold_and_rebuild_cite_the_same_source_set(store: Path) -> None:
    """The AC, on live engines and on identical input: over a never-folded ticket the fold's
    candidate set and the rebuild's full log are the same events, so the two must cite the
    same sources."""
    repo = store
    folded, rebuilt = _seed(repo, "folded twin"), _seed(repo, "rebuilt twin")
    raw = {tid: _raw_uuids(repo, tid) for tid in (folded, rebuilt)}

    assert _fold(repo, folded) == 0
    assert _rebuild.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), rebuilt, str(_tdir(repo, rebuilt)), no_commit=True
    )

    cited = {tid: set(_snapshot(repo, tid)["data"]["source_event_uuids"]) for tid in raw}
    for tid in (folded, rebuilt):
        assert cited[tid] == raw[tid], f"{tid}: cited sources do not match its raw event log"
    assert len(cited[folded]) == len(cited[rebuilt]) > 0


def test_neither_engine_cites_a_consumed_prior_snapshot(store: Path) -> None:
    """Bug ``aea0``, pinned on BOTH paths. A folded prior SNAPSHOT is absorbed STATE, never a
    source: citing it makes fsck's ``snapshot_missing_sources`` report a perfectly healthy
    ticket as damaged the moment the consumed file goes away. The rebuild had this rule from
    day one; the fold had to be taught it years later."""
    repo = store
    folded, rebuilt = _seed(repo, "second fold"), _seed(repo, "rebuild over snapshot")
    for tid in (folded, rebuilt):
        assert _fold(repo, tid) == 0
        rebar.comment(tid, "after the first fold", repo_root=str(repo))

    prior = {tid: _snapshot_uuids(repo, tid) for tid in (folded, rebuilt)}
    fresh = {tid: _raw_uuids(repo, tid, active_only=True) for tid in (folded, rebuilt)}

    assert _fold(repo, folded) == 0
    assert _rebuild.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), rebuilt, str(_tdir(repo, rebuilt)), no_commit=True
    )

    for tid in (folded, rebuilt):
        assert prior[tid], "fixture did not produce a prior SNAPSHOT to consume"
        cited = set(_snapshot(repo, tid)["data"]["source_event_uuids"])
        assert cited & prior[tid] == set(), (
            f"{tid}: the consumed prior SNAPSHOT was cited as a source (bug aea0)"
        )
        assert fresh[tid] <= cited, f"{tid}: a live event was dropped from the source list"


def test_both_engines_stamp_the_same_snapshot_envelope(store: Path) -> None:
    """One builder, so the two engines' envelopes carry the same shape — and that shape is
    asserted explicitly, not merely compared against itself."""
    repo = store
    folded, rebuilt = _seed(repo, "envelope fold"), _seed(repo, "envelope rebuild")
    assert _fold(repo, folded) == 0
    assert _rebuild.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), rebuilt, str(_tdir(repo, rebuilt)), no_commit=True
    )

    a, b = _snapshot(repo, folded), _snapshot(repo, rebuilt)
    assert set(a) == set(b), "the two engines' SNAPSHOT envelopes have different keys"
    assert set(a["data"]) == set(b["data"])
    for env in (a, b):
        assert env["event_type"] == "SNAPSHOT"
        assert env["data"]["compacted_at"] == env["timestamp"]
        for key in ("uuid", "env_id", "author"):
            assert env[key], f"envelope is missing {key}"
        for key in ("compiled_state", "source_event_uuids"):
            assert key in env["data"], f"envelope data is missing {key}"


# ======================================================================================
# EDGE — the recorded convergence, and the boolean that gates data loss
# ======================================================================================
def test_a_rebuild_ignores_a_stray_non_event_file(store: Path) -> None:
    """Recorded convergence. The rebuild's old scan filtered only dotfiles and
    ``-SYNC.json``, so a stray non-JSON file was cited in ``source_event_uuids`` under its own
    BASENAME and renamed to ``*.retired``. The shared listing requires the ``.json`` suffix the
    fold's listing always required."""
    repo = store
    tid = _seed(repo, "stray file")
    stray = _tdir(repo, tid) / "operator-notes.txt"
    stray.write_text("not an event", encoding="utf-8")

    assert _rebuild.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), tid, str(_tdir(repo, tid)), no_commit=True
    )

    assert stray.exists(), "a stray non-event file was retired by the rebuild"
    assert not (stray.parent / "operator-notes.txt.retired").exists()
    assert stray.name not in _snapshot(repo, tid)["data"]["source_event_uuids"]


def test_restore_retired_reports_a_stuck_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boolean is the data-loss guard, not decoration: a FALSE tells the caller a source
    is stuck as ``*.retired`` with its effect living only in the uncommitted SNAPSHOT, so the
    SNAPSHOT must be RETAINED."""
    good, stuck = tmp_path / "a.json", tmp_path / "b.json"
    for p in (good, stuck):
        (p.parent / (p.name + ".retired")).write_text("{}", encoding="utf-8")

    real_rename = compact_plan.os.rename

    def _rename(src: str, dst: str) -> None:
        if str(dst).endswith("b.json"):
            raise OSError("stuck")
        real_rename(src, dst)

    monkeypatch.setattr(compact_plan.os, "rename", _rename)
    assert compact_plan.restore_retired([str(good), str(stuck)]) is False
    assert good.exists(), "a restorable source was skipped after the failure"
    assert not stuck.exists()


def test_restore_retired_is_idempotent(tmp_path: Path) -> None:
    """Recovery hands it a partially-reverted tree; a source whose original is already back
    must be left alone rather than clobbered."""
    original = tmp_path / "a.json"
    original.write_text("live", encoding="utf-8")
    (tmp_path / "a.json.retired").write_text("stale", encoding="utf-8")

    assert compact_plan.restore_retired([str(original)]) is True
    assert original.read_text(encoding="utf-8") == "live"


def test_needs_folding_keeps_both_selection_arms() -> None:
    """Backfill and recurrence, asked once. Dropping either arm is a silent regression: only
    backfill makes the sweep a one-time operation; only recurrence starves small tickets of a
    first SNAPSHOT."""
    assert compact_plan.needs_folding(1, has_snap=False, threshold=10) is True  # backfill
    assert compact_plan.needs_folding(11, has_snap=True, threshold=10) is True  # recurrence
    assert compact_plan.needs_folding(1, has_snap=True, threshold=10) is False
    assert compact_plan.needs_folding(0, has_snap=False, threshold=10) is False


def test_has_snapshot_reports_an_unreadable_dir_as_unknown(tmp_path: Path) -> None:
    """``None`` is the third answer the two callers need: the sweep treats it as
    already-snapshotted, the per-close trigger declines to fire."""
    assert compact_plan.has_snapshot(str(tmp_path / "nope")) is None
    assert compact_plan.has_snapshot(str(tmp_path)) is False
    (tmp_path / "1-x-SNAPSHOT.json").write_text("{}", encoding="utf-8")
    assert compact_plan.has_snapshot(str(tmp_path)) is True


# ======================================================================================
# CONSTRUCT-UNIQUENESS GUARD — a static scan, so a copy no test executes still fails
# ======================================================================================
def test_the_snapshot_envelope_and_rename_back_appear_only_in_compact_plan() -> None:
    """A STATIC scan of the whole package: a second copy-pasted builder or rename-back that
    no test happens to execute must still fail here. That is what makes the consolidation
    durable — the class (one copy fixed, its twin left broken) cannot re-enter by imitation."""
    offenders: list[str] = []
    for module in parsed_python_files(_SRC_REBAR):
        if module.path == _OWNER:
            continue
        for why in compact_plan.offending_lines(module.source):
            offenders.append(f"{module.path.relative_to(_SRC_REBAR)}:{why}")
    assert offenders == [], (
        "a compaction construct leaked outside rebar._commands.compact_plan — route the new "
        f"site through the planner, or mark it `# compact-plan-ok: <reason>`: {offenders}"
    )


def test_exactly_one_snapshot_envelope_builder_under_src() -> None:
    """The definition-count guard, for the case the atoms are renamed rather than copied."""
    defs = [
        module.path.relative_to(_SRC_REBAR)
        for module in parsed_python_files(_SRC_REBAR)
        if "def build_snapshot_event(" in module.source
    ]
    assert defs == [_OWNER.relative_to(_SRC_REBAR)], f"more than one envelope builder: {defs}"


def test_the_scan_flags_an_envelope_builder() -> None:
    """The guard must be provable in the FLAGGING direction: a scan that only ever reports
    'no offender today' reports the same thing when its matcher is broken."""
    assert compact_plan._offending_line('            "source_event_uuids": source_uuids,')
    assert compact_plan._offending_line('        "compacted_at": snapshot_ts,')


def test_the_scan_flags_a_retired_rename_back() -> None:
    assert compact_plan._offending_line("                os.rename(retired, original)")
    assert compact_plan._offending_line("    os.rename( retired, fp)")


def test_the_scan_ignores_reads_and_the_forward_retire() -> None:
    """Consuming an envelope is not a second builder, and retiring a source forward is each
    engine's own failure policy — neither is the construct."""
    assert compact_plan._offending_line('    ts = data.get("compacted_at")') is None
    assert (
        compact_plan._offending_line('    s = snap.get("data", {}).get("source_event_uuids", [])')
        is None
    )
    assert compact_plan._offending_line("            os.rename(fp, retired)") is None
    assert compact_plan._offending_line("    x = compute(a, b)  # unrelated") is None


def test_a_reasoned_marker_suppresses_the_offence() -> None:
    assert (
        compact_plan._offending_line(
            '    "compacted_at": ts,  # compact-plan-ok: legacy fixture writer'
        )
        is None
    )


def test_a_reason_less_marker_is_itself_an_offence() -> None:
    """A bare marker would let the exception hide, so it is a violation in its own right —
    the rule ``scripts/check_raw_git_writes.py`` enforces for ``# raw-git-ok:``."""
    got = compact_plan._offending_line("    os.rename(retired, fp)  # compact-plan-ok:")
    assert got is not None and "reason" in got
