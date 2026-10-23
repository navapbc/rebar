"""Check pass convergence, excluded fields, remote changes, dry-run writes, and
absent-key suppression. Mutable Jira and JQL stubs preserve the production search
partitions and write-back behavior.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "reconcile.py"
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "fetcher.py"
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


@pytest.fixture(scope="module")
def reconcile_mod():
    """Load reconcile.py, failing all tests if absent."""
    if not RECONCILE_PATH.exists():
        pytest.fail(
            f"reconcile.py not found at {RECONCILE_PATH} — implement the module to make tests pass."
        )
    return importlib.import_module("rebar.reconciler.reconcile")


@pytest.fixture(scope="module")
def fetcher_mod():
    """Load fetcher.py."""
    return importlib.import_module("rebar.reconciler.fetcher")


@pytest.fixture(scope="module")
def applier_mod():
    """Load applier.py."""
    return importlib.import_module("rebar.reconciler.applier")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_acli_module(issues: list[dict]) -> object:
    """Serve mutable Jira state through the production client shape: writes affect
    later searches, so two-pass convergence cannot pass against a static fixture.
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


def _ok_snapshot_head(repo_root: Path) -> str:
    """Stand-in for ``applier.snapshot_head`` with a stable snapshot head."""
    return "aabbccdd" * 5


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


def _patch_acli_and_snapshot_head(fetcher_mod, applier_mod, issues: list[dict]):
    """Context manager: patch _load_acli in fetcher + applier, and snapshot_head in applier."""
    import contextlib
    from unittest.mock import patch

    mock_acli = _make_acli_module(issues)

    @contextlib.contextmanager
    def _ctx():
        with (
            patch.object(fetcher_mod, "_load_acli", return_value=mock_acli),
            patch.object(applier_mod, "_load_acli", return_value=mock_acli),
        ):
            original_snapshot_head = applier_mod.snapshot_head
            applier_mod.snapshot_head = _ok_snapshot_head
            try:
                yield
            finally:
                applier_mod.snapshot_head = original_snapshot_head

    return _ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_idempotency_two_passes_with_unchanged_remote(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """Two passes over unchanged Jira state converge: the second produces no mutations
    or writes.
    """
    issues = _make_stable_issues()
    pass_id = "idempotency-pass"

    with _patch_acli_and_snapshot_head(fetcher_mod, applier_mod, issues):
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

    with _patch_acli_and_snapshot_head(fetcher_mod, applier_mod, issues):
        reconcile_mod.reconcile_once("key-set-shape", repo_root=tmp_path)

    prev_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "prev_snapshot.json"
    persisted = json.loads(prev_path.read_text())

    assert persisted == {"DIG-1": {}, "DIG-2": {}}
    assert prev_path.stat().st_size <= 64 * 1024


def test_excluded_fields_change_does_not_drive_mutations(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """Changing only an excluded field does not generate a mutation."""
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

    from unittest.mock import patch

    # Pass 1: base issues (no excluded fields)
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_base),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_base),
    ):
        saved_snapshot_head = applier_mod.snapshot_head
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = saved_snapshot_head

    # Pass 2: same issues but with excluded field added
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_excluded),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_excluded),
    ):
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = saved_snapshot_head

    assert result2["mutation_count"] == 0, (
        f"Changing only an EXCLUDED_FIELDS field must produce mutation_count=0, "
        f"got {result2['mutation_count']}"
    )


def test_real_field_change_converges_after_one_pass(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """An unseen Jira key plans work on pass 1; a Jira-only field edit on pass 2
    plans no outbound revert of its current value.
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

    from unittest.mock import patch

    # Pass 1: prime prev snapshot with v1
    with (
        patch.object(fetcher_mod, "_load_acli", return_value=mock_acli_v1),
        patch.object(applier_mod, "_load_acli", return_value=mock_acli_v1),
    ):
        saved_snapshot_head2 = applier_mod.snapshot_head
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            result1 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = saved_snapshot_head2

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
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = saved_snapshot_head2

    assert result2["mutation_count"] == 0, (
        "the pass planned work for a Jira-side field edit on a key with no local "
        "counterpart. The only thing the snapshot differ can emit here is an outbound "
        "update carrying the STALE previous value — a revert of the edit, not a sync of "
        f"it. Got mutation_count={result2['mutation_count']}"
    )


# ---------------------------------------------------------------------------
# Ticket yaw-plait-doe: cap-0 dry-run mode must run the
# full differ COMPUTATION but write NOTHING to the local store.
# ---------------------------------------------------------------------------

MODE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "mode.py"


@pytest.fixture(scope="module")
def mode_mod():
    """Load mode.py under the dotted key reconcile_once uses."""
    return importlib.import_module("rebar.reconciler.mode")


def _make_jql_partitioned_acli(issues: list[dict]) -> object:
    """Partition active and Done-recent searches as Jira does, keeping each key in its
    proper query window.
    """
    base = _make_acli_module(issues)
    Base = type(base)  # S4: _make_acli_module now returns a transport instance

    class _Partitioned(Base):
        def search_issues(self, jql: str, **kwargs) -> list[dict]:
            # Dedup JQL (labels = "rebar-id:...") keeps the base behaviour.
            if jql.strip().startswith('labels = "rebar-id:'):
                return super().search_issues(jql, **kwargs)
            # Distinguish != Done from = Done, which is a substring of the
            # active query; partition current reflected state, not seed data.
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
    """Like _patch_acli_and_snapshot_head but with the JQL-partitioned stub."""
    import contextlib
    from unittest.mock import patch

    mock_acli = _make_jql_partitioned_acli(issues)

    @contextlib.contextmanager
    def _ctx():
        with (
            patch.object(fetcher_mod, "_load_acli", return_value=mock_acli),
            patch.object(applier_mod, "_load_acli", return_value=mock_acli),
        ):
            original = applier_mod.snapshot_head
            applier_mod.snapshot_head = _ok_snapshot_head
            try:
                yield
            finally:
                applier_mod.snapshot_head = original

    return _ctx()


def _snapshot_tree(root: Path) -> set[str]:
    """Return the set of relative file paths currently under *root*."""
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_dry_run_reconcile_once_writes_nothing(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod, mode_mod
):
    """Dry-run computes the mutation plan without writing snapshots, bindings, or Jira
    state.
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


def test_live_mode_reconcile_once_still_persists(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod, mode_mod
):
    """Writing modes (LIVE) keep persisting: a snapshot file IS written, under the
    repo-local scratch ``.rebar/bridge/`` and never the pre-move repo-root location."""
    issues = _make_stable_issues()
    pass_id = "live-persists"

    with _patch_partitioned(fetcher_mod, applier_mod, issues):
        result = reconcile_mod.reconcile_once(
            pass_id, repo_root=tmp_path, target_mode=mode_mod.Mode.LIVE
        )

    after = _snapshot_tree(tmp_path)
    # The snapshot file is the canonical persistence artifact.
    assert f".rebar/bridge/snapshots/{pass_id}.json" in after, (
        f"LIVE must persist the snapshot; files: {sorted(after)}"
    )
    assert not (tmp_path / "bridge_state").exists(), (
        f"LIVE wrote to the pre-move repo-root scratch; files: {sorted(after)}"
    )
    assert result.get("no_write") is not True


# ---------------------------------------------------------------------------
# Ticket d103: a bound key that leaves the fetch window must never be planned
# as an outbound CREATE — that resurrects it from stale snapshot fields.
# ---------------------------------------------------------------------------


def test_a_key_absent_from_the_current_snapshot_never_reaches_create_issue(
    tmp_path, reconcile_mod, fetcher_mod, applier_mod
):
    """A key present only in the previous snapshot cannot trigger Jira creation.
    Partitioned JQL and window searches make the absence path observable.
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
        _bak = applier_mod.snapshot_head
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = _bak

    # The bound off-window key gets one direct probe. Return the confirmed-404
    # sentinel through the loaded probe seam; one 404 remains below retire grace
    # and must not create a mutation or reach live transport.
    ob_mod = importlib.import_module("rebar.reconciler.outbound_differ")

    def _probe_404(_client, _key):
        return ob_mod._DELETED

    with (
        patch.object(fetcher_mod, "_load_acli", return_value=acli_v2),
        patch.object(applier_mod, "_load_acli", return_value=acli_v2),
        patch.object(ob_mod, "_safe_get_issue", _probe_404),
    ):
        _bak = applier_mod.snapshot_head
        applier_mod.snapshot_head = _ok_snapshot_head
        try:
            result2 = reconcile_mod.reconcile_once(pass_id, repo_root=tmp_path)
        finally:
            applier_mod.snapshot_head = _bak

    assert created == [], (
        "a key that merely left the fetch window was RESURRECTED: reconcile_once called "
        f"client.create_issue with fields rebuilt from the stale prev snapshot: {created}"
    )
    assert result2["mutation_count"] == 0, (
        "the pass planned work for a key that is absent from the current snapshot; "
        f"absence alone licenses no action at all. Got {result2['mutation_count']}"
    )
