"""A relocated store (``REBAR_TRACKER_DIR`` outside the checkout) must not make rebar
infer the repo/config root as ``os.path.dirname(tracker)``.

Sibling of ``chuffy-arbored-goldfish`` (claim + gates.py) — this pins the REMAINING
``os.path.dirname(tracker)``-as-repo-root sites (ticket auspicial-friended-merganser):
the ``transition open -> in_progress`` start-work gate, the CLI dispatcher's ``repo_root``,
scratch cleanup on delete, and the library clarity-check threshold. In a co-located
checkout ``dirname(tracker)`` == the repo root, so these tests only bite when the store is
relocated — exactly the deployed MCP-server topology (store at ``/var/gerrit/site/mcp-tickets``,
repo at ``/app``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit


def _init_relocated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A repo whose ``rebar.toml`` enables the plan-review gate, and a tracker relocated
    OUTSIDE it with NO ``rebar.toml`` anywhere up-tree — the deployed-server topology."""
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\n", encoding="utf-8"
    )
    external = tmp_path / "elsewhere" / "store"
    external.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external))
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # The store's parent — and every dir up-tree from it — must carry no rebar.toml, or
    # the config walk would find one and mask the bug (as a co-located checkout does).
    assert not (external.parent / "rebar.toml").exists()
    return repo, external


def test_transition_open_in_progress_gate_still_applies_when_tracker_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``transition open -> in_progress`` is the OTHER start-work gate path (sibling of the
    already-fixed ``claim``). ``transition_compute`` computed ``repo_root_str =
    os.path.dirname(tracker)`` and fed it to ``gates.plan_review_precheck`` as the config
    root — so on a relocated store the gate flag read from an empty config, i.e. OFF, and
    the enforcement boundary silently STOPPED APPLYING. (AC#1)
    """
    repo, _external = _init_relocated_store(tmp_path, monkeypatch)
    # A TASK — bugs/session_logs are gate-exempt, so they cannot show the gate.
    tid = rebar.create_ticket("task", "relocated-store transition gate", repo_root=str(repo))

    with pytest.raises(Exception) as exc:  # CommandError surfaces through the seam
        rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    msg = str(exc.value).lower()
    assert "plan" in msg or "review" in msg, (
        "the plan-review gate must still BLOCK `transition open -> in_progress` when the "
        "store is relocated; reading the flag from the tracker's parent resolves an empty "
        f"config and silently disables the gate. got: {exc.value!r}"
    )


def test_delete_scratch_cleanup_targets_the_repo_root_not_the_store_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete.py passed ``os.path.dirname(tracker)`` to ``scratch.cleanup_for_ticket``.

    On a relocated store that is the store's parent, not the code root, so a ticket's
    scratch under ``<repo>/.rebar/scratch`` was never cleaned. The fix passes the in-scope
    ``repo_root`` through. Capture the argument the delete path hands the cleanup helper.
    """
    from rebar._commands import delete as delete_mod

    repo, external = _init_relocated_store(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "relocated-store delete scratch", repo_root=str(repo))

    seen: list = []
    monkeypatch.setattr(
        delete_mod.scratch,
        "cleanup_for_ticket",
        lambda repo_root, ticket_id: seen.append(repo_root),
    )
    rc = delete_mod.delete_cli([tid, "--user-approved"], repo_root=str(repo))
    assert rc == 0, "delete should succeed"
    assert seen, "delete never reached scratch cleanup"
    cleaned = str(seen[0])
    assert cleaned != str(external.parent), (
        "scratch cleanup targeted the STORE's parent (os.path.dirname(tracker)); on a "
        "relocated store that never reaches the repo's .rebar/scratch"
    )
    assert cleaned == str(repo), f"cleanup should target the repo root, got {cleaned!r}"


def test_clarity_threshold_reads_repo_config_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second site in the same class: ``clarity_check`` resolved its threshold from
    ``os.path.dirname(tracker)`` — on a relocated store an empty config, so a repo that
    configured a non-default ``ticket_clarity.threshold`` silently got the built-in
    default (5) instead. With the fix the threshold discovers the repo config (REBAR_ROOT /
    git toplevel of cwd), independent of where the store lives.
    """
    repo, _external = _init_relocated_store(tmp_path, monkeypatch)
    # Override the co-located gate config with a NON-default clarity threshold.
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\n\n[ticket_clarity]\nthreshold = 9\n",
        encoding="utf-8",
    )
    tid = rebar.create_ticket("task", "clarity threshold on relocated store", repo_root=str(repo))
    # repo_root omitted → discover (REBAR_ROOT points at repo); the tracker is elsewhere.
    result = rebar.clarity_check(tid, repo_root=None)
    assert result["threshold"] == 9, (
        "clarity_check must read ticket_clarity.threshold from the repo config even when the "
        "store is relocated; resolving it from the tracker's parent found an empty config and "
        f"fell back to the default. got: {result!r}"
    )


def test_transition_gate_off_by_config_still_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config-effect contrast: with the gate flag OFF the SAME relocated-store transition
    proceeds — proving the block above is the gate firing, not an unrelated failure."""
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = false\n", encoding="utf-8"
    )
    external = tmp_path / "elsewhere" / "store"
    external.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external))
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "gate off", repo_root=str(repo))
    result = rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    assert result["to"] == "in_progress"


# ── injurious-pugnacious-azurevase: the str()-wrapped advisory config reads ──────────
#
# composer.py / composer_edit.py computed the ADVISORY description-cap warning's config
# root as ``os.path.dirname(str(tracker))``. That is the str()-wrapped variant of the same
# class: on a relocated store the tracker's parent has no rebar.toml, so the cap
# (verify.max_ticket_description_chars) and the plan-review applicability both read an empty
# config and the save-time warning was silently SUPPRESSED. The fix resolves the config root
# from the in-scope ``repo_root`` (str(config.repo_root(repo_root))). Config MUST come from a
# rebar.toml FILE, not an env var, or the read would be root-independent and hide the bug.


def _relocated_store_with_low_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """A relocated store whose CODE repo enables the claim gate AND sets a low description
    cap in its rebar.toml FILE — the two config reads the description-cap warning makes."""
    repo, external = _init_relocated_store(tmp_path, monkeypatch)
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\nmax_ticket_description_chars = 50\n",
        encoding="utf-8",
    )
    return repo, external


def test_create_description_cap_warning_fires_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#1 (create path): a create whose description exceeds the repo-configured cap must
    warn even when the store is relocated. ``cfg_root=os.path.dirname(str(tracker))`` read
    the store's parent (empty config, default 8,000 cap) and returned None."""
    repo, _external = _relocated_store_with_low_cap(tmp_path, monkeypatch)
    created = rebar.create_ticket(
        "task",
        "oversized on relocated store",
        description="D" * 80,
        return_alias=True,
        repo_root=str(repo),
    )
    warning = created["description_warning"]
    assert warning and "max_ticket_description_chars" in warning, (
        "the create-path description-cap warning must fire on a relocated store; resolving "
        "cfg_root from the tracker's parent read an empty config (8,000-char default) and "
        f"suppressed it. got: {warning!r}"
    )
    assert "80" in warning and "50" in warning, f"warning must state length and cap: {warning!r}"


def test_edit_description_cap_warning_fires_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#1 (edit path): the same for an edit that writes an oversized description —
    ``_edit_description_warning`` resolved cfg_root from the tracker's parent."""
    repo, _external = _relocated_store_with_low_cap(tmp_path, monkeypatch)
    tid = rebar.create_ticket("task", "small", description="ok", repo_root=str(repo))
    warning = rebar.edit_ticket(tid, description="D" * 80, repo_root=str(repo))
    assert warning and "max_ticket_description_chars" in warning, (
        "the edit-path description-cap warning must fire on a relocated store; resolving "
        f"cfg_root from the tracker's parent suppressed it. got: {warning!r}"
    )


def test_within_cap_still_silent_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast: a description WITHIN the repo cap stays silent — proving the warning above
    is the configured cap firing on the correct root, not an unconditional notice."""
    repo, _external = _relocated_store_with_low_cap(tmp_path, monkeypatch)
    created = rebar.create_ticket(
        "task", "within cap", description="D" * 20, return_alias=True, repo_root=str(repo)
    )
    assert created["description_warning"] is None


# ────────────── the detached compaction sweep: run_sweep's config root ──────────────
#
# ``compact_trigger.run_sweep`` is the DETACHED-CHILD compaction entry point (bug
# scathing-custommade-bobcat, 6bf1-326b-7404-4034). It held only the tracker and hardcoded
# ``repo_root = os.path.dirname(tracker)`` — the ONE config-root site auspicial-friended-merganser
# deliberately deferred, because the child outlives the worktree that spawned it and the two
# supported topologies disagreed on where a DURABLE code root lives. It now resolves one with
# ``config.compaction_child_repo_root(StorePaths(tracker).canonical)``: REBAR_ROOT (the deployed
# relocated-store topology) > dirname(canonical tracker) (the local co-located worktree, resolved
# THROUGH the .tickets-tracker symlink so the outliving child never points at a dying toplevel —
# bugs 3198/93a9). On a relocated store the buggy ``dirname(tracker)`` read the store's parent, so
# a non-default ``[compact]`` block was ignored and the sweep folded by the DEFAULT rule.
#
# Config MUST come from a rebar.toml FILE, never REBAR_COMPACT_THRESHOLD /
# REBAR_COMPACTION_HORIZON_NS
# — an env read is root-independent and would hide the bug (the suite-wide conftest sets
# REBAR_COMPACTION_HORIZON_NS=0, so these delenv it). The HORIZON is the discriminator, not the
# threshold: select_tickets' BACKFILL arm folds any snapshot-less ticket with one foldable event
# whatever the threshold, so the default 1,800 s horizon (which holds fresh events back) is what
# makes the buggy default-config sweep fold nothing.


def _relocated_store_with_compact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    compact_block: str,
    set_rebar_root: bool = True,
    comments: int = 4,
) -> tuple[Path, Path, str]:
    """A code repo carrying a ``[compact]`` block in its rebar.toml FILE and a store RELOCATED
    outside it (``REBAR_TRACKER_DIR``), seeded with one foldable ticket. Returns
    ``(repo, external_store, resolved_ticket_id)``."""
    import rebar._engine_support.resolver as _resolver

    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    # Force config to come from the FILE, not a root-independent env read (which hides the bug).
    monkeypatch.delenv("REBAR_COMPACTION_HORIZON_NS", raising=False)
    monkeypatch.delenv("REBAR_COMPACT_THRESHOLD", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "rebar.toml").write_text(compact_block, encoding="utf-8")
    external = tmp_path / "elsewhere" / "store"
    external.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external))
    if set_rebar_root:
        monkeypatch.setenv("REBAR_ROOT", str(repo))
    else:
        monkeypatch.delenv("REBAR_ROOT", raising=False)
    rebar.init_repo(repo_root=str(repo))
    assert not (external.parent / "rebar.toml").exists()
    tid = rebar.create_ticket("task", "fold me", description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return repo, external, _resolver.resolve_ticket_id(tid, str(external))


def _folds_horizon_zero() -> str:
    return "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 0\n"


def test_run_sweep_folds_by_the_repo_compact_config_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#2 behaviour: on a relocated store the sweep honours the repo's non-default
    ``[compact]`` block (horizon 0 folds fresh events). The buggy ``dirname(tracker)`` read the
    store's parent (no rebar.toml → the DEFAULT 1,800 s horizon), so the just-written events sat
    inside the horizon and NOTHING folded — no SNAPSHOT."""
    from rebar._commands import compact_trigger

    _repo, external, tid = _relocated_store_with_compact(
        tmp_path, monkeypatch, compact_block=_folds_horizon_zero()
    )
    compact_trigger.run_sweep(str(external))
    assert list((external / tid).glob("*-SNAPSHOT.json")), (
        "run_sweep did not fold under the repo's [compact] horizon=0 on a relocated store; it "
        "read the DEFAULT config from the store's parent instead of resolving the code root"
    )


def test_run_sweep_hands_the_resolved_code_root_to_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#2 seam: the root handed to ``compact_all_cli`` IS the resolved code root
    (realpath of REBAR_ROOT), pinned at the seam so a fix that merely lands on a root reading
    the same config by accident — e.g. passing ``repo_root=None`` and letting compose_config
    rediscover it downstream — is still caught."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    repo, external, _tid = _relocated_store_with_compact(
        tmp_path, monkeypatch, compact_block=_folds_horizon_zero()
    )
    seen: list = []
    monkeypatch.setattr(
        compact_mod, "compact_all_cli", lambda argv, repo_root: seen.append(repo_root)
    )
    compact_trigger.run_sweep(str(external))
    assert seen == [os.path.realpath(str(repo))], (
        "run_sweep must hand compact_all_cli the RESOLVED code root (realpath(REBAR_ROOT)); "
        f"got {seen!r} (dirname(tracker) would be the store's parent {external.parent!r})"
    )


def test_run_sweep_resolves_the_code_root_without_rebar_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#4: the SECOND precedence arm — REBAR_ROOT UNSET → ``dirname(canonical tracker)`` —
    also reaches the DURABLE code root, resolved THROUGH the worktree's ``.tickets-tracker``
    symlink. A worktree view hands the child the symlink; ``StorePaths(tracker).canonical``
    realpath-resolves it to the main checkout's store, whose parent is the durable checkout —
    NOT the ephemeral worktree ``dirname(tracker)`` would name (bugs 3198/93a9)."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    monkeypatch.delenv("REBAR_COMPACTION_HORIZON_NS", raising=False)
    monkeypatch.delenv("REBAR_COMPACT_THRESHOLD", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ROOT", raising=False)

    checkout = tmp_path / "main-checkout"
    checkout.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=checkout, check=True)
    (checkout / "rebar.toml").write_text(_folds_horizon_zero(), encoding="utf-8")
    rebar.init_repo(repo_root=str(checkout))
    store = Path(rebar.config.tracker_dir(str(checkout)))
    assert store.parent == checkout, "the co-located store must sit beside the checkout"

    # A worktree view: a .tickets-tracker SYMLINK to the canonical store, its parent NOT a
    # rebar.toml-bearing checkout. dirname(symlink) would name this ephemeral dir.
    worktree = tmp_path / "ephemeral-worktree"
    worktree.mkdir()
    symlinked_tracker = worktree / ".tickets-tracker"
    symlinked_tracker.symlink_to(store, target_is_directory=True)

    seen: list = []
    monkeypatch.setattr(
        compact_mod, "compact_all_cli", lambda argv, repo_root: seen.append(repo_root)
    )
    compact_trigger.run_sweep(str(symlinked_tracker))
    assert seen == [str(checkout)], (
        "with REBAR_ROOT unset, run_sweep must resolve the DURABLE code root as "
        "dirname(canonical tracker) = the main checkout, not the ephemeral worktree that "
        f"dirname(tracker)={worktree!r} would name; got {seen!r}"
    )


def test_run_sweep_respects_a_repo_config_that_folds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#5 config-effect contrast: the SAME store and ticket fold NOTHING under a ``[compact]``
    block whose horizon puts the just-written events out of reach — proving the fold in AC#2 is
    the repo config being READ, not an unconditional sweep. Also asserts LIVENESS: ``run_sweep``
    swallows every exception, so an absent SNAPSHOT would otherwise be satisfied by a crash
    BEFORE the sweep. The seam spy proves compact_all_cli was actually reached."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    # A horizon so large that no just-written event is old enough to fold.
    never = "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 100000000000000000\n"
    repo, external, tid = _relocated_store_with_compact(tmp_path, monkeypatch, compact_block=never)

    original = compact_mod.compact_all_cli
    reached: list = []

    def _spy(argv, repo_root):
        reached.append(repo_root)
        return original(argv, repo_root=repo_root)

    monkeypatch.setattr(compact_mod, "compact_all_cli", _spy)
    compact_trigger.run_sweep(str(external))

    assert reached, (
        "LIVENESS: run_sweep must actually REACH compact_all_cli — it swallows every "
        "exception, so an early crash would leave this empty and the no-snapshot assertion "
        "below would pass vacuously"
    )
    assert reached == [os.path.realpath(str(repo))], (
        "and it must reach it with the RESOLVED code root, not dirname(tracker) — the store's "
        f"parent {external.parent!r}. got: {reached!r}"
    )
    assert not list((external / tid).glob("*-SNAPSHOT.json")), (
        "the repo's [compact] horizon puts the events out of reach, so the sweep must fold "
        "NOTHING; a snapshot means the fold ignored the configured horizon"
    )
