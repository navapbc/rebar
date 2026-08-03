"""Tests for rebar_reconciler/reconcile.py reconcile_once().

Covers:
- Idempotency: two consecutive reconcile_once() calls with unchanged remote
  both produce mutation_count=0 (second call sees prev==curr snapshot).
- EXCLUDED_FIELDS filter: a change only in an excluded field produces
  mutation_count=0 (excluded fields do not drive mutations).
- Remote field convergence: a Jira-side field change is detected without being
  planned as an outbound REVERT of itself, while a never-before-seen key is still
  planned (the regression guard against over-filtering).
- Snapshot absence: a key that leaves the fetch window is never resurrected by an
  outbound create (ADR 0028, ticket d103).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def reconcile_mod():
    """Load reconcile.py, failing all tests if absent."""
    if not RECONCILE_PATH.exists():
        pytest.fail(
            f"reconcile.py not found at {RECONCILE_PATH} — implement the module to make tests pass."
        )
    return _load_module("reconcile", RECONCILE_PATH)


@pytest.fixture(scope="module")
def fetcher_mod():
    """Load fetcher.py."""
    return _load_module("reconcile_fetcher", FETCHER_PATH)


@pytest.fixture(scope="module")
def applier_mod():
    """Load applier.py."""
    return _load_module("reconcile_applier", APPLIER_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_acli_module(issues: list[dict]) -> object:
    """Return a FAITHFUL stub acli_integration module over a mutable Jira state.

    Ticket robe-creek-zealot: the previous stub returned the same static issue
    list for every search and dropped every write on the floor. Real Jira
    REFLECTS writes on subsequent reads — in particular the ``rebar-id:``
    label / ``local_id`` entity-property write-back performed by
    ``_apply_inbound_create`` is visible in the NEXT pass's search snapshot.
    A stub that never reflects those writes silently under-tests idempotency
    (the differ's own-write-back echo path is never exercised). All client
    instances created from this module share one mutable state dict.
    """
    import copy as _copy
    import json as _json

    # key -> issue dict ({"key": ..., "fields": {...}}); deep-copied so the
    # caller's literal is never mutated by reflected writes.
    state: dict[str, dict] = {i["key"]: _copy.deepcopy(i) for i in issues if i.get("key")}
    entity_props: dict[str, dict] = {}
    created_counter = [0]

    def _labels(key: str) -> list:
        return state[key].setdefault("fields", {}).setdefault("labels", [])

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def search_issues(self, jql: str, **kwargs) -> list[dict]:
            # create_one's dedup JQL: labels = "rebar-id:<local_id>"
            if jql.strip().startswith('labels = "rebar-id:'):
                want = jql.split('"')[1]
                return [
                    _json.loads(_json.dumps(i))
                    for i in state.values()
                    if want in (i.get("fields", {}).get("labels") or [])
                ]
            return [_json.loads(_json.dumps(i)) for i in state.values()]

        def create_issue(self, fields: dict) -> dict:
            created_counter[0] += 1
            key = f"DIG-NEW-{created_counter[0]}"
            state[key] = {
                "key": key,
                "fields": {
                    "summary": fields.get("title") or fields.get("summary", ""),
                    "status": {"name": "To Do"},
                    "labels": [],
                },
            }
            return {"key": key}

        def update_issue(self, key: str, **fields) -> dict:
            # F3: applier unpacks fields as kwargs (real signature is
            # update_issue(jira_key, **kwargs)).
            if key in state:
                state[key].setdefault("fields", {}).update(fields)
            return {"key": key}

        def transition_issue(self, key: str, status: str) -> None:
            if key in state:
                state[key].setdefault("fields", {})["status"] = {"name": status}

        def add_label(self, key: str, label: str) -> None:
            if key in state and label not in _labels(key):
                _labels(key).append(label)

        def remove_label(self, key: str, label: str) -> None:
            if key in state and label in _labels(key):
                _labels(key).remove(label)

        def add_comment(self, key: str, body: str) -> dict:
            return {"id": "stub-comment"}

        def get_comments(self, key: str) -> list:
            return []

        def set_entity_property(self, key: str, prop: str, value) -> None:
            entity_props.setdefault(key, {})[prop] = value

        def delete_issue(self, key: str) -> None:
            state.pop(key, None)

        def unassign_issue(self, key: str) -> None:
            return None

        def transition_issue_by_name(self, key: str, target: str) -> None:
            self.transition_issue(key, target)

    # S4: _load_acli returns the transport (client) instance directly. All call
    # sites (fetcher + applier) share this one instance and thus one mutable
    # ``state`` (the closure the methods above capture), preserving write-reflection.
    return _Client()


def _make_ok_concurrency() -> types.ModuleType:
    """Return a stub _concurrency module that always reports ok=True."""
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class _ConcurrencyEvent:
        kind: str
        message: str = ""
        attempt: int = 0

    @dataclass
    class _Result:
        ok: bool
        event: _ConcurrencyEvent | None = None
        value: Any = None

    def _snapshot_head(repo_root: Path) -> str:
        return "aabbccdd" * 5

    def _rebase_retry(repo_root, write_fn, *, max_attempts=3):
        write_fn()
        return _Result(ok=True)

    fake = types.ModuleType("_concurrency")
    fake.ConcurrencyEvent = _ConcurrencyEvent
    fake.Result = _Result
    fake.snapshot_head = _snapshot_head
    fake.rebase_retry = _rebase_retry
    return fake


def _make_stable_issues() -> list[dict]:
    """A small stable list of Jira issues with no EXCLUDED_FIELDS."""
    return [
        {
            "key": "DIG-1",
            "fields": {
                "summary": "Implement login",
                "status": {"name": "In Progress"},
                "issuetype": {"name": "Story"},
            },
        },
        {
            "key": "DIG-2",
            "fields": {
                "summary": "Write unit tests",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
            },
        },
    ]


def _patch_acli_and_concurrency(fetcher_mod, applier_mod, issues: list[dict]):
    """Context manager: patch _load_acli in fetcher + applier, and _load_concurrency in applier."""
    import contextlib
    from unittest.mock import patch

    mock_acli = _make_acli_module(issues)
    ok_concurrency = _make_ok_concurrency()

    @contextlib.contextmanager
    def _ctx():
        with (
            patch.object(fetcher_mod, "_load_acli", return_value=mock_acli),
            patch.object(applier_mod, "_load_acli", return_value=mock_acli),
        ):
            original_load_concurrency = applier_mod._load_concurrency
            applier_mod._load_concurrency = lambda: ok_concurrency
            try:
                yield
            finally:
                applier_mod._load_concurrency = original_load_concurrency

    return _ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_idempotency_two_passes_with_unchanged_remote(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """Two consecutive reconcile_once() calls with unchanged remote both have mutation_count=0.

    The first pass initialises prev snapshot from empty ({}) so all issues appear
    as "create" mutations.  The second pass reads the prev snapshot written by the
    first pass and compares it against an identical current snapshot — producing
    zero mutations, proving idempotency.

    This test uses pass_id="idempotency-pass" for both calls so the prev file
    written by pass 1 is the one read by pass 2.
    """
    issues = _make_stable_issues()
    pass_id = "idempotency-pass"

    with _patch_acli_and_concurrency(fetcher_mod, applier_mod, issues):
        result1 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)

    assert result2["mutation_count"] == 0, (
        f"Second pass over unchanged remote must produce mutation_count=0, "
        f"got {result2['mutation_count']}"
    )
    assert result1["pass_id"] == pass_id
    assert result2["pass_id"] == pass_id


def test_live_pass_persists_prev_snapshot_as_a_bare_key_set(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """The cross-pass artifact retains membership but no Jira field bodies."""
    issues = _make_stable_issues()

    with _patch_acli_and_concurrency(fetcher_mod, applier_mod, issues):
        reconcile_mod.reconcile_once("key-set-shape", repo_root=tmp_path)

    prev_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "prev_snapshot.json"
    persisted = json.loads(prev_path.read_text())

    assert persisted == {"DIG-1": {}, "DIG-2": {}}
    assert prev_path.stat().st_size <= 64 * 1024


def test_excluded_fields_change_does_not_drive_mutations(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """A change only in an EXCLUDED_FIELDS field produces mutation_count=0.

    Pass 1: snapshot has issues with 'summary' field (real) and no excluded fields.
    Pass 2: same issues but with an 'local_id' field added (in EXCLUDED_FIELDS).
    The differ must ignore that change and emit zero mutations.
    """
    pass_id = "excluded-fields-pass"
    base_issues = [
        {
            "key": "DIG-10",
            "fields": {
                "summary": "Some issue",
                "status": {"name": "To Do"},
            },
        }
    ]
    # Second call's issues add an excluded field — should not trigger a mutation
    issues_with_excluded = [
        {
            "key": "DIG-10",
            "fields": {
                "summary": "Some issue",
                "status": {"name": "To Do"},
                "local_id": "abc-123",  # EXCLUDED_FIELDS member
            },
        }
    ]

    mock_acli_base = _make_acli_module(base_issues)
    mock_acli_excluded = _make_acli_module(issues_with_excluded)
    ok_concurrency = _make_ok_concurrency()

    from unittest.mock import patch

    # Pass 1: base issues (no excluded fields)
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_base),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_base),
    ):
        applier_mod._load_concurrency_bak = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak

    # Pass 2: same issues but with excluded field added
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_excluded),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_excluded),
    ):
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak

    assert result2["mutation_count"] == 0, (
        f"Changing only an EXCLUDED_FIELDS field must produce mutation_count=0, "
        f"got {result2['mutation_count']}"
    )


def test_real_field_change_converges_after_one_pass(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """A remote field change is detected without planning an outbound REVERT of it.

    WHAT THIS CELL USED TO ASSERT, AND WHY IT WAS WRONG (bugs 727f / d103). It required
    ``mutation_count == 1`` on the detecting pass, calling that "a genuine field change".
    Instrumenting the diff phase in this exact harness showed the one mutation it counted
    was::

        outbound update DIG-20 payload={'summary': 'Original summary'}
                        provenance={'source':'differ','reason':'field_drift',...}

    i.e. the STALE PREVIOUS value pushed BACK to Jira — a revert of the very edit the
    fixture makes, not a sync of it. There is no local side here at all (stderr reports
    ``local_tickets=[]``, ``outbound_differ total=0``, ``inbound_differ total=0``), so the
    "field change" is purely Jira-side, and the count came entirely from the snapshot
    differ reading a remote-vs-remote delta as local-wins drift. The cell was pinning the
    defect's mechanism, not the behaviour its docstring described.

    WHAT IT ASSERTS NOW, PRESERVING BOTH HALVES OF THE ORIGINAL INTENT.

    * "detect a genuine change" -> pass 1 must still plan the inbound create for a key it
      has never seen. That is the snapshot differ's real job on this fixture.
    * "a regression guard against over-filtering" -> that pass-1 assertion is the guard: a
      suppression broad enough to swallow real work fails it.
    * added, and the actual contract: pass 2 must NOT plan an outbound revert of the
      Jira-side edit. Correct handling of a Jira-side field edit is inbound mirroring by
      the binding-aware differ (ADR 0026's arbitrated inbound-mirrored scalar set; ticket
      b9b8 and ``diffing/test_inbound_field_sync_directionality.py``), which needs a
      binding this snapshot-only harness never creates — so zero planned work here is the
      correct outcome, not lost work.
    """
    pass_id = "real-change-pass"
    issues_v1 = [
        {
            "key": "DIG-20",
            "fields": {
                "summary": "Original summary",
                "status": {"name": "To Do"},
            },
        }
    ]
    issues_v2 = [
        {
            "key": "DIG-20",
            "fields": {
                "summary": "CHANGED summary",  # real field changed
                "status": {"name": "To Do"},
            },
        }
    ]

    mock_acli_v1 = _make_acli_module(issues_v1)
    mock_acli_v2 = _make_acli_module(issues_v2)
    ok_concurrency = _make_ok_concurrency()

    from unittest.mock import patch

    # Pass 1: prime prev snapshot with v1
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_v1),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_v1),
    ):
        applier_mod._load_concurrency_bak2 = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result1 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak2

    # The over-filtering guard, preserved: a key the reconciler has never seen is genuine
    # work and must still be planned. A suppression broad enough to swallow real work
    # fails right here.
    assert result1["mutation_count"] >= 1, (
        "pass 1 planned nothing for a Jira key it had never seen — the inbound-create "
        "path has been over-filtered away"
    )

    # Pass 2: present v2 — the Jira-side edit must not be reverted outbound
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_v2),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_v2),
    ):
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = applier_mod._load_concurrency_bak2

    assert result2["mutation_count"] == 0, (
        "the pass planned work for a Jira-side field edit on a key with no local "
        "counterpart. The only thing the snapshot differ can emit here is an outbound "
        "update carrying the STALE previous value — a revert of the edit, not a sync of "
        f"it. Got mutation_count={result2['mutation_count']}"
    )


# ---------------------------------------------------------------------------
# Ticket yaw-plait-doe: cap-0 (dry-run / reconcile-check) modes must run the
# full differ COMPUTATION but write NOTHING to the local store.
# ---------------------------------------------------------------------------

MODE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mode.py"


@pytest.fixture(scope="module")
def mode_mod():
    """Load mode.py under the dotted key reconcile_once uses."""
    return _load_module("rebar_reconciler.mode", MODE_PATH)


def _make_jql_partitioned_acli(issues: list[dict]) -> object:
    """Faithful acli stub that partitions by the active/Done JQL split.

    The production fetcher issues TWO queries (active ``status != Done`` then
    Done-recent). A real Jira partitions the set by status, so no key appears
    in both. The simple ``_make_acli_module`` returns the same list for every
    query, which spuriously triggers the cross-query dedup observability alert.
    This stub returns an issue only for the query whose status-band matches,
    mirroring production so the snapshot build emits no dedup alert.
    """
    base = _make_acli_module(issues)
    Base = type(base)  # S4: _make_acli_module now returns a transport instance

    class _Partitioned(Base):
        def search_issues(self, jql: str, **kwargs) -> list[dict]:
            # Dedup JQL (labels = "rebar-id:...") keeps the base behaviour.
            if jql.strip().startswith('labels = "rebar-id:'):
                return super().search_issues(jql, **kwargs)
            # Active query carries ``status != "Done"``; Done-recent carries
            # ``status = "Done"``. Distinguish on the ``!=`` operator so the
            # active substring (which also contains ``= "Done"``) isn't
            # mis-classified as the Done query. Partition over the LIVE
            # state (base returns reflected writes), not the seed list.
            done = '= "Done"' in jql and '!= "Done"' not in jql
            out = []
            for issue in super().search_issues(jql, **kwargs):
                name = (issue.get("fields", {}).get("status") or {}).get("name", "")
                is_done = name == "Done"
                if done == is_done:
                    out.append(issue)
            return out

    return _Partitioned()


def _patch_partitioned(fetcher_mod, applier_mod, issues: list[dict]):
    """Like _patch_acli_and_concurrency but with the JQL-partitioned stub."""
    import contextlib
    from unittest.mock import patch

    mock_acli = _make_jql_partitioned_acli(issues)
    ok_concurrency = _make_ok_concurrency()

    @contextlib.contextmanager
    def _ctx():
        with (
            patch.object(fetcher_mod, "_load_acli", return_value=mock_acli),
            patch.object(applier_mod, "_load_acli", return_value=mock_acli),
        ):
            original = applier_mod._load_concurrency
            applier_mod._load_concurrency = lambda: ok_concurrency
            try:
                yield
            finally:
                applier_mod._load_concurrency = original

    return _ctx()


def _snapshot_tree(root: Path) -> set[str]:
    """Return the set of relative file paths currently under *root*."""
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_reconcile_once_writes_nothing(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod, mode_mod
):
    """A DRY_RUN reconcile_once produces ZERO local writes.

    The full differ runs (mutation_count reflects the computed plan), but NO
    file may appear under bridge_state/ or .tickets-tracker/.bridge_state/ —
    no snapshot, manifest, health record, sync-log, bindings, or prev_snapshot.
    Encodes the fix for ticket yaw-plait-doe (cap-0 modes were persisting).
    """
    issues = _make_stable_issues()
    pass_id = "dry-run-no-write"

    before = _snapshot_tree(tmp_path)

    with _patch_partitioned(fetcher_mod, applier_mod, issues):
        result = reconcile_mod.reconcile_once(
            pass_id, repo_root=tmp_path, target_mode=mode_mod.Mode.DRY_RUN
        )

    after = _snapshot_tree(tmp_path)
    new_files = sorted(after - before)

    assert new_files == [], f"DRY_RUN reconcile_once must write NOTHING, but created: {new_files}"

    # The computed plan must still be produced — two stable issues with an
    # empty prev snapshot yield non-zero computed mutations.
    assert result["mutation_count"] > 0, (
        "DRY_RUN must still COMPUTE the mutation plan; "
        f"got mutation_count={result['mutation_count']}"
    )
    # Nothing applied; plan surfaced.
    assert result["mutations_applied"] == 0
    assert result.get("no_write") is True
    assert result.get("mode") == "dry-run"
    assert isinstance(result.get("plan"), list)
    assert len(result["plan"]) == result["mutation_count"]
    # Plan entries carry useful per-mutation detail.
    for entry in result["plan"]:
        assert set(entry) >= {"direction", "action", "target", "local_id"}


def test_reconcile_check_reconcile_once_writes_nothing(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod, mode_mod
):
    """RECONCILE_CHECK is also a cap-0 mode → reconcile_once must not write."""
    issues = _make_stable_issues()
    pass_id = "reconcile-check-no-write"

    before = _snapshot_tree(tmp_path)

    with _patch_partitioned(fetcher_mod, applier_mod, issues):
        result = reconcile_mod.reconcile_once(
            pass_id, repo_root=tmp_path, target_mode=mode_mod.Mode.RECONCILE_CHECK
        )

    after = _snapshot_tree(tmp_path)
    new_files = sorted(after - before)

    assert new_files == [], (
        f"RECONCILE_CHECK reconcile_once must write NOTHING, but created: {new_files}"
    )
    assert result["mutation_count"] > 0
    assert result["mutations_applied"] == 0
    assert result.get("no_write") is True


def test_live_mode_reconcile_once_still_persists(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod, mode_mod
):
    """Writing modes (LIVE) keep persisting: a snapshot file IS written."""
    issues = _make_stable_issues()
    pass_id = "live-persists"

    with _patch_partitioned(fetcher_mod, applier_mod, issues):
        result = reconcile_mod.reconcile_once(
            pass_id, repo_root=tmp_path, target_mode=mode_mod.Mode.LIVE
        )

    after = _snapshot_tree(tmp_path)
    # The snapshot file is the canonical persistence artifact.
    assert f"bridge_state/snapshots/{pass_id}.json" in after, (
        f"LIVE must persist the snapshot; files: {sorted(after)}"
    )
    assert result.get("no_write") is not True


# ---------------------------------------------------------------------------
# Ticket d103: a bound key that leaves the fetch window must never be planned
# as an outbound CREATE — that resurrects it from stale snapshot fields.
# ---------------------------------------------------------------------------


def test_a_key_absent_from_the_current_snapshot_never_reaches_create_issue(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """The live-path oracle for d103: absence must not drive a create.

    Pass 1 primes prev_snapshot with DIG-9; pass 2 presents an EMPTY working set, so
    DIG-9 is present in prev and absent from curr. That happens for a deleted issue AND
    for one that merely ages out of the working-set query (``status = Done`` beyond the
    recent cap), so the reconciler cannot tell the two apart from absence alone.

    ADR 0028 Decision para 1: "No destructive or terminal action ... may be driven by a
    key's absence from the fetched snapshot"; deletion is proven only by a bounded direct
    GET returning 404 (para 2). A create targeted at the issue's own Jira key, built from
    the stale prev fields, is exactly such a terminal action.

    Neither downstream guard catches it, which is why the assertion is at the transport:
    ``applier._cross_project_targets`` skips ``action == "create"`` outright, and
    ``create_one``'s JQL dedup degenerates to ``labels = "rebar-id:"`` because
    EXCLUDED_FIELDS strips ``local_id`` from the payload.
    """
    from unittest.mock import patch

    issues_v1 = [
        {
            "key": "DIG-9",
            "fields": {
                "summary": "a bound issue that later leaves the window",
                "status": {"name": "To Do"},
                "labels": ["rebar-id:jira-dig-9"],
            },
        }
    ]
    acli_v1 = _make_acli_module(issues_v1)
    acli_v2 = _make_acli_module([])  # DIG-9 gone from the working set
    ok_concurrency = _make_ok_concurrency()

    created: list = []
    _orig_create = acli_v2.create_issue

    def _recording_create(fields):
        created.append(dict(fields))
        return _orig_create(fields)

    acli_v2.create_issue = _recording_create  # type: ignore[method-assign]

    pass_id = "d103-absent-key"
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=acli_v1),
        patch.object(applier_mod, "_load_acli", return_value=acli_v1),
    ):
        _bak = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = _bak

    with (
        patch.object(fetcher_mod, "_load_acli", return_value=acli_v2),
        patch.object(applier_mod, "_load_acli", return_value=acli_v2),
    ):
        _bak = applier_mod._load_concurrency
        applier_mod._load_concurrency = lambda: ok_concurrency
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod._load_concurrency = _bak

    assert created == [], (
        "a key that merely left the fetch window was RESURRECTED: reconcile_once called "
        f"client.create_issue with fields rebuilt from the stale prev snapshot: {created}"
    )
    assert result2["mutation_count"] == 0, (
        "the pass planned work for a key that is absent from the current snapshot; "
        f"absence alone licenses no action at all. Got {result2['mutation_count']}"
    )
