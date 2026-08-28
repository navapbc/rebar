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
