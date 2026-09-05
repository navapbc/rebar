"""H2 ECHO-LOOP EXPERIMENTS (observational, always-pass).

Question: can the reconciler be driven into an "echo loop" in which repeated
passes keep growing the local-ticket count and/or the Jira issue count?

Every test here reuses the FAITHFUL stateful Jira fake and the real git-backed
store fixtures from ``test_reconcile_idempotency.py`` (imported, not copied, so
the fake stays single-sourced). Each test:

  1. establishes a BOUND rebar<->Jira pair (seed DIG-1, run pass 1),
  2. applies ONE perturbation,
  3. runs 4 further passes,
  4. records after each pass: local ticket dir count, Jira issue count,
     the bindings dict, and the write_calls delta,
  5. PRINTS the table (run with ``-s``).

Assertions only DOCUMENT the observed steady state; a no-growth result is a
real finding, not a failure.
"""

# ruff: noqa: F811 — pytest fixtures are imported by name; ruff reads the
# parameter of every test that uses one as a redefinition.
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from test_reconcile_idempotency import (  # noqa: F401
    _FakeClient,
    _FakeJiraState,
    _make_ok_concurrency,
    git_repo,
    reconciler_modules,
)

RECONCILER_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"
)


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _run_pass(reconciler_modules, client, repo_root, monkeypatch, pass_id, *, wire_backend=True):
    """One reconcile pass over ``client`` (any _FakeClient-shaped object)."""
    fetcher, applier, reconcile = reconciler_modules
    monkeypatch.setitem(sys.modules, "acli_integration", client)
    monkeypatch.setattr(fetcher, "_load_acli", lambda: client)
    monkeypatch.setattr(applier, "_load_acli", lambda: client)
    monkeypatch.setattr(applier, "_load_concurrency", lambda: _make_ok_concurrency())
    if wire_backend:
        from rebar_reconciler.adapters.jira.backend import JiraBackend

        run_differs_mod = sys.modules["reconcile_run_differs"]
        monkeypatch.setattr(run_differs_mod, "_load_reconcile_backend", lambda: JiraBackend(client))
    return reconcile.reconcile_once(pass_id, repo_root=repo_root)


def _tracker(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker"


def _local_ticket_ids(repo_root: Path) -> list[str]:
    """Every ticket directory in the tracker (dot-dirs are bookkeeping)."""
    t = _tracker(repo_root)
    return sorted(p.name for p in t.iterdir() if p.is_dir() and not p.name.startswith("."))


def _bindings(repo_root: Path) -> dict:
    p = _tracker(repo_root) / ".bridge_state" / "bindings.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())["bindings"]


def _create_event_counts(repo_root: Path) -> dict[str, int]:
    """CREATE events per local ticket — an echo can duplicate EVENTS inside an
    existing deterministic-id dir without ever growing the dir count."""
    t = _tracker(repo_root)
    out: dict[str, int] = {}
    for tid in _local_ticket_ids(repo_root):
        out[tid] = len(list((t / tid).glob("*-CREATE.json")))
    return out


def _seed_one(state: _FakeJiraState) -> None:
    state.seed(
        "DIG-1",
        summary="Implement login",
        status={"name": "In Progress"},
        issuetype={"name": "Story"},
        priority={"name": "High"},
    )


class _Recorder:
    """Accumulates the per-pass observation table and prints it."""

    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple] = []

    def record(self, label: str, repo_root: Path, state: _FakeJiraState, writes: list[str]):
        locals_ = _local_ticket_ids(repo_root)
        issues = sorted(state.issues)
        binds = {k: v.get("jira_key") for k, v in _bindings(repo_root).items()}
        events = _create_event_counts(repo_root)
        self.rows.append(
            (label, len(locals_), locals_, len(issues), issues, binds, list(writes), events)
        )
        return self.rows[-1]

    def dump(self):
        print(f"\n\n===== {self.title} =====")
        print(f"{'pass':<14} {'#local':>6} {'#jira':>6}  bindings / locals / issues / writes")
        for label, nloc, locs, nis, iss, binds, writes, events in self.rows:
            print(f"{label:<14} {nloc:>6} {nis:>6}")
            print(f"    locals   = {locs}")
            print(f"    issues   = {iss}")
            print(f"    bindings = {binds}")
            print(f"    CREATEev = {events}")
            print(f"    writes   = {writes}")
        print(f"===== end {self.title} =====\n")


def _bootstrap_bound_pair(reconciler_modules, git_repo, monkeypatch, pass_id):
    """Seed DIG-1 and run pass 1 -> local ticket jira-dig-1 bound to DIG-1."""
    state = _FakeJiraState()
    _seed_one(state)
    client = _FakeClient(state)
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, pass_id)
    assert _local_ticket_ids(git_repo) == ["jira-dig-1"]
    assert _bindings(git_repo)["jira-dig-1"]["jira_key"] == "DIG-1"
    return state, client


def _drive(rec, reconciler_modules, client, git_repo, monkeypatch, pass_id, n=4):
    state = client._s
    for i in range(1, n + 1):
        state.write_calls.clear()
        _run_pass(reconciler_modules, client, git_repo, monkeypatch, f"{pass_id}-{i}")
        rec.record(f"pass {i + 1}", git_repo, state, state.write_calls)


def _delete_binding(git_repo: Path, local_id: str) -> None:
    p = _tracker(git_repo) / ".bridge_state" / "bindings.json"
    data = json.loads(p.read_text())
    entry = data["bindings"].pop(local_id, None)
    if entry and entry.get("jira_key"):
        data.get("reverse", {}).pop(entry["jira_key"], None)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(_tracker(git_repo)), "commit", "-aqm", "perturb: drop binding"],
        check=False,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# (a) binding dropped, rebar-id: label KEPT
# ---------------------------------------------------------------------------


def test_a_binding_dropped_label_kept(git_repo, reconciler_modules, monkeypatch):
    state, client = _bootstrap_bound_pair(reconciler_modules, git_repo, monkeypatch, "expA")
    rec = _Recorder("(a) binding DROPPED, rebar-id label KEPT")
    rec.record("pass 1", git_repo, state, state.write_calls)

    _delete_binding(git_repo, "jira-dig-1")
    assert "rebar-id:jira-dig-1" in state.issues["DIG-1"]["labels"]
    rec.record("perturbed", git_repo, state, [])

    _drive(rec, reconciler_modules, client, git_repo, monkeypatch, "expA")
    rec.dump()

    first, last = rec.rows[1], rec.rows[-1]
    print(f"(a) local {first[1]} -> {last[1]} ; jira {first[3]} -> {last[3]}")
    # OBSERVED: no growth at all. The retained rebar-id label makes the outbound
    # create's dedup JQL (dispatch_one.py ~line 154, `labels = "rebar-id:<id>"`)
    # HIT, so the pass takes the dedup-create-skipped branch and re-binds with
    # ZERO Jira writes. The inbound adopt arm stands down separately
    # (_adopt_stands_down, binding_walk.py ~405-411: the marker equals the
    # deterministic id AND a local ticket for it exists).
    assert (last[1], last[3]) == (1, 1)
    assert last[7] == {"jira-dig-1": 1}, "no duplicate CREATE event"
    assert rec.rows[2][6] == [], "the re-binding pass issues ZERO Jira writes"


# ---------------------------------------------------------------------------
# (b) label removed, binding KEPT
# ---------------------------------------------------------------------------


def test_b_label_removed_binding_kept(git_repo, reconciler_modules, monkeypatch):
    state, client = _bootstrap_bound_pair(reconciler_modules, git_repo, monkeypatch, "expB")
    rec = _Recorder("(b) rebar-id label REMOVED, binding KEPT")
    rec.record("pass 1", git_repo, state, state.write_calls)

    state.issues["DIG-1"]["labels"] = [
        lbl for lbl in state.issues["DIG-1"]["labels"] if not lbl.startswith("rebar-id")
    ]
    rec.record("perturbed", git_repo, state, [])

    _drive(rec, reconciler_modules, client, git_repo, monkeypatch, "expB")
    rec.dump()

    first, last = rec.rows[1], rec.rows[-1]
    print(f"(b) local {first[1]} -> {last[1]} ; jira {first[3]} -> {last[3]}")
    # OBSERVED: a total no-op. The binding is the authority on both sides
    # (outbound_differ ~line 588 keys create-vs-update SOLELY off it; the inbound
    # adopt arm skips any key with a binding, binding_walk.py ~line 202), so a
    # missing label alone is invisible. Note the label is never re-attached.
    assert (last[1], last[3]) == (1, 1)
    assert all(row[6] == [] for row in rec.rows[1:]), "zero writes on every pass"


# ---------------------------------------------------------------------------
# (c) BOTH binding and label removed
# ---------------------------------------------------------------------------


def test_c_binding_and_label_both_removed(git_repo, reconciler_modules, monkeypatch):
    state, client = _bootstrap_bound_pair(reconciler_modules, git_repo, monkeypatch, "expC")
    rec = _Recorder("(c) binding DROPPED and rebar-id label REMOVED")
    rec.record("pass 1", git_repo, state, state.write_calls)

    _delete_binding(git_repo, "jira-dig-1")
    state.issues["DIG-1"]["labels"] = [
        lbl for lbl in state.issues["DIG-1"]["labels"] if not lbl.startswith("rebar-id")
    ]
    rec.record("perturbed", git_repo, state, [])

    _drive(rec, reconciler_modules, client, git_repo, monkeypatch, "expC")
    rec.dump()

    first, last = rec.rows[1], rec.rows[-1]
    print(f"(c) local {first[1]} -> {last[1]} ; jira {first[3]} -> {last[3]}")
    # OBSERVED: no ticket-dir growth and no Jira-issue growth, but ONE duplicate
    # CREATE EVENT is appended into the existing dir. With no marker,
    # _adopt_stands_down returns False, so the level-triggered adopt arm re-adopts
    # DIG-1; _jira_key_to_local_id gives the SAME deterministic id `jira-dig-1`,
    # so the materialisation lands in the existing directory as a second CREATE.
    # It converges after one pass (the re-written binding + label restore the
    # steady state).
    assert (last[1], last[3]) == (1, 1)
    assert last[7] == {"jira-dig-1": 2}, "exactly ONE duplicate CREATE event, then stable"
    assert all(row[6] == [] for row in rec.rows[3:]), "converged: later passes write nothing"


# ---------------------------------------------------------------------------
# (d) OUTBOUND orphan: a locally-authored ticket whose binding AND label are gone
#     (c) already covers the inbound-created orphan exactly, so (d) uses an
#     OUTBOUND-origin ticket: created locally, pushed to Jira by a pass, then
#     stripped of both its binding and its rebar-id label — the shape a crash
#     between create_issue and the binding write leaves behind on the NEXT pass
#     once the write-ahead pending entry is also lost.
# ---------------------------------------------------------------------------


def _ensure_tracker_git(tracker: Path) -> None:
    """The reconciler's applier writes ticket events straight to disk, so the
    fixture tracker has no ``.git``; the REAL write core (event_append) requires
    one. Initialise it once so a locally-authored ticket is possible."""
    if (tracker / ".git").exists():
        return
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tracker), *a], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-q", str(tracker)], check=True)
    run("config", "user.email", "echo-experiment@example.invalid")
    run("config", "user.name", "echo-experiment")
    run("add", "-A")
    run("commit", "-qm", "baseline: tracker as reconstructed by the reconciler")


def _make_local_ticket(git_repo: Path, title: str) -> str:
    """Author a ticket in the tracker with the REAL write core (create_core).

    ``rebar.create_ticket`` resolves its tracker through a path that does not see
    this fixture's relocated store, so the experiment drives the same underlying
    ``composer.create_core`` the library wraps.
    """
    from rebar._commands import composer

    t = _tracker(git_repo)
    _ensure_tracker_git(t)
    res = composer.create_core("task", title, repo_root=git_repo, creation_channel="python")
    if isinstance(res, dict):
        local_id = res.get("id") or res.get("ticket_id") or ""
    else:
        local_id = str(res)
    if not local_id:
        # Fall back to "the newest non-jira- ticket dir".
        cands = [i for i in _local_ticket_ids(git_repo) if not i.startswith("jira-")]
        local_id = sorted(cands, key=lambda i: (t / i).stat().st_mtime)[-1]
    print(f"  [author] created local ticket {local_id!r} in {t}")
    return local_id


def test_d_outbound_orphan_local_ticket_unbound_and_unlabelled(
    git_repo, reconciler_modules, monkeypatch
):
    state = _FakeJiraState()
    _seed_one(state)
    client = _FakeClient(state)
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "expD-0")

    rec = _Recorder("(d) OUTBOUND orphan: local ticket, no binding, no label")
    rec.record("pass 1", git_repo, state, state.write_calls)

    # An outbound-origin ticket: created locally, then pushed out by a pass.
    try:
        local_id = _make_local_ticket(git_repo, "Locally authored work")
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"(d) SKIPPED - could not author a local ticket: {exc!r}")
        raise

    state.write_calls.clear()
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "expD-1")
    rec.record("pass 2(out)", git_repo, state, state.write_calls)
    pushed_key = _bindings(git_repo).get(local_id, {}).get("jira_key")
    print(f"(d) local ticket {local_id!r} pushed out as {pushed_key!r}")

    # Perturb: drop the binding AND the label on the pushed issue.
    _delete_binding(git_repo, local_id)
    if pushed_key and pushed_key in state.issues:
        state.issues[pushed_key]["labels"] = [
            lbl
            for lbl in state.issues[pushed_key].get("labels", [])
            if not lbl.startswith("rebar-id")
        ]
    rec.record("perturbed", git_repo, state, [])

    _drive(rec, reconciler_modules, client, git_repo, monkeypatch, "expD")
    rec.dump()

    before, after = rec.rows[2], rec.rows[3]
    last = rec.rows[-1]
    print(f"(d) local {before[1]} -> {last[1]} ; jira {before[3]} -> {last[3]}")
    # OBSERVED: a genuine ONE-SHOT duplication burst — +1 local ticket AND +1 Jira
    # issue, both in the SAME pass, then convergence. Two independent arms fire:
    #   * inbound: the orphaned issue is unbound and unmarked, so the adopt arm
    #     materialises a NEW local ticket `jira-dig-<n>` (echo ticket);
    #   * outbound: the local ticket is unbound, so outbound_differ (~588) emits a
    #     create, and the dedup JQL (dispatch_one ~154) MISSES because the label
    #     was stripped -> a second Jira issue.
    # Growth stops because both new entities are bound + labelled in that same pass.
    assert after[1] == before[1] + 1, "exactly one echo ticket"
    assert after[3] == before[3] + 1, "exactly one duplicate Jira issue"
    assert (last[1], last[3]) == (after[1], after[3]), "bounded: converges after ONE pass"
    assert all(row[6] == [] for row in rec.rows[4:]), "converged: later passes write nothing"
    assert any(i.startswith("jira-dig-1") for i in last[2]), last[2]


# ---------------------------------------------------------------------------
# (e) SEARCH-INDEX LAG: the dedup JQL always misses
#     Models the eventual-consistency window binding_store.py's
#     ``is_keyless_pending_within_grace`` docstring describes (JRASERVER-70423):
#     the issue EXISTS in state.issues, but `labels = "rebar-id:<id>"` returns
#     nothing, so dispatch_one.py's dedup search cannot see it.
# ---------------------------------------------------------------------------


class _LaggingIndexClient(_FakeClient):
    """A fake whose dedup JQL always returns [] (Lucene index permanently behind).

    Window searches still work — a lagging index in production degrades the
    label-equality lookup specifically, which is what the dedup path uses.
    """

    def search_issues(self, jql: str, **kwargs):
        q = jql.strip()
        # BOTH identity-label forms must lag. The base fake only models the
        # colon form; the LEGACY HYPHEN fallback in
        # binding_recovery.recover_pending_bindings ("rebar-id-<id>") falls
        # through its dedup branch and is answered as a WINDOW query, which
        # returns every open issue — and recover_pending_bindings then
        # bind_confirm()s results[0], mis-binding the pending local to an
        # unrelated key. That is a HARNESS artifact, not production behaviour
        # (real JQL label-equality cannot return an unlabelled issue), so the
        # experiment models both forms rather than measuring the artifact.
        if q.startswith('labels = "rebar-id:') or q.startswith('labels = "rebar-id-'):
            self._s.write_calls.append("identity_label_search->LAGGED(empty)")
            return []
        return super().search_issues(jql, **kwargs)


def test_e_search_index_lag_dedup_jql_always_empty(git_repo, reconciler_modules, monkeypatch):
    state = _FakeJiraState()
    _seed_one(state)
    lagging = _LaggingIndexClient(state)
    _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, "expE-0")

    rec = _Recorder(
        "(e) SEARCH-INDEX LAG + binding re-stripped EVERY pass (forced, not autonomous)"
    )
    rec.record("pass 1", git_repo, state, state.write_calls)

    # Author a local ticket so the OUTBOUND create path (the one guarded by the
    # dedup JQL) actually runs.
    try:
        local_id = _make_local_ticket(git_repo, "Outbound under index lag")
    except Exception as exc:  # pragma: no cover
        print(f"(e) SKIPPED - could not author a local ticket: {exc!r}")
        raise
    print(f"(e) authored local ticket {local_id!r}")

    state.write_calls.clear()
    _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, "expE-1")
    rec.record("pass 2(out)", git_repo, state, state.write_calls)

    # Now strip the binding BEFORE EVERY pass. This is a FORCED perturbation, not an
    # autonomous loop: the only thing that would otherwise stop a second create is the
    # `jira_key is None` create-vs-update branch in outbound_differ.py (~line 588),
    # which keys SOLELY off the binding store. Removing the binding every pass is
    # therefore the strongest possible driver, and it establishes the upper bound.
    for i in range(1, 5):
        _delete_binding(git_repo, local_id)
        state.write_calls.clear()
        _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, f"expE-loop-{i}")
        rec.record(f"loop {i}", git_repo, state, state.write_calls)

    rec.dump()
    first, last = rec.rows[1], rec.rows[-1]
    print(f"(e) local {first[1]} -> {last[1]} ; jira {first[3]} -> {last[3]}")
    # OBSERVED: under a permanently-lagging identity-label index AND a binding that
    # is re-stripped every pass, the JIRA side grows by exactly +1 per pass and never
    # converges, while the LOCAL side does not grow at all (the orphaned issues all
    # carry a foreign rebar-id marker, so _adopt_stands_down keeps the adopt arm
    # down). Nothing bounds the Jira side here: the two independent create guards —
    # the binding store (outbound_differ ~588) and the dedup JQL (dispatch_one ~154)
    # — are both defeated by construction. This is the UPPER BOUND under a forced
    # per-pass perturbation, not an autonomous echo loop.
    loops = [r for r in rec.rows if r[0].startswith("loop")]
    assert [r[3] for r in loops] == list(range(loops[0][3], loops[0][3] + len(loops))), (
        "one duplicate Jira issue per forced pass"
    )
    assert last[1] == first[1], "the LOCAL side does not grow"


# ---------------------------------------------------------------------------
# ID-FORMAT question: inbound-created tickets always carry jira-<key-lowercased>
# ---------------------------------------------------------------------------


def test_inbound_created_ticket_id_format(git_repo, reconciler_modules, monkeypatch):
    state = _FakeJiraState()
    for key, summary in (
        ("DIG-1", "Implement login"),
        ("DIG-2", "Write unit tests"),
        ("ABC-42", "Mixed case key"),
        ("proj-7", "Already-lowercase key"),
    ):
        state.seed(
            key,
            summary=summary,
            status={"name": "To Do"},
            issuetype={"name": "Task"},
            priority={"name": "Medium"},
        )
    client = _FakeClient(state)
    _run_pass(reconciler_modules, client, git_repo, monkeypatch, "idfmt")

    ids = _local_ticket_ids(git_repo)
    binds = {k: v.get("jira_key") for k, v in _bindings(git_repo).items()}
    print("\n\n===== ID FORMAT =====")
    print(f"seeded keys      = {sorted(state.issues)}")
    print(f"local ticket ids = {ids}")
    print(f"bindings         = {binds}")
    import re

    canonical = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$")
    for tid in ids:
        print(
            f"  {tid:<22} jira-prefixed={tid.startswith('jira-')} "
            f"canonical-4quad={bool(canonical.match(tid))}"
        )
    print("===== end ID FORMAT =====\n")

    for tid in ids:
        assert tid.startswith("jira-"), tid
        assert not canonical.match(tid), tid
    for key in state.issues:
        assert f"jira-{key.lower()}" in ids, (key, ids)


# ---------------------------------------------------------------------------
# (e2) / (e3) KEYLESS-PENDING under a lagging index — the exact shape
#      ``binding_store.is_keyless_pending_within_grace`` describes.
#      The crash-during-create leaves ``{"state": "pending", "jira_key": None}``.
#      Inside the 3600s grace the create must DEFER (no duplicate); once the
#      grace expires the defer lifts and a lagging dedup search cannot stop it.
# ---------------------------------------------------------------------------


def _make_keyless_pending(git_repo: Path, local_id: str, created_at: str) -> None:
    p = _tracker(git_repo) / ".bridge_state" / "bindings.json"
    data = json.loads(p.read_text())
    entry = data["bindings"].get(local_id, {})
    if entry.get("jira_key"):
        data.get("reverse", {}).pop(entry["jira_key"], None)
    data["bindings"][local_id] = {
        "jira_key": None,
        "state": "pending",
        "created_at": created_at,
    }
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_keyless_pending_experiment(
    git_repo, reconciler_modules, monkeypatch, *, created_at, title
):
    state = _FakeJiraState()
    _seed_one(state)
    lagging = _LaggingIndexClient(state)
    _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, "kp-0")

    rec = _Recorder(title)
    rec.record("pass 1", git_repo, state, state.write_calls)
    local_id = _make_local_ticket(git_repo, "Crashed mid-create")
    state.write_calls.clear()
    _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, "kp-1")
    rec.record("pass 2(out)", git_repo, state, state.write_calls)

    # Crash shape: the create landed on Jira but only the KEYLESS write-ahead
    # entry survives locally.
    # Surface the whole outbound batch when the cross-project guard trips, so an
    # aborted pass is diagnosable rather than opaque.
    _applier_mod = reconciler_modules[1]

    _real_ct = _applier_mod._cross_project_targets

    def _spy_ct(mutations, allowed):
        offenders = _real_ct(mutations, allowed)
        if offenders:
            print(f"  [guard] offenders={offenders} batch={list(mutations)}")
        return offenders

    monkeypatch.setattr(_applier_mod, "_cross_project_targets", _spy_ct)

    _make_keyless_pending(git_repo, local_id, created_at)
    rec.record("perturbed", git_repo, state, [])
    print(
        "  [perturbed bindings.json] "
        + (_tracker(git_repo) / ".bridge_state" / "bindings.json").read_text()
    )

    for i in range(1, 5):
        state.write_calls.clear()
        try:
            _run_pass(reconciler_modules, lagging, git_repo, monkeypatch, f"kp-loop-{i}")
        except Exception as exc:  # noqa: BLE001 - an aborted pass IS an observation
            print(f"  [loop {i}] pass ABORTED: {type(exc).__name__}: {exc}")
            state.write_calls.append(f"PASS-ABORTED({type(exc).__name__})")
        rec.record(f"loop {i}", git_repo, state, state.write_calls)
    rec.dump()
    return rec


def test_e2_keyless_pending_within_grace_defers(git_repo, reconciler_modules, monkeypatch):
    from datetime import datetime, timedelta, timezone

    fresh = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = _run_keyless_pending_experiment(
        git_repo,
        reconciler_modules,
        monkeypatch,
        created_at=fresh,
        title="(e2) KEYLESS-PENDING inside the 3600s grace + lagging index",
    )
    before, after = rec.rows[2], rec.rows[-1]
    print(f"(e2) local {before[1]} -> {after[1]} ; jira {before[3]} -> {after[3]}")
    # OBSERVED: NO growth. outbound_differ still emits the create (the keyless entry
    # has jira_key None), but dispatch_one.py ~148-152
    # (`is_keyless_pending_within_grace`) DEFERS the write for the whole 3600s
    # grace window, so no duplicate is ever written.
    assert (after[1], after[3]) == (before[1], before[3])
    assert not any("create_issue" in w for r in rec.rows[3:] for w in r[6]), (
        "the keyless-pending grace must suppress every create_issue"
    )


def test_e3_keyless_pending_past_grace(git_repo, reconciler_modules, monkeypatch):
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(seconds=7200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec = _run_keyless_pending_experiment(
        git_repo,
        reconciler_modules,
        monkeypatch,
        created_at=stale,
        title="(e3) KEYLESS-PENDING PAST the 3600s grace + lagging index",
    )
    before, after = rec.rows[2], rec.rows[-1]
    print(f"(e3) local {before[1]} -> {after[1]} ; jira {before[3]} -> {after[3]}")
    # OBSERVED: exactly ONE duplicate Jira issue, then convergence. Past the grace
    # the defer lifts and the lagging dedup JQL cannot see the original, so a second
    # issue is written — but its key is bind_confirm'd immediately, and from the next
    # pass on outbound_differ (~588) takes the update branch. Bounded at +1.
    assert after[1] == before[1], "the LOCAL side does not grow"
    assert after[3] == before[3] + 1, "exactly ONE duplicate Jira issue"
    assert all(row[6] == [] for row in rec.rows[4:]), "converged after one pass"
