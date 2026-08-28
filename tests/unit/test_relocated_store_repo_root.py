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
    threshold: ``select_tickets``'s BACKFILL arm selects any snapshot-less ticket with at
    least one foldable event whatever the threshold, so only a horizon that puts these
    just-written events out of reach can hold the sweep back."""
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
    assert not list((external / tid).glob("*-SNAPSHOT.json")), (
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
