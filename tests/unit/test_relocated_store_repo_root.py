"""Keep configuration rooted at the code checkout when the store is relocated.

The suite covers the transition start-work gate, CLI dispatch, delete scratch
cleanup, and the clarity threshold. Fixtures separate the tracker from the code
root because a colocated store would hide incorrect ``dirname(tracker)`` usage.
"""

from __future__ import annotations

import json
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
    """Keep the transition start-work gate active for a relocated tracker.

    Its plan review precheck must read configuration from the code root rather
    than the tracker's parent.
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
    """Send the code root to ticket scratch cleanup.

    A relocated tracker's parent does not contain ``<repo>/.rebar/scratch``. The
    test captures the argument passed by the delete path.
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
    """Read the clarity threshold from the code repository configuration.

    A relocated tracker has no local ``ticket_clarity.threshold``. Resolution
    therefore uses ``REBAR_ROOT`` or the current Git top level, not the store.
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


# Description-cap warnings must read ``rebar.toml`` from the code root. Using
# ``dirname(str(tracker))`` suppresses both the configured cap and plan review
# applicability when the tracker is relocated. A file-based setting keeps this
# test sensitive to root selection.


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
    """Warn when a create exceeds the code repository's description cap."""
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


# ``run_sweep`` must pass the resolved code root to ``compact_all_cli``. Using
# the tracker's parent makes a relocated store read default compaction settings
# instead of the repository's ``[compact]`` block. Resolution follows explicit
# root, ``REBAR_ROOT``, then the detached child's Git top level. File-based
# settings keep the test sensitive to that root.


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
    """Apply the code repository's compaction settings to a relocated store."""
    from rebar._commands import compact_trigger

    _repo, external, tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    assert not list((external / tid).glob("*-SNAPSHOT.json")), "precondition: never folded"

    compact_trigger.run_sweep(str(external))

    assert list((external / tid).glob("*-SNAPSHOT.json")), (
        "the sweep did not fold an eligible ticket: it resolved its config root from the "
        "STORE's parent, read the default threshold/horizon instead of the repo's "
        "[compact] block, and folded the right tickets by the wrong rule"
    )


def test_run_sweep_hands_the_resolved_code_root_to_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require ``run_sweep`` to pass the resolved code root across the seam.

    An outcome-only assertion could pass through accidental config rediscovery.
    """
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
    """Prove that folding follows repository configuration rather than running unconditionally.

    The horizon supplies the contrast because the backfill arm selects a
    snapshot-less ticket regardless of threshold.
    """
    from rebar._commands import compact as compact_mod
    from rebar._commands import compact_trigger

    repo, external, tid = _relocated_store_folding_everything(tmp_path, monkeypatch)
    (repo / "rebar.toml").write_text(
        "[compact]\nthreshold = 1\nCOMPACTION_HORIZON_NS = 3600000000000\n", encoding="utf-8"
    )
    # Record a clean return from the production sweep because ``run_sweep`` swallows
    # exceptions and an absent snapshot alone would not prove liveness.
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
    assert not list((external / tid).glob("*-SNAPSHOT.json")), (
        "the sweep folded events its repo config puts INSIDE the compaction horizon"
    )


def test_run_sweep_resolves_the_code_root_without_rebar_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise Git top-level fallback with ``REBAR_ROOT`` unset.

    The detached child runs from the canonical store parent, which anchors the
    durable code checkout rather than the ephemeral worktree. This end-to-end
    test verifies the compaction effect but survives passing ``repo_root=None``
    because ``compact_all_cli`` can rediscover the same root. The paired seam
    test below verifies the exact argument.
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

    assert list((external / tid).glob("*-SNAPSHOT.json")), (
        "with REBAR_ROOT unset the sweep failed to resolve the code root from the git "
        "toplevel of its anchored cwd, so it fell back to default compaction config"
    )


# The fallback effect test can pass if ``compact_all_cli`` rediscovers a discarded
# root from its current directory. This seam assertion records the exact argument.
# A worktree symlink makes ``dirname(tracker)`` differ from the realpath-resolved
# code root, so the fixture distinguishes the two implementations.


def _colocated_symlinked_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Model the symlinked store topology created by ``make worktree``.

    Return the main checkout and worktree tracker symlink. The symlink makes its
    lexical parent the ephemeral worktree while its resolved parent remains the
    main checkout selected by ``_proc.detached_child_cwd``.
    """
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
    """Pin the fallback argument to the resolved code root.

    It must be neither ``None`` nor the symlink's lexical parent. The test uses
    the fixture's store symlink and detached-child anchor, wrapping only
    ``compact_all_cli`` to capture the seam value.
    """
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


# ``ensure_fresh`` and ``_load_scratch`` must resolve ``sync.pull``,
# ``tickets.branch``, and ``scratch.base_dir`` from the code root. Building a
# root from the relocated tracker's resolved or absolute path reads empty config.


def _reads_relocated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return a code root and tracker in separate trees.

    ``REBAR_ROOT`` pins the code checkout. These read paths accept the tracker
    directly, so the fixture does not initialize a store.
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
    """Default scratch reads to ``<code_root>/.rebar/scratch``.

    The payload exists only there, so using the store parent returns no data.
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
    """Resolve ``sync.pull`` and ``tickets.branch`` from the code root.

    Capture the root received by both configuration readers.
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


# Fsck and freshness read ``tickets.branch`` and ``tickets.remote`` from
# ``rebar.toml``. Capture their roots to keep configuration on the code checkout
# while Git operations continue to target the store.


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
