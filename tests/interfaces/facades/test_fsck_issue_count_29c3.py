"""Bug 29c3-b025-04d7-454e (sugarcane-scrummy-arctichare) — ``fsck --output json``
``issue_count`` must equal the COUNTED subset so it AGREES with the exit code, and an
uninitialised store must be distinguishable from a clean one.

Before this fix ``issue_count`` was ``len(issues)``: it counted every emitted ``KIND:``
line, including the never-counted kinds (``push_pending``, ``status_fork_resolved``,
``tracker_dirty_tmp_event``, ``warn``) that contribute nothing to the exit code — so a JSON
consumer gating on ``issue_count > 0`` reached a different verdict than a shell consumer
gating on the exit code against the same store. The fix keeps ``issues[]`` complete, stamps
each item with an additive ``counted`` boolean, and redefines ``issue_count`` as the counted
subset.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as fsck_mod
from rebar._commands.fsck import _transform_json
from rebar._errors import RebarError
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir

pytestmark = pytest.mark.interface


_COUNTED = "MISSING_CREATE: aaaa-bbbb-cccc-dddd — no CREATE event found"

# One representative text line per never-counted kind (is_issue=False).
_NEVER_COUNTED = {
    "push_pending": (
        "PUSH_PENDING: local 'tickets' branch is ahead of origin/tickets by 2 "
        "commit(s) — push pending"
    ),
    "status_fork_resolved": (
        "STATUS_FORK_RESOLVED: aaaa-bbbb-cccc-dddd — concurrent claim/status race "
        "resolved (dropped uuid=x)"
    ),
    "tracker_dirty_tmp_event": (
        "TRACKER_DIRTY_TMP_EVENT: 1 path(s): .tmp-event-x — orphaned event staging file(s)"
    ),
    "warn": "WARN: .git/index.lock exists (younger than 5 minutes) — not removed",
}


def _payload(*lines: str) -> dict:
    # The trailing summary line is realistic and skipped by the transform.
    return json.loads(_transform_json("\n".join([*lines, "fsck complete: N issues found"])))


def _seed_missing_create(repo: Path, ticket_id: str = "aaaa-bbbb-cccc-dddd") -> str:
    d = Path(layout_ticket_dir(repo / ".tickets-tracker", ticket_id))
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001-comment.json").write_text(
        json.dumps({"type": "COMMENT", "ticket_id": ticket_id, "body": "orphan"})
    )
    return ticket_id


def _bare_git(dirpath: Path) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=dirpath, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=dirpath, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=dirpath, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=dirpath, check=True)
    return dirpath


# ── the additive per-issue counted flag ───────────────────────────────────────
def test_each_issue_carries_a_counted_boolean() -> None:
    payload = _payload(_COUNTED, *_NEVER_COUNTED.values())
    assert payload["issues"], "expected findings"
    for item in payload["issues"]:
        assert "counted" in item, item
        assert isinstance(item["counted"], bool), item


def test_counted_kind_is_counted_true() -> None:
    payload = _payload(_COUNTED)
    entry = next(i for i in payload["issues"] if i["kind"] == "missing_create")
    assert entry["counted"] is True
    assert payload["issue_count"] == 1


@pytest.mark.parametrize(("kind", "line"), list(_NEVER_COUNTED.items()))
def test_never_counted_kind_is_flagged_and_excluded(kind: str, line: str) -> None:
    payload = _payload(_COUNTED, line)
    # completeness: the never-counted finding is STILL present in issues[]
    entry = next(i for i in payload["issues"] if i["kind"] == kind)
    assert entry["counted"] is False
    assert len(payload["issues"]) == 2, "issues[] must retain every emitted finding"
    # but it does NOT inflate the count — only the single counted finding is tallied
    assert payload["issue_count"] == 1


# ── issue_count is the counted subset, not len(issues) ────────────────────────
def test_issue_count_is_counted_subset_not_len_issues() -> None:
    payload = _payload(_COUNTED, *_NEVER_COUNTED.values())
    assert len(payload["issues"]) == 1 + len(_NEVER_COUNTED)  # full findings list retained
    assert payload["issue_count"] == 1  # only the one counted finding
    assert payload["issue_count"] == sum(1 for i in payload["issues"] if i["counted"])


def test_clean_store_payload_is_zero() -> None:
    payload = _payload()
    assert payload["issues"] == []
    assert payload["issue_count"] == 0


def test_cli_json_issue_count_agrees_with_exit_code(rebar_repo: Path, capsys) -> None:
    # The library/MCP surface: fsck_cli --output json in-process (the CLI top-level
    # blocks uninitialised stores earlier, but the library calls fsck_cli directly).
    _seed_missing_create(rebar_repo)
    rc = fsck_mod.fsck_cli(["--output=json"], repo_root=str(rebar_repo), no_mutate=True)
    payload = json.loads(capsys.readouterr().out)
    # the JSON verdict and the shell verdict must never disagree
    assert (rc == 1) == (payload["issue_count"] > 0)
    assert payload["issue_count"] == sum(1 for i in payload["issues"] if i["counted"])


# ── an uninitialised store is distinguishable from a clean one ────────────────
def test_uninitialised_store_json_is_distinguishable_from_clean(tmp_path: Path, capsys) -> None:
    bare = tmp_path / "not-a-store"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bare, check=True)
    rc = fsck_mod.fsck_cli(["--output=json"], repo_root=str(bare), no_mutate=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    kinds = {i["kind"] for i in payload["issues"]}
    assert "not_initialized" in kinds, payload
    # NOT byte-identical to a clean store's {issues:[],fixed:[],issue_count:0}
    assert payload != {"issues": [], "fixed": [], "issue_count": 0}
    assert payload["issue_count"] == 1  # agrees with the exit code


def test_fsck_report_still_fails_closed_on_uninitialised_store(tmp_path: Path) -> None:
    bare = _bare_git(tmp_path / "bare")
    with pytest.raises(RebarError) as exc:
        rebar.fsck_report(repo_root=str(bare))
    msg = str(exc.value)
    assert "could not scan" in msg, exc.value
    # The not_initialized finding's ``detail`` must be SURFACED in the error, not
    # swallowed — that is what distinguishes an uninitialised store from a bland
    # "empty scan" and gives the caller the remediation hint.
    assert "not initialized" in msg, exc.value
    assert "rebar init" in msg, exc.value


def test_missing_tracker_json_carries_dir_mismatch_warn_alongside_not_initialized(
    tmp_path: Path, capsys
) -> None:
    # The dir-mismatch branch of _missing_tracker_result: the configured tracker.dir is
    # absent, but a default-named ``.tickets-tracker`` store exists alongside it. The JSON
    # payload must carry BOTH the counted ``not_initialized`` finding AND the never-counted
    # WARN about the mismatch, and ``issue_count`` must still equal the exit code (1).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".tickets-tracker").mkdir()  # a default-named store sits alongside
    absent = repo / "custom-tracker-dir"  # configured tracker.dir, absent
    rc = fsck_mod._missing_tracker_result(str(absent), "json")
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    by_kind = {i["kind"]: i for i in payload["issues"]}
    assert by_kind["not_initialized"]["counted"] is True
    assert "warn" in by_kind, payload  # the tracker.dir mismatch WARN rode alongside
    assert by_kind["warn"]["counted"] is False
    assert "changed without migrating" in by_kind["warn"]["detail"]
    assert payload["issue_count"] == 1  # only not_initialized is counted; agrees with rc


# ── drift guard: _NEVER_COUNTED_KINDS must stay in lock-step with the per-site ────
# ── is_issue=False flags, or the JSON count silently disagrees with the exit code ─
# (plan-review G6). The exit code sums is_issue at production while JSON counted-ness
# derives from the kind; these tests keep the two definitions from diverging.
def test_never_counted_tracker_dirty_kinds_single_sourced_from_specs() -> None:
    # The tracker-dirty portion is DERIVED from _DIRTY_LINE_SPECS (not a hand copy), so a
    # future counted=False dirty class flows in automatically and a counted=True one never
    # leaks in. Round-trip both directions against the production specs.
    from rebar._commands import fsck_tracker_health

    specs = fsck_tracker_health._DIRTY_LINE_SPECS
    spec_never = {k.lower() for _key, k, _b, counted in specs if not counted}
    spec_counted = {k.lower() for _key, k, _b, counted in specs if counted}
    assert spec_never <= fsck_mod._NEVER_COUNTED_KINDS, "a counted=False dirty class is missing"
    assert spec_counted.isdisjoint(fsck_mod._NEVER_COUNTED_KINDS), (
        "a counted=True dirty class leaked in"
    )
    # The derived set must be LOWERCASED, because the stamping site compares against
    # ``item['kind']`` which _transform_json lowercases. Assert the set matches the
    # lowercased specs (not the raw UPPERCASE literals) so the case-fold seam is covered.
    assert fsck_mod._NEVER_COUNTED_TRACKER_DIRTY_KINDS == spec_never
    assert all(k == k.lower() for k in fsck_mod._NEVER_COUNTED_KINDS)


def test_case_fold_seam_uppercase_spec_kind_is_not_counted() -> None:
    # End-to-end guard for the exact G6 case-fold defect: feed each is_issue=False dirty
    # class's RAW UPPERCASE kind (as production emits it) as a KIND: line and confirm the
    # transform stamps it counted=False. A missing .lower() at the derivation would make
    # 'tracker_dirty_tmp_event' not in {'TRACKER_DIRTY_TMP_EVENT'} → counted=True here.
    from rebar._commands import fsck_tracker_health

    for _key, kind, _blurb, counted in fsck_tracker_health._DIRTY_LINE_SPECS:
        if counted:
            continue
        payload = _payload(f"{kind}: 1 path(s): x — report-only dirty class")
        assert payload["issues"][0]["counted"] is False, kind
        assert payload["issue_count"] == 0, kind


def test_never_counted_kinds_is_exactly_scan_plus_dirty_components() -> None:
    # Guard against a raw literal being appended to the union that bypasses either the
    # derived dirty set or the explicit scan set — the whole constant must remain the union
    # of its two documented components, nothing more.
    assert (
        fsck_mod._NEVER_COUNTED_KINDS
        == fsck_mod._NEVER_COUNTED_SCAN_KINDS | fsck_mod._NEVER_COUNTED_TRACKER_DIRTY_KINDS
    )
    # The scan/health kinds are the known inline is_issue=False decisions; a NEW one added
    # at a production site without updating this set is exactly the drift this pins against.
    assert fsck_mod._NEVER_COUNTED_SCAN_KINDS == {"push_pending", "status_fork_resolved", "warn"}


def test_counted_tally_equals_exit_code_tally_for_every_never_counted_kind() -> None:
    # Behavioural invariant: for a payload mixing one counted finding with EVERY
    # never-counted kind, the counted subset (JSON issue_count) equals the number of
    # is_issue=True findings (the exit-code tally = 1). A never-counted kind wrongly marked
    # counted=True would break this — so this fails loudly instead of drifting silently.
    payload = _payload(_COUNTED, *_NEVER_COUNTED.values())
    counted_items = sum(1 for i in payload["issues"] if i["counted"])
    assert payload["issue_count"] == counted_items == 1
    assert len(payload["issues"]) == 1 + len(_NEVER_COUNTED)  # full findings list still intact
