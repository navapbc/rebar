"""One derivation of every store-relative path (story 6f18-05de-beaf-42be).

Five modules independently answered "where is ``.rebar`` for this store?", and three of them
carried the SAME defect in turn — a bare ``dirname`` that stops at the CALLER, so a ``make
worktree`` view (whose ``.tickets-tracker`` is a SYMLINK into the canonical store while its
``.rebar`` is a real per-worktree directory) keyed its sidecars on the view instead of the
store. Each was fixed one site per ticket: ``da68-fc7c-068c-4c53`` (``nuclear-calm-heron``),
``93a9-66cf-e681-4f49`` (``intangible-ladyish-vicuna``), ``conscious-weighable-spittlebug``.

These tests pin the consolidation the way ``tests/unit/test_spawn_detached.py`` pins the
detached-spawn one: the behaviour is correct, AND the construct exists in exactly one place,
so a sixth copy cannot re-enter by imitation.
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

import rebar

#: Anchored on the PACKAGE, not on the module under construction: the uniqueness guards below
#: must run — and fail honestly — even when ``_store/paths.py`` does not import, because their
#: subject is the rest of the tree, not the owner.
_SRC_REBAR = Path(rebar.__file__).resolve().parent
_OWNER = _SRC_REBAR / "_store" / "paths.py"


# --------------------------------------------------------------------------------------
# Fixtures — the canonical-store/worktree-view pair, as ``make worktree`` provisions it.
# --------------------------------------------------------------------------------------
def _canonical_store(tmp_path: Path) -> str:
    """A canonical store: ``<root>/canonical-repo/.tickets-tracker``. Returns the tracker."""
    tracker = Path(os.path.realpath(tmp_path)) / "canonical-repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return str(tracker)


def _worktree_tracker(tmp_path: Path, tracker: str, name: str) -> str:
    """A worktree view of *tracker*: a real ``.rebar`` beside a ``.tickets-tracker`` SYMLINK
    into the canonical store, exactly as ``make worktree`` provisions one."""
    wt = Path(os.path.realpath(tmp_path)) / name
    (wt / ".rebar").mkdir(parents=True)
    (wt / ".tickets-tracker").symlink_to(tracker)
    return str(wt / ".tickets-tracker")


# ======================================================================================
# HAPPY PATH
# ======================================================================================
def test_a_symlinked_worktree_view_derives_the_canonical_stores_sidecars(tmp_path: Path) -> None:
    """Independence from the caller, proved by INVARIANCE: two worktree views of one store
    derive the SAME sidecar path, and it is the canonical store's — not either worktree's."""
    from rebar._store.paths import StorePaths

    canonical = _canonical_store(tmp_path)
    a = _worktree_tracker(tmp_path, canonical, "worktree-a")
    b = _worktree_tracker(tmp_path, canonical, "worktree-b")

    # precondition: the fixture really is the dangerous shape (a symlink, and a real
    # per-worktree .rebar that a bare dirname would wrongly select).
    assert os.path.islink(a), "fixture is not a symlinked worktree view"
    assert os.path.isdir(os.path.join(os.path.dirname(a), ".rebar"))
    assert os.path.realpath(a) == canonical

    for name in ("enrich-drain.lock", "compact-worker.lock", "hlc.state"):
        assert StorePaths(a).sidecar(name) == StorePaths(b).sidecar(name), (
            f"sidecar({name!r}) is keyed on the worktree"
        )
        assert StorePaths(a).sidecar(name) == StorePaths(canonical).sidecar(name), (
            f"sidecar({name!r}) is not on the canonical store"
        )
        assert not StorePaths(a).sidecar(name).startswith(os.path.dirname(a) + os.sep), (
            f"sidecar({name!r}) was written inside the ephemeral worktree"
        )


def test_store_paths_exposes_the_documented_derivations(tmp_path: Path) -> None:
    """The surface the story names: canonical, git_dir, git_common_dir, rebar_dir,
    sidecar(name), log(name) — every one a ``str``, so callers need no coercion."""
    from rebar._store.paths import StorePaths

    canonical = _canonical_store(tmp_path)
    sp = StorePaths(canonical)

    assert sp.canonical == canonical
    assert sp.rebar_dir == os.path.join(os.path.dirname(canonical), ".rebar")
    assert sp.sidecar("x.lock") == os.path.join(sp.rebar_dir, "x.lock")
    assert sp.log("x.log") == os.path.join(sp.rebar_dir, "x.log")
    for value in (
        sp.canonical,
        sp.rebar_dir,
        sp.git_dir,
        sp.git_common_dir,
        sp.sidecar("a"),
        sp.log("b"),
    ):
        assert isinstance(value, str), f"{value!r} is not a str"


# ======================================================================================
# HELD OUT — edge / boundary / contrast
# ======================================================================================
def test_a_plain_tracker_is_its_own_canonical(tmp_path: Path) -> None:
    """Negative control: with NO symlink in play the derivation must not move. This is the
    input where behaviour must NOT change, and it is what proves the symlink test above
    distinguishes broken from working rather than just asserting realpath everywhere."""
    from rebar._store.paths import StorePaths

    tracker = str(Path(os.path.realpath(tmp_path)) / "plain-repo" / ".tickets-tracker")
    os.makedirs(tracker)

    sp = StorePaths(tracker)
    assert sp.canonical == tracker
    assert sp.rebar_dir == os.path.join(os.path.dirname(tracker), ".rebar")
    assert sp.sidecar("enrich-drain.lock") == os.path.join(
        os.path.dirname(tracker), ".rebar", "enrich-drain.lock"
    )


def test_a_nonexistent_tracker_degrades_instead_of_raising(tmp_path: Path) -> None:
    """The two pre-existing helpers both wrapped canonicalisation in ``except OSError: return
    tracker`` because a sidecar derivation runs on best-effort background paths where raising
    would fail the operation that triggered it. The consolidation must keep degrading."""
    from rebar._store.paths import StorePaths

    missing = str(tmp_path / "no-such-repo" / ".tickets-tracker")
    sp = StorePaths(missing)
    assert sp.rebar_dir.endswith(".rebar")
    assert sp.sidecar("x") == os.path.join(sp.rebar_dir, "x")


def test_store_paths_is_frozen(tmp_path: Path) -> None:
    """Frozen, as the story specifies: a caller cannot mutate a derived path out from under
    the invariant (which is exactly how a caller would re-defeat the resolution)."""
    from rebar._store.paths import StorePaths

    sp = StorePaths(_canonical_store(tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        sp.tracker = "/tmp/elsewhere"  # type: ignore[misc]


def test_git_dir_of_a_normal_clone_is_the_dot_git_directory(tmp_path: Path) -> None:
    """Contrast case for the gitfile test below: when ``.git`` is a real directory the
    derivation returns it unchanged."""
    from rebar._store.paths import StorePaths

    tracker = Path(os.path.realpath(tmp_path)) / "repo" / ".tickets-tracker"
    (tracker / ".git").mkdir(parents=True)
    assert StorePaths(str(tracker)).git_dir == str(tracker / ".git")


def test_git_dir_follows_a_linked_worktrees_gitfile_pointer(tmp_path: Path) -> None:
    """``<tracker>/.git`` is a FILE holding ``gitdir: <path>`` in a linked worktree or a
    submodule. The pointer must be followed WITHOUT a git subprocess — this runs on the write
    path of every push, where a status read must stay a file read."""
    from rebar._store.paths import StorePaths

    tracker = Path(os.path.realpath(tmp_path)) / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    real_git = Path(os.path.realpath(tmp_path)) / "real-git-dir"
    real_git.mkdir()
    (tracker / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    assert StorePaths(str(tracker)).git_dir == str(real_git)


def test_git_dir_falls_back_to_dot_git_when_the_pointer_is_unreadable(tmp_path: Path) -> None:
    """An unreadable pointer degrades to ``<tracker>/.git`` — the caller's write then fails and
    is swallowed, which is the correct best-effort degradation and the PRE-EXISTING contract of
    ``push_state._git_dir``. Pinned so the consolidation cannot silently adopt one of the two
    OTHER sentinels in the tree (``""`` in gitutil, ``None`` in lock)."""
    from rebar._store.paths import StorePaths

    tracker = Path(os.path.realpath(tmp_path)) / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    (tracker / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")

    assert StorePaths(str(tracker)).git_dir == str(tracker / ".git")


# ======================================================================================
# HELD OUT — the construct-uniqueness guards
# ======================================================================================
_MARKER_RE = re.compile(r"#\s*store-path-ok:(.*)$")


def _tracker_sibling_offenders() -> list[str]:
    """Every unsanctioned tracker-sibling derivation under ``src/rebar`` outside the owner.

    The per-line rule itself lives in ``rebar._store.paths._offending_line`` and is unit-tested
    directly below; re-implementing the matcher here would make this guard a second copy of the
    very construct the guard exists to keep singular.
    """
    from rebar._store.paths import _offending_line

    offenders: list[str] = []
    for path in sorted(_SRC_REBAR.rglob("*.py")):
        if path.resolve() == _OWNER.resolve():
            continue  # the owner is exempt: refactoring INSIDE it cannot fail the guard
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            why = _offending_line(line)
            if why:
                offenders.append(f"{path.relative_to(_SRC_REBAR.parent)}:{lineno}: {why}")
    return offenders


def test_the_tracker_sibling_rebar_derivation_appears_only_in_paths() -> None:
    """A STATIC scan, not a runtime assertion: a sixth copy-pasted derivation that no test
    happens to execute must still fail here. This is what makes the consolidation durable —
    the class (one omission, replicated by imitation) cannot re-enter by copy-paste."""
    assert _tracker_sibling_offenders() == [], (
        "a tracker-sibling '.rebar' derivation leaked outside _store/paths.py — route it "
        "through rebar._store.paths.StorePaths instead, or annotate the line with "
        "'# store-path-ok: <reason>': " + repr(_tracker_sibling_offenders())
    )


def test_exactly_one_rebar_dir_definition_under_src() -> None:
    """The story's first acceptance criterion, asserted rather than left to a manual grep:
    ``grep -rn "def _rebar_dir" src/rebar`` returns only ``_store/paths.py``."""
    found = [
        f"{path.relative_to(_SRC_REBAR.parent)}:{lineno}"
        for path in sorted(_SRC_REBAR.rglob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if line.startswith("def _rebar_dir")
    ]
    assert len(found) <= 1, f"more than one _rebar_dir definition survives: {found}"
    for site in found:
        assert site.startswith("rebar/_store/paths.py"), (
            f"_rebar_dir defined outside the owner: {site}"
        )


def test_the_guard_marker_requires_a_reason(tmp_path: Path) -> None:
    """The escape hatch is only legitimate when the exception is VISIBLE in review, so a bare
    ``# store-path-ok`` with no reason is itself a violation — the same rule
    ``scripts/check_raw_git_writes.py`` enforces for ``# raw-git-ok:``."""
    assert _MARKER_RE.search("x = dirname(t) + '.rebar'  # store-path-ok: legacy shim")
    reasoned = _MARKER_RE.search("# store-path-ok: because")
    assert reasoned is not None and reasoned.group(1).strip() == "because"
    bare = _MARKER_RE.search("# store-path-ok:")
    assert bare is not None and bare.group(1).strip() == ""


# ======================================================================================
# HELD OUT — end to end through the five REAL call sites
# ======================================================================================
def test_every_real_sidecar_helper_lands_in_the_one_canonical_rebar(tmp_path: Path) -> None:
    """The teeth. Path equality on ``StorePaths`` alone would pass even if no call site were
    rewired, so this drives the FIVE production helpers on a symlinked worktree view and
    asserts every artifact lands in the canonical store's ``.rebar`` — the end-to-end contract
    the three original bugs each broke at one site.

    Note this drives ONLY production helpers — it deliberately does not import ``StorePaths``,
    so it is a true end-to-end pin: it passed before the consolidation (every site already
    resolved, per the leaf's recorded convergence status) and must keep passing after."""
    from rebar._commands import compact_trigger, doctor_locks
    from rebar.llm import enrich_drain
    from rebar.llm.overlap import queue

    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    canonical_rebar = os.path.join(os.path.dirname(canonical), ".rebar")
    worktree_rebar = os.path.join(os.path.dirname(wt), ".rebar")

    derived = {
        "compact trigger lock": compact_trigger._trigger_lock_path(wt),
        "compact sweep stamp": compact_trigger._sweep_stamp_path(wt),
        "compact trigger log": compact_trigger._trigger_log_path(wt),
        "drain lock": enrich_drain._drain_lock_path(wt),
        "drain log": enrich_drain._drain_log_path(wt),
        "overlap gate marker": queue._gate_marker_path(wt),
    }
    # doctor is driven through its PUBLIC scan: the private helper is deleted by this very
    # consolidation, so asserting on it would be a change-detector for the refactor itself.
    for row in doctor_locks.scan_locks(wt):
        if row["name"] in (
            doctor_locks.LEG_HLC,
            doctor_locks.LEG_ENRICH_DRAIN,
            doctor_locks.LEG_COMPACT_WORKER,
        ):
            derived[f"doctor {row['name']} path"] = row["path"]
    for label, got in derived.items():
        assert got.startswith(canonical_rebar + os.sep) or got == canonical_rebar, (
            f"{label} is not on the canonical store: {got}"
        )
        assert not got.startswith(worktree_rebar + os.sep), (
            f"{label} was keyed on the ephemeral worktree: {got}"
        )


def test_an_hlc_tick_from_a_worktree_view_writes_the_canonical_stores_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HLC cache is a store-wide clock. Ticking it from a worktree VIEW must persist
    ``hlc.state`` on the canonical store — a view-local cache is a second clock, which is the
    whole defect class. Asserted on the file the tick actually writes, not on a helper name,
    so a behaviour-preserving rename cannot break it."""
    from rebar._store import hlc

    monkeypatch.delenv("REBAR_HLC", raising=False)  # the clock is ON by default
    canonical = _canonical_store(tmp_path)
    wt = _worktree_tracker(tmp_path, canonical, "worktree-a")
    (Path(canonical) / "tk-1").mkdir()

    canonical_state = Path(os.path.dirname(canonical)) / ".rebar" / "hlc.state"
    worktree_rebar = Path(os.path.dirname(wt)) / ".rebar"
    assert not canonical_state.exists(), "precondition: no state yet"
    assert list(worktree_rebar.iterdir()) == [], "precondition: worktree .rebar is empty"

    tick = hlc.next_tick(wt, "tk-1")

    assert isinstance(tick, int) and tick > 0
    assert canonical_state.exists(), "the HLC cache was not written to the canonical store"
    assert not (worktree_rebar / "hlc.state").exists(), (
        "the HLC cache was written into the ephemeral worktree — a second, view-local clock"
    )


def test_the_push_marker_still_follows_a_linked_worktrees_gitdir_pointer(tmp_path: Path) -> None:
    """``push_state`` derives the GIT dir rather than the ``.rebar`` dir, and its marker must
    keep following a ``gitdir:`` pointer through the consolidation."""
    from rebar._store import push_state

    tracker = Path(os.path.realpath(tmp_path)) / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    real_git = Path(os.path.realpath(tmp_path)) / "real-git-dir"
    real_git.mkdir()
    (tracker / ".git").write_text(f"gitdir: {real_git}\n", encoding="utf-8")

    assert push_state._marker_path(str(tracker)) == str(real_git / push_state.MARKER)


# The guard's own unit tests: the tree scan above proves "no offender exists TODAY", which
# passes just as well if the scanner is broken. These exercise the classifier directly on
# synthetic lines, so the guard is proven to FLAG as well as to pass.
def test_the_scan_flags_a_tracker_sibling_join() -> None:
    """The offence the guard exists to catch: joining ".rebar" to the tracker's parent."""
    from rebar._store.paths import _offending_line

    assert _offending_line('    return os.path.join(os.path.dirname(tracker), ".rebar")')
    assert _offending_line('    return Path(tracker).resolve().parent / ".rebar"')


def test_the_scan_ignores_a_repo_root_join() -> None:
    """The negative control that makes the guard usable at all: ~36 sites legitimately join
    ".rebar" to an explicit repo_root (prompts, scratch, usage log, snapshots). Those are a
    different derivation with no symlink hazard, and a guard that flagged them would be turned
    off within a day."""
    from rebar._store.paths import _offending_line

    assert _offending_line('    pdir = Path(repo_root) / ".rebar" / "prompts"') is None
    assert _offending_line('    default = os.path.join(root, ".rebar", "usage.jsonl")') is None
    assert _offending_line("    x = compute(a, b)  # unrelated") is None


def test_a_reasoned_marker_suppresses_the_offence() -> None:
    """A legitimate second use stays possible — but only VISIBLY, with its reason in review."""
    from rebar._store.paths import _offending_line

    assert (
        _offending_line(
            '    return os.path.join(os.path.dirname(t), ".rebar")  # store-path-ok: legacy shim'
        )
        is None
    )


def test_a_reason_less_marker_is_itself_an_offence() -> None:
    """A bare marker would let the exception hide, so it is a violation in its own right --
    the same rule ``scripts/check_raw_git_writes.py`` enforces for ``# raw-git-ok:``."""
    from rebar._store.paths import _offending_line

    got = _offending_line('    return os.path.join(os.path.dirname(t), ".rebar")  # store-path-ok:')
    assert got is not None and "requires a reason" in got
