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

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir

pytestmark = pytest.mark.unit


def _ticket_dir(tracker: Path, tid: str) -> Path:
    return Path(layout_ticket_dir(tracker, tid))


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


# ── scathing-custommade-bobcat: the detached compaction sweep's config root ──────────
#
# ``run_sweep`` is the DETACHED-CHILD compaction entry point and the LAST sanctioned site of
# this class. It composed ``repo_root = os.path.dirname(tracker)`` and handed that to
# ``compact_all_cli`` as the config root, so on a relocated store the sweep read DEFAULT
# compaction config (threshold 10, 30-minute horizon) instead of the project's ``[compact]``
# block: it folded the RIGHT tickets by the WRONG rule. The fix RESOLVES the code root the way
# every other config reader does — bare ``config.repo_root_or_none()`` (explicit > REBAR_ROOT >
# git toplevel of the cwd, which for the detached child is the DURABLE canonical-store parent
# that ``_proc.detached_child_cwd`` anchors it to). Config MUST come from a rebar.toml FILE,
# not ``REBAR_COMPACT_THRESHOLD`` / ``REBAR_COMPACTION_HORIZON_NS``, or the read would be
# root-independent and hide the bug.


def _relocated_store_folding_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """A relocated store whose CODE repo configures a fold-everything ``[compact]`` block, and
    one seeded ticket with four comments. Returns ``(repo, external, resolved_ticket_id)``."""
    monkeypatch.delenv("REBAR_COMPACT_THRESHOLD", raising=False)
    monkeypatch.delenv("REBAR_COMPACTION_HORIZON_NS", raising=False)
    repo, external = _init_relocated_store(tmp_path, monkeypatch)
    # threshold 1 + horizon 0 fold anything; the DEFAULTS (10 / 1800 s) fold nothing here,
    # so which root the sweep reads is observable in whether a SNAPSHOT appears.
    (repo / "rebar.toml").write_text(
        "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 0\n", encoding="utf-8"
    )
    tid = rebar.create_ticket("task", "sweep me", description="x" * 60, repo_root=str(repo))
    for i in range(4):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    resolved = rebar._engine_support.resolver.resolve_ticket_id(tid, str(external))
    return repo, external, resolved


def test_run_sweep_folds_by_the_repo_compact_config_on_relocated_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#2: the detached sweep must honour the CODE repo's ``[compact]`` config when the
    store is relocated. With ``repo_root = os.path.dirname(tracker)`` it read the store's
    parent — no rebar.toml, so the built-in defaults — and folded nothing."""
    from rebar._commands import compact_trigger

    _repo, external, tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    assert not list(_ticket_dir(external, tid).glob("*-SNAPSHOT.json")), (
        "precondition: never folded"
    )

    compact_trigger.run_sweep(str(external))

    assert list(_ticket_dir(external, tid).glob("*-SNAPSHOT.json")), (
        "the sweep did not fold an eligible ticket: it resolved its config root from the "
        "STORE's parent, read the default threshold/horizon instead of the repo's "
        "[compact] block, and folded the right tickets by the wrong rule"
    )


def test_run_sweep_hands_the_resolved_code_root_to_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SPECIFIC behaviour, pinned at the seam so no other guard can rescue it: the root
    ``run_sweep`` passes to ``compact_all_cli`` is the RESOLVED code root, never the store's
    parent. Asserting only the fold outcome would survive a fix that happened to land on a
    root that reads the same config by accident."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    repo, external, _tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        compact_mod, "compact_all_cli", lambda argv, *, repo_root=None: seen.append(repo_root) or 0
    )

    compact_trigger.run_sweep(str(external))

    assert seen, "run_sweep never reached the sweep"
    handed = str(seen[0])
    assert handed != str(external.parent), (
        "run_sweep handed the sweep the STORE's parent (os.path.dirname(tracker)); on a "
        "relocated store that directory holds no rebar.toml"
    )
    assert handed == str(repo), f"expected the resolved code root {str(repo)!r}, got {handed!r}"


def test_run_sweep_respects_a_repo_config_that_folds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config-effect contrast: the SAME relocated store and the SAME seeded ticket fold
    nothing when the repo's ``[compact]`` block says so — proving the fold above is the repo
    config being read, not an unconditional sweep. The horizon is the discriminator, not the
    threshold: ``_scan_snapshot_state`` (``compact.py``) asks ``compact_plan.needs_folding``,
    whose BACKFILL arm selects any snapshot-less ticket with at least one foldable event
    whatever the threshold, so only a horizon that puts these just-written events out of
    reach can hold the sweep back."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    repo, external, tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    (repo / "rebar.toml").write_text(
        "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 3600000000000\n", encoding="utf-8"
    )
    # LIVENESS. run_sweep swallows every exception (`except Exception: logger.warning`), so
    # "no SNAPSHOT" alone would also be satisfied by a sweep that crashed or stood aside —
    # the assertion would pass for the wrong reason. Wrap the REAL sweep (never replace it)
    # to record that it ran to a clean return code.
    real = compact_mod.compact_all_cli
    outcome: list = []

    def _recording(argv, *, repo_root=None):
        rc = real(argv, repo_root=repo_root)
        outcome.append(rc)
        return rc

    monkeypatch.setattr(compact_mod, "compact_all_cli", _recording)

    compact_trigger.run_sweep(str(external))

    assert outcome == [0], (
        f"the sweep did not run to a clean return code, so the absence of a SNAPSHOT below "
        f"proves nothing about the configured horizon. got: {outcome!r}"
    )
    assert not list(_ticket_dir(external, tid).glob("*-SNAPSHOT.json")), (
        "the sweep folded events its repo config puts INSIDE the compaction horizon"
    )


def test_run_sweep_resolves_the_code_root_without_rebar_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER precedence arm — the one the local ``make worktree`` topology rides on.

    Every test above resolves through ``REBAR_ROOT``, which is the DEPLOYED arm. Locally
    nothing exports it: the detached child gets there on ``repo_root_or_none``'s git-toplevel
    fallback, read from the cwd ``_proc.detached_child_cwd`` anchored at the canonical store's
    parent (proven separately in ``test_detached_child_cwd.py`` to be the durable main
    checkout, not the ephemeral worktree). With ``REBAR_ROOT`` unset and the cwd standing in
    for that anchor, the sweep must STILL read the code repo's ``[compact]`` block — otherwise
    the fix only works on the deployment and the local half of the argument is unproven.

    RETAINED DELIBERATELY (a027-d86c AC#3), not deleted for being weak: this is the
    fold-EFFECT (end-to-end) half of the fallback-arm contract and the config-effect contrast
    for its horizon. It is END-TO-END only, so it survives the ``repo_root=None`` mutant — the
    resolved root is discarded but ``compact_all_cli`` re-discovers the same root from this
    test's cwd and folds by accident. The SEAM half that closes that gap is
    ``test_run_sweep_seam_pins_the_resolved_code_root_on_the_fallback_arm`` below; the two are
    kept as a pair.
    """
    from rebar._commands import compact_trigger

    _repo, external, tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    repo = _repo
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    # The store's parent is NOT a git repo, so a toplevel probe from THERE finds nothing —
    # only the anchored cwd can supply the code root.
    assert not (external.parent / ".git").exists()
    monkeypatch.chdir(repo)

    compact_trigger.run_sweep(str(external))

    assert list(_ticket_dir(external, tid).glob("*-SNAPSHOT.json")), (
        "with REBAR_ROOT unset the sweep failed to resolve the code root from the git "
        "toplevel of its anchored cwd, so it fell back to default compaction config"
    )


# ── a027-d86c: the SEAM assertion for run_sweep's FALLBACK arm ────────────────────────
#
# The end-to-end fallback test above pins the fold EFFECT but survives the ``repo_root=None``
# mutant: with the cwd inside the code repo, discarding the resolved root and passing ``None``
# lets ``compact_all_cli`` re-discover the same root and fold by accident. Only a SEAM
# assertion — recording the exact ``repo_root`` value ``run_sweep`` hands ``compact_all_cli`` —
# kills that mutant on this arm (the ``REBAR_ROOT`` seam test above kills it only on the
# deployed arm). The topology below is a REAL worktree symlink over a co-located store so that
# ``dirname(tracker)`` (the ephemeral worktree) is distinguishable from the realpath-resolved
# code root; a mock or a co-located dir without the symlink cannot tell the two arms apart.


def _colocated_symlinked_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """The LOCAL topology the fallback arm actually rides on: a canonical store CO-LOCATED at
    ``<main-checkout>/.tickets-tracker`` inside a git repo whose ``rebar.toml`` folds
    everything, reached through an ephemeral worktree's ``.tickets-tracker`` SYMLINK — exactly
    what ``make worktree`` provisions and what ``scathing-custommade-bobcat`` verified
    out-of-band. Returns ``(main_checkout, worktree_tracker_symlink)``.

    The symlink is load-bearing, not decoration: ``os.path.dirname(worktree_tracker)`` is the
    EPHEMERAL worktree, while ``realpath(worktree_tracker)``'s parent — the anchor
    ``_proc.detached_child_cwd`` picks — is the main checkout (the code root). A co-located dir
    with no symlink cannot tell those two apart, so it could not distinguish the resolved-root
    arm from the ``dirname(tracker)`` arm (a027-d86c AC#4)."""
    monkeypatch.delenv("REBAR_COMPACT_THRESHOLD", raising=False)
    monkeypatch.delenv("REBAR_COMPACTION_HORIZON_NS", raising=False)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    checkout = tmp_path / "main-checkout"
    checkout.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=checkout, check=True)
    (checkout / "rebar.toml").write_text(
        "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 0\n", encoding="utf-8"
    )
    # REBAR_ROOT is set ONLY to init the store here; each test deletes it to force the
    # git-toplevel fallback arm under measurement.
    monkeypatch.setenv("REBAR_ROOT", str(checkout))
    rebar.init_repo(repo_root=str(checkout))
    canonical_tracker = checkout / ".tickets-tracker"
    assert canonical_tracker.is_dir(), "precondition: the store is co-located in the checkout"
    worktree = tmp_path / "ephemeral-worktree"
    worktree.mkdir()
    worktree_tracker = worktree / ".tickets-tracker"
    worktree_tracker.symlink_to(canonical_tracker)
    return checkout, worktree_tracker


def test_run_sweep_seam_pins_the_resolved_code_root_on_the_fallback_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEAM assertion for the FALLBACK arm (``REBAR_ROOT`` unset). The root ``run_sweep`` hands
    ``compact_all_cli`` on this arm must be the RESOLVED code root — never ``None`` (which the
    end-to-end ``test_run_sweep_resolves_the_code_root_without_rebar_root`` lets
    ``compact_all_cli`` re-discover from the same cwd, folding by accident: the mutant a027-d86c
    reports as surviving) and never ``os.path.dirname(tracker)`` (the ephemeral worktree on this
    symlinked topology, which holds no ``rebar.toml``).

    Real symlinked store, real ``_proc.detached_child_cwd`` anchor — the resolution is not
    mocked; only ``compact_all_cli`` is wrapped to record the value handed across the seam."""
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger
    from rebar._proc import detached_child_cwd

    checkout, worktree_tracker = _colocated_symlinked_store(tmp_path, monkeypatch)

    # The REAL detached-child anchor: the canonical store's parent reached THROUGH the worktree
    # symlink — the main checkout, deliberately not the ephemeral worktree that dirname() sees.
    anchor = detached_child_cwd(str(worktree_tracker))
    assert anchor == os.path.realpath(checkout), "precondition: the anchor is the code root"
    assert anchor != os.path.dirname(str(worktree_tracker)), (
        "precondition: the symlink makes dirname(tracker) (the ephemeral worktree) differ from "
        "the resolved code root — the property that makes the dirname(tracker) mutant killable"
    )

    monkeypatch.delenv("REBAR_ROOT", raising=False)  # force the git-toplevel fallback arm
    monkeypatch.chdir(anchor)

    seen: list = []
    monkeypatch.setattr(
        compact_mod,
        "compact_all_cli",
        lambda argv, *, repo_root=None: seen.append(repo_root) or 0,
    )

    compact_trigger.run_sweep(str(worktree_tracker))

    assert seen, "run_sweep never reached the sweep on the fallback arm"
    handed = seen[0]
    assert handed is not None, (
        "run_sweep handed the sweep repo_root=None: the resolved code root was discarded, so "
        "compact_all_cli re-discovers it from cwd and folds by accident — the repo_root=None "
        "mutant the end-to-end AC#4 test survives"
    )
    assert str(handed) != os.path.dirname(str(worktree_tracker)), (
        "run_sweep handed the sweep os.path.dirname(tracker) — the EPHEMERAL worktree, which "
        "holds no rebar.toml; on a symlinked/relocated store that reads the wrong [compact] rule"
    )
    assert str(handed) == os.path.realpath(checkout), (
        f"expected the resolved code root {os.path.realpath(checkout)!r}, got {str(handed)!r}"
    )


# ── flowered-basaltic-beagle: the reads.py realpath()/abspath() config reads ─────────
#
# ``ensure_fresh`` and ``_load_scratch`` (reads.py) each read a ``compose_config()``-derived
# value from a root that was composed from the tracker path:
#   * ``ensure_fresh`` — ``sync.pull`` (via ``_sync_disabled(os.path.dirname(realpath(tracker)))``)
#     and ``tickets.branch`` (via ``tickets_branch(os.path.dirname(tracker_abs))``).
#   * ``_load_scratch`` — ``scratch.base_dir`` (default under
#     ``os.path.dirname(abspath(tracker))``).
# These are the realpath()/abspath() members of the class (feisty's guard blind spot). On a
# relocated store the tracker's parent holds no rebar.toml, so each read resolved an empty
# config; the fix routes all three through ``config.repo_root_or_none()`` (the CODE root).


def _reads_relocated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A CODE checkout and a store in SEPARATE trees, code root pinned via ``REBAR_ROOT``.

    Returns ``(code_root, tracker)``. The tracker's parent (``mcp-tickets/``) is deliberately
    NOT the code root, so a site that composes a root from the tracker path lands in the wrong
    tree. Lighter than ``_init_relocated_store`` — these reads.py sites take an explicit
    ``tracker`` and never open the store, so no ``rebar init`` is needed.
    """
    code_root = tmp_path / "mcp-code"
    store = tmp_path / "mcp-tickets"
    tracker = store / "tickets"
    tracker.mkdir(parents=True)
    code_root.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(code_root))
    return code_root, tracker


def test_load_scratch_reads_scratch_under_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_load_scratch`` (scratch.base_dir site, reads.py) must default the scratch base to
    ``<code_root>/.rebar/scratch``. The payload is placed ONLY under the code root, so a site
    that instead composes ``<store_parent>/.rebar/scratch`` (the pre-fix
    ``os.path.dirname(os.path.abspath(tracker))``) finds nothing — RED pre-fix (empty dict),
    GREEN post-fix (the payload is read). (AC#1)
    """
    from rebar._engine_support import reads

    code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    ticket_id = "postwar-bardic-walleye"
    scratch_dir = code_root / ".rebar" / "scratch" / ticket_id
    scratch_dir.mkdir(parents=True)
    (scratch_dir / "note").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "value": "code-root scratch"}),
        encoding="utf-8",
    )

    data = reads._load_scratch(ticket_id)

    assert data.get("note", {}).get("value") == "code-root scratch", (
        "scratch.base_dir was resolved from the store's parent, not the code root: "
        f"{data!r} (tracker={tracker}, code_root={code_root})"
    )


def test_ensure_fresh_resolves_sync_and_branch_from_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ensure_fresh`` reads ``sync.pull`` (via ``_sync_disabled``) and ``tickets.branch`` (via
    ``tickets_branch``). Both must be resolved with the CODE repo root. We capture the ``root``
    each receives and assert it is the code root — RED pre-fix (the store's parent, the
    ``os.path.dirname(...realpath(tracker))`` spelling), GREEN post-fix. (AC#1)
    """
    import os as _os

    from rebar._engine_support import reads

    code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    expected_root = _os.path.realpath(str(code_root))
    seen: dict = {}

    def _capture_sync_disabled(root):
        seen["sync_disabled_root"] = root
        return False  # proceed past the sync.pull short-circuit

    def _capture_tickets_branch(root=None):
        seen["tickets_branch_root"] = None if root is None else _os.fspath(root)
        return "definitely-not-a-real-branch"  # git rev-parse --verify fails -> return

    monkeypatch.setattr(reads, "_sync_disabled", _capture_sync_disabled)
    monkeypatch.setattr("rebar.config.tickets_branch", _capture_tickets_branch)

    reads.ensure_fresh(str(tracker))

    assert seen.get("sync_disabled_root") == expected_root, (
        "sync.pull was read from the store's parent, not the code root: "
        f"{seen.get('sync_disabled_root')!r} != {expected_root!r}"
    )
    assert seen.get("tickets_branch_root") == expected_root, (
        "tickets.branch was read from the store's parent, not the code root: "
        f"{seen.get('tickets_branch_root')!r} != {expected_root!r}"
    )


def test_ensure_fresh_local_read_context_skips_root_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-git fast path is preserved: inside ``_LOCAL_READ_CONTEXT`` (or ``no_sync``)
    ``ensure_fresh`` returns BEFORE resolving any root, so a local read pays no root-discovery
    cost. Guards the short-circuit-before-resolve ordering the fix depends on."""
    from rebar._engine_support import reads

    _code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    called: list = []
    monkeypatch.setattr(reads, "_sync_disabled", lambda root: called.append(root) or False)

    token = reads._LOCAL_READ_CONTEXT.set(True)
    try:
        reads.ensure_fresh(str(tracker))
    finally:
        reads._LOCAL_READ_CONTEXT.reset(token)
    assert called == [], "a local-context read must not resolve sync.pull at all"

    reads.ensure_fresh(str(tracker), no_sync=True)
    assert called == [], "a no_sync read must not resolve sync.pull at all"


# ── flowered-basaltic-beagle: fsck/freshness tickets.branch/tickets.remote config reads ──
#
# The plan-review gate (G1G2/G6) correctly identified that fsck_tracker_health and freshness
# READ compose_config() — `config.tickets_branch()` / `config.tickets_remote()` resolve
# `tickets.branch` / `tickets.remote` from rebar.toml — so by the approved principle #1 they
# resolve the CODE repo root, exactly like reads.py:249. They were initially mis-bucketed as
# store-root git ops; these oracles pin the corrected code-root resolution (RED against the
# `dirname(realpath(tracker))` spelling, GREEN once each resolves via repo_root_or_none()).


def _capture_branch_remote_roots(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record the ``root`` each ``config.tickets_branch`` / ``tickets_remote`` call receives,
    returning dummy values so the caller's git probe stands down."""
    import os as _os

    from rebar import config as _config

    seen: dict = {"branch": [], "remote": []}

    def _branch(root=None):
        seen["branch"].append(None if root is None else _os.fspath(root))
        return "definitely-not-a-real-branch"

    def _remote(root=None):
        seen["remote"].append(None if root is None else _os.fspath(root))
        return "definitely-not-a-real-remote"

    monkeypatch.setattr(_config, "tickets_branch", _branch)
    monkeypatch.setattr(_config, "tickets_remote", _remote)
    return seen


def test_freshness_remote_ref_reads_branch_remote_from_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``freshness._remote_ref`` resolves the store's remote-tracking ref from
    ``tickets.remote`` / ``tickets.branch`` — CODE-repo config. On a relocated store it must
    read the code root, not the tracker's parent. (principle #1)"""
    import os as _os

    from rebar._store import freshness

    code_root, _tracker = _reads_relocated_store(tmp_path, monkeypatch)
    expected = _os.path.realpath(str(code_root))
    seen = _capture_branch_remote_roots(monkeypatch)

    freshness._remote_ref()

    assert seen["remote"] == [expected] and seen["branch"] == [expected], (
        f"freshness._remote_ref read tickets.remote/branch from the store's parent: {seen!r} "
        f"(expected code root {expected!r})"
    )


def test_tracker_sync_status_reads_branch_remote_from_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fsck_tracker_health._tracker_sync_status`` reads the same CODE-repo config to name the
    store's remote-tracking ref for the health probe."""
    import os as _os

    from rebar._commands import fsck_tracker_health as fth

    code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    expected = _os.path.realpath(str(code_root))
    seen = _capture_branch_remote_roots(monkeypatch)

    fth._tracker_sync_status(str(tracker))

    assert seen["branch"] == [expected] and seen["remote"] == [expected], (
        f"_tracker_sync_status read tickets.branch/remote from the store's parent: {seen!r} "
        f"(expected code root {expected!r})"
    )


def test_configured_remote_ref_reads_branch_remote_from_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fsck_tracker_health._configured_remote_ref`` composes ``<remote>/<branch>`` from the
    CODE-repo config, not the tracker's parent."""
    import os as _os

    from rebar._commands import fsck_tracker_health as fth

    code_root, _tracker = _reads_relocated_store(tmp_path, monkeypatch)
    expected = _os.path.realpath(str(code_root))
    seen = _capture_branch_remote_roots(monkeypatch)

    fth._configured_remote_ref()

    assert seen["remote"] == [expected] and seen["branch"] == [expected], (
        f"_configured_remote_ref read tickets.remote/branch from the store's parent: {seen!r} "
        f"(expected code root {expected!r})"
    )


def test_branch_mismatch_fallback_reads_branch_from_the_code_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fsck_tracker_health._branch_mismatch`` prefers an explicitly-threaded ``repo_root``
    (the code root fsck already passes); its FALLBACK (repo_root=None) must ALSO be the code
    root, not the tracker's parent."""
    import os as _os

    from rebar._commands import fsck_tracker_health as fth

    code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    expected = _os.path.realpath(str(code_root))
    seen = _capture_branch_remote_roots(monkeypatch)

    fth._branch_mismatch(str(tracker))  # repo_root defaults to None -> fallback path

    assert seen["branch"] == [expected], (
        f"_branch_mismatch fallback read tickets.branch from the store's parent: {seen!r} "
        f"(expected code root {expected!r})"
    )


def test_branch_mismatch_prefers_explicit_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast: when fsck threads an explicit ``repo_root`` it wins over the fallback —
    proving the fallback change did not break the threaded-root path."""
    from rebar._commands import fsck_tracker_health as fth

    _code_root, tracker = _reads_relocated_store(tmp_path, monkeypatch)
    threaded = str(tmp_path / "explicit-code-root")
    seen = _capture_branch_remote_roots(monkeypatch)

    fth._branch_mismatch(str(tracker), repo_root=threaded)

    assert seen["branch"] == [threaded], (
        f"an explicitly threaded repo_root must be used verbatim: {seen!r} (expected {threaded!r})"
    )
