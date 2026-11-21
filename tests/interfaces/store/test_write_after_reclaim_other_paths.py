"""The remaining write paths on a clone that a reclaim superseded (follow-up to bug
smiling-ancient-finwhale).

The comment/transition/claim paths heal a superseded clone and retry. These tests cover
the other writers: delete, the NDJSON importer, a reconciler pass, and the store-freshness
gates that run before a completion close, a gated claim and a plan review. Each fails on a
tree that only heals the first three paths. The heal-failure tests prove a clone the heal
could not move stays refused as DIVERGED and loses no event.
"""

from __future__ import annotations

import io
import subprocess
import types
from pathlib import Path

import pytest
from reclaim_harness import (
    added_events,
    build_shared_store,
    git,
    publish_unrelated,
    remote_tip,
    supersede,
)

import rebar
from rebar import _cli
from rebar._commands._seam import CommandError
from rebar._store import freshness, lock, stale_override, sync


@pytest.fixture
def shared_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, str]:
    return build_shared_store(tmp_path, monkeypatch)


def _on_reclaimed_lineage(tracker: Path, baseline: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", baseline, "HEAD"],
            cwd=tracker,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, capsys
) -> None:
    repo, tracker, origin, _seed = shared_store
    doomed = rebar.create_ticket("task", "to delete", repo_root=str(repo))
    baseline = supersede(tracker, origin, tmp_path)

    rc = _cli.main(["delete", doomed, "--user-approved"])

    assert rc == 0, capsys.readouterr().err
    statuses = [p for p in added_events(origin, baseline, "-STATUS.json") if p.startswith(doomed)]
    assert len(statuses) == 1, statuses


def test_delete_against_an_unrelated_remote_is_still_refused(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, capsys
) -> None:
    repo, tracker, origin, _seed = shared_store
    doomed = rebar.create_ticket("task", "to delete", repo_root=str(repo))
    before_remote = publish_unrelated(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    rc = _cli.main(["delete", doomed, "--user-approved"])

    assert rc != 0
    assert "DIVERGED" in capsys.readouterr().err
    assert git("rev-parse", "HEAD", cwd=tracker) == before
    assert git("status", "--porcelain", cwd=tracker) == ""
    assert remote_tip(origin) == before_remote


# ── NDJSON import ─────────────────────────────────────────────────────────────


def _export_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Export one ticket from an unrelated source repo, then point REBAR_ROOT back."""
    src = tmp_path / "src"
    src.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "s@t.co"), ("config", "user.name", "s")):
        git(*args, cwd=src)
    previous = str(rebar.config.repo_root_or_none())
    monkeypatch.setenv("REBAR_ROOT", str(src))
    rebar.init_repo(repo_root=str(src))
    rebar.create_ticket("task", "imported task", repo_root=str(src))
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(src))
    monkeypatch.setenv("REBAR_ROOT", previous)
    return buf.getvalue().splitlines()


def test_import_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, tracker, origin, _seed = shared_store
    lines = _export_lines(tmp_path, monkeypatch)
    baseline = supersede(tracker, origin, tmp_path)

    result = rebar.import_tickets(lines, repo_root=str(repo))

    assert result["created"] == 1
    assert len(added_events(origin, baseline, "-CREATE.json")) == 1


def test_import_against_an_unrelated_remote_is_still_refused(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, tracker, origin, _seed = shared_store
    lines = _export_lines(tmp_path, monkeypatch)
    before_remote = publish_unrelated(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    # The importer surfaces the seam refusal as CommandError, not RebarError.
    with pytest.raises(CommandError, match="DIVERGED"):
        rebar.import_tickets(lines, repo_root=str(repo))

    assert git("rev-parse", "HEAD", cwd=tracker) == before
    assert remote_tip(origin) == before_remote


# ── reconciler pass ──────────────────────────────────────────────────────────


class _NeverRaised(Exception):
    """The stub applier's reschedule class; nothing in these passes raises it."""


def _run_pass_writing_one_event(
    repo: Path, tracker: Path, ticket: str, monkeypatch: pytest.MonkeyPatch
):
    """Run a real guarded-pass body whose reconcile step writes one inbound event."""
    from rebar.reconciler import guarded_pass, inbound_translate

    written: list[Path] = []

    def _reconcile_once(*_args, **_kwargs):
        written.append(
            inbound_translate._write_event_file(tracker, ticket, "COMMENT", {"body": "from jira"})
        )
        return {}

    steps = {
        "reconcile": types.SimpleNamespace(reconcile_once=_reconcile_once),
        "applier": types.SimpleNamespace(RescheduleError=_NeverRaised, EXIT_RESCHEDULE=75),
    }
    monkeypatch.setattr(guarded_pass, "_try_load_step", steps.get)
    monkeypatch.setattr(guarded_pass, "_project_visibility_preflight", lambda *_a: None)
    monkeypatch.setattr(guarded_pass, "_classify_reconcile_result", lambda result: result)
    result = guarded_pass.run_pass_result(repo_root=repo, pass_id="reclaim-pass")
    return result, written


def test_reconciler_pass_on_a_superseded_clone_writes_on_the_reclaimed_baseline(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = supersede(tracker, origin, tmp_path)

    _result, written = _run_pass_writing_one_event(repo, tracker, seed, monkeypatch)

    assert len(written) == 1 and written[0].is_file()
    assert _on_reclaimed_lineage(tracker, baseline)


def test_reconciler_pass_against_an_unrelated_remote_writes_nothing(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, tracker, origin, seed = shared_store
    publish_unrelated(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    _result, written = _run_pass_writing_one_event(repo, tracker, seed, monkeypatch)

    assert written == []
    assert git("rev-parse", "HEAD", cwd=tracker) == before
    assert git("status", "--porcelain", cwd=tracker) == ""


# ── store-freshness gates (completion close, gated claim, plan review) ───────


def _enable_gate(repo: Path, key: str) -> None:
    """Turn one [verify] gate on in the repo's own config."""
    toml = repo / "rebar.toml"
    existing = toml.read_text(encoding="utf-8") if toml.exists() else ""
    toml.write_text(f"{existing}\n[verify]\n{key} = true\n", encoding="utf-8")


class _VerifierReached(Exception):
    """Raised in place of the billable verifier: the precheck got past its freshness gate."""


@pytest.mark.parametrize("sync_pull", ["on", "off"])
def test_completion_close_heals_a_superseded_clone_before_its_gates(
    shared_store: tuple[Path, Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_pull: str,
) -> None:
    """The close's own forced material refresh (``_refresh_close_material``) reconverges
    before either close gate, so a superseded clone reaches the verifier on the healed store."""
    from rebar._commands import close_precheck

    repo, tracker, origin, seed = shared_store
    rebar.transition(seed, "open", "in_progress", repo_root=str(repo))
    _enable_gate(repo, "require_completion_verification_for_close")
    # sync.pull=off skips the close's forced material refresh, the only heal it had.
    monkeypatch.setenv("REBAR_SYNC_PULL", sync_pull)
    baseline = supersede(tracker, origin, tmp_path)

    def _verifier(*_a, **_k):
        raise _VerifierReached

    monkeypatch.setattr(close_precheck, "_verify_with_duration_metrics", _verifier)
    with pytest.raises(_VerifierReached):
        rebar.transition(seed, "in_progress", "closed", repo_root=str(repo))

    assert _on_reclaimed_lineage(tracker, baseline)


class _ContextReached(Exception):
    """Raised in place of review context assembly: review-plan got past its stale check."""


@pytest.mark.parametrize("sync_pull", ["on", "off"])
def test_review_plan_heals_a_superseded_clone_before_its_stale_check(
    shared_store: tuple[Path, Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_pull: str,
) -> None:
    """review-plan's relation snapshot syncs before ``_stale_store_verdict``."""
    from rebar import llm
    from rebar.llm.plan_review import context_assembly

    repo, tracker, origin, seed = shared_store
    baseline = supersede(tracker, origin, tmp_path)
    monkeypatch.setenv("REBAR_SYNC_PULL", sync_pull)

    def _assemble(*_a, **_k):
        raise _ContextReached

    monkeypatch.setattr(context_assembly, "assemble_context", _assemble)
    with pytest.raises(_ContextReached):
        llm.review_plan(seed, sign=False, repo_root=str(repo))

    assert _on_reclaimed_lineage(tracker, baseline)


def test_gated_claim_on_a_superseded_clone_is_not_refused_as_stale(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    _enable_gate(repo, "require_plan_review_for_claim")
    baseline = supersede(tracker, origin, tmp_path)

    with pytest.raises(rebar.RebarError) as exc:
        rebar.claim(seed, repo_root=str(repo))

    # The claim now fails only because no plan-review attestation exists.
    assert "cannot start work" in str(exc.value)
    assert "DIVERGED" not in str(exc.value) and freshness.STALE_VERDICT not in str(exc.value)
    assert _on_reclaimed_lineage(tracker, baseline)


def test_gated_start_work_transition_on_a_superseded_clone_is_not_refused_as_stale(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    _enable_gate(repo, "require_plan_review_for_claim")
    baseline = supersede(tracker, origin, tmp_path)

    with pytest.raises(rebar.RebarError) as exc:
        rebar.transition(seed, "open", "in_progress", repo_root=str(repo))

    assert "cannot start work" in str(exc.value)
    assert "DIVERGED" not in str(exc.value) and freshness.STALE_VERDICT not in str(exc.value)
    assert _on_reclaimed_lineage(tracker, baseline)


def test_claim_review_gate_check_sees_the_healed_store(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claim --review`` checks the gate before reviewing; on a superseded clone that check
    must not read ``stale-store`` (which would buy a needless review)."""
    from rebar import llm
    from rebar._commands import claim as claim_cmd

    repo, tracker, origin, seed = shared_store
    _enable_gate(repo, "require_plan_review_for_claim")
    baseline = supersede(tracker, origin, tmp_path)
    seen: list[dict] = []
    real_check = llm.claim_gate_check

    def _spy(*a, **k):
        seen.append(real_check(*a, **k))
        return seen[-1]

    def _review(*_a, **_k):
        raise _ContextReached

    monkeypatch.setattr(llm, "claim_gate_check", _spy)
    monkeypatch.setattr(llm, "review_plan", _review)
    with pytest.raises(_ContextReached):
        claim_cmd._review_before_claim(seed, str(tracker), str(repo))

    assert seen and seen[0].get("verdict") != freshness.STALE_VERDICT, seen
    assert _on_reclaimed_lineage(tracker, baseline)


def test_read_only_claim_gate_check_still_moves_nothing(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    from rebar.llm.plan_review.attest_gate import claim_gate_check

    repo, tracker, origin, seed = shared_store
    supersede(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    check = claim_gate_check(seed, repo_root=str(repo))

    assert check.get("verdict") == freshness.STALE_VERDICT, check
    assert git("rev-parse", "HEAD", cwd=tracker) == before


def test_completion_close_gate_still_refuses_an_unrelated_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    publish_unrelated(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    with pytest.raises(CommandError, match="DIVERGED"):
        freshness.assert_gate_store_fresh("the completion close gate", seed, str(repo))

    assert git("rev-parse", "HEAD", cwd=tracker) == before


# ── a heal that fails stays refused and loses nothing ────────────────────────


def _unpushed_local_event(tracker: Path, ticket: str) -> str:
    rel = f"{ticket}/1700000000000000000-0000cafe-0000-4000-8000-00000000cafe-COMMENT.json"
    (tracker / rel).write_text('{"kept": true}\n', encoding="utf-8")
    git("add", rel, cwd=tracker)
    git("commit", "-q", "--no-verify", "-m", "unpushed local event", cwd=tracker)
    return rel


def _refusal(op: str, repo: Path, tracker: Path, seed: str, lines: list[str], monkeypatch, capsys):
    """Run one write path on a superseded clone whose heal cannot run; return its refusal."""
    if op == "comment":
        with pytest.raises(rebar.RebarError) as exc:
            rebar.comment(seed, "must not be stranded", repo_root=str(repo))
        return str(exc.value)
    if op == "delete":
        assert _cli.main(["delete", seed, "--user-approved"]) != 0
        return capsys.readouterr().err
    if op == "import":
        with pytest.raises(CommandError) as exc:
            rebar.import_tickets(lines, repo_root=str(repo))
        return str(exc.value)
    if op == "reconciler":
        result, written = _run_pass_writing_one_event(repo, tracker, seed, monkeypatch)
        assert written == []
        return str(result.details.get("error", ""))
    if op == "close":
        from rebar._commands import close_precheck

        _enable_gate(repo, "require_completion_verification_for_close")
        git("add", "-A", cwd=repo)
        monkeypatch.setenv("REBAR_SYNC_PULL", "off")

        def _verifier(*_a, **_k):
            raise _VerifierReached

        monkeypatch.setattr(close_precheck, "_verify_with_duration_metrics", _verifier)
        with pytest.raises(rebar.RebarError) as exc:
            rebar.transition(seed, "in_progress", "closed", repo_root=str(repo))
        return str(exc.value)
    if op == "gated-claim":
        _enable_gate(repo, "require_plan_review_for_claim")
        git("add", "-A", cwd=repo)  # keep the config edit out of the tracker's status check
        with pytest.raises(rebar.RebarError) as exc:
            rebar.claim(seed, repo_root=str(repo))
        return str(exc.value)
    raise AssertionError(op)


def _assert_refused_and_intact(
    op: str, repo: Path, tracker: Path, origin: Path, seed: str, lines, monkeypatch, capsys
) -> None:
    kept = _unpushed_local_event(tracker, seed)
    before = git("rev-parse", "HEAD", cwd=tracker)
    before_remote = remote_tip(origin)

    refusal = _refusal(op, repo, tracker, seed, lines, monkeypatch, capsys)

    assert "DIVERGED" in refusal, refusal
    assert git("rev-parse", "HEAD", cwd=tracker) == before
    assert (tracker / kept).is_file()
    assert git("status", "--porcelain", cwd=tracker) == ""
    assert remote_tip(origin) == before_remote
    backups = git("for-each-ref", stale_override.BACKUP_REF_PREFIX, cwd=tracker)
    assert backups == "", "a heal that did not run must leave no override backup"


_OPS = ["comment", "delete", "import", "reconciler", "gated-claim", "close"]
# The sites that heal through heal_if_superseded's short lock budget.
_SHORT_BUDGET_OPS = ["import", "reconciler", "gated-claim", "close"]


@pytest.mark.parametrize("op", _OPS)
def test_heal_with_a_failed_fetch_stays_refused_and_loses_no_event(
    shared_store: tuple[Path, Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    op: str,
) -> None:
    repo, tracker, origin, seed = shared_store
    lines = _export_lines(tmp_path, monkeypatch) if op == "import" else []
    if op == "close":
        rebar.transition(seed, "open", "in_progress", repo_root=str(repo))
    supersede(tracker, origin, tmp_path)
    monkeypatch.setattr(sync, "_coordinated_fetch", lambda *_a, **_k: False)

    _assert_refused_and_intact(op, repo, tracker, origin, seed, lines, monkeypatch, capsys)


@pytest.mark.parametrize("op", _SHORT_BUDGET_OPS)
def test_heal_with_a_busy_write_lock_stays_refused_and_loses_no_event(
    shared_store: tuple[Path, Path, Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    op: str,
) -> None:
    repo, tracker, origin, seed = shared_store
    lines = _export_lines(tmp_path, monkeypatch) if op == "import" else []
    if op == "close":
        rebar.transition(seed, "open", "in_progress", repo_root=str(repo))
    supersede(tracker, origin, tmp_path)
    real_reconverge = sync.reconverge
    monkeypatch.setattr(sync, "reconverge", lambda t, **_k: real_reconverge(t, lock_timeout=1))
    calls: list[str] = []
    real = sync._do_reconverge
    monkeypatch.setattr(sync, "_do_reconverge", lambda *a: calls.append("reconverged") or real(*a))
    holder = lock.acquire(str(tracker), timeout=5, attempts=1)
    try:
        # The write's own commit would wait on the lock too; the precheck refuses first.
        _assert_refused_and_intact(op, repo, tracker, origin, seed, lines, monkeypatch, capsys)
    finally:
        holder.release()
    assert calls == [], "the heal must not have reconverged while the lock was held"
