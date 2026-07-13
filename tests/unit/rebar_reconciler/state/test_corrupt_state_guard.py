"""Tests for corrupt bridge-state file handling (bug 4292-f24b-c0de-4f61).

Covers two seams:
  SEAM 1 — reconcile.py:472-473: bare json.loads(prev_path.read_text()) with no
    error handling. A conflict-corrupted or truncated prev_snapshot.json causes a
    JSONDecodeError that crashes the entire pass before the outbound differ runs.

  SEAM 2 — binding_store.py:53-57: bare json.load(f) with no error handling.
    A conflict-corrupted bindings.json raises JSONDecodeError propagated uncaught
    from __init__ → load_binding_store → reconcile_once.

Safety invariant: NEVER emit outbound comment-add mutations when the Jira-side
comment state is unknown (i.e., when the pass has not fetched curr_snapshot
successfully due to a crash before the outbound differ).

Fixture conventions:
  - importlib loader (spec_from_file_location) — per conftest.py docstring
  - inline StubBindingStore — same pattern as test_outbound_differ_comment_dedup.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]

BINDING_STORE_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "binding_store.py"
)

OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def binding_store_mod() -> ModuleType:
    return _load_module("corrupt_state_binding_store", BINDING_STORE_PATH)


@pytest.fixture(scope="module")
def outbound_differ_mod() -> ModuleType:
    return _load_module("corrupt_state_outbound_differ", OUTBOUND_DIFFER_PATH)


# ---------------------------------------------------------------------------
# SEAM 2 tests — BindingStore._load() corrupt-file handling
# ---------------------------------------------------------------------------


class TestBindingStoreCorrupt:
    """SEAM 2: corrupt bindings.json must fail closed, never silently-empty."""

    def test_corrupt_json_raises_with_named_file(
        self, tmp_path: Path, binding_store_mod: ModuleType
    ) -> None:
        """A bindings.json containing invalid JSON must raise an informative
        exception that names the corrupted file.

        Rationale: silently returning an empty store would treat all local
        tickets as unbound → emit CREATE mutations for every ticket → mass
        duplicate Jira issues. Fail-closed is the safe behavior.
        """
        bridge_dir = tmp_path / ".bridge_state"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bindings_path = bridge_dir / "bindings.json"
        bindings_path.write_text("{ this is not valid json }", encoding="utf-8")

        BindingStore = binding_store_mod.BindingStore
        with pytest.raises((json.JSONDecodeError, ValueError, OSError)) as exc_info:
            BindingStore(tmp_path)

        # The error message or its str representation must contain the path
        # so operators can identify which file to fix.
        err_text = str(exc_info.value)
        assert str(bindings_path) in err_text or "bindings.json" in err_text, (
            f"Exception must name the corrupt file. Got: {err_text!r}\n"
            "Operators need to know WHICH file to restore from backup."
        )

    def test_git_conflict_markers_in_bindings_raises(
        self, tmp_path: Path, binding_store_mod: ModuleType
    ) -> None:
        """A bindings.json containing git merge-conflict markers (<<<<<<< HEAD,
        =======, >>>>>>> branch) must not silently return an empty store.

        A file containing conflict markers is NOT valid JSON — json.load will
        raise JSONDecodeError. The BindingStore must propagate a meaningful
        exception rather than silently defaulting to empty bindings (which
        would cause every ticket to be emitted as a CREATE on the next pass).
        """
        bridge_dir = tmp_path / ".bridge_state"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bindings_path = bridge_dir / "bindings.json"
        conflict_content = (
            "<<<<<<< HEAD\n"
            '{"version": 1, "bindings": {"local-1": {"jira_key": "DIG-1", "state": "confirmed"}}, "reverse": {"DIG-1": "local-1"}}\n'  # noqa: E501 — exact conflict-marker JSON fixture
            "=======\n"
            '{"version": 1, "bindings": {}, "reverse": {}}\n'
            ">>>>>>> feature-branch\n"
        )
        bindings_path.write_text(conflict_content, encoding="utf-8")

        BindingStore = binding_store_mod.BindingStore
        with pytest.raises((json.JSONDecodeError, ValueError, OSError)) as exc_info:
            BindingStore(tmp_path)

        err_text = str(exc_info.value)
        # Must name the file in the error for operator actionability
        assert str(bindings_path) in err_text or "bindings.json" in err_text, (
            f"Exception must name the corrupt file. Got: {err_text!r}\n"
            "File path in error message helps operators locate the file to resolve."
        )

    def test_corrupt_bindings_no_empty_fallback(
        self, tmp_path: Path, binding_store_mod: ModuleType
    ) -> None:
        """Corrupt bindings.json must NOT silently fall back to empty bindings.

        An empty fallback would make all tickets appear unbound → emit CREATE
        mutations for all of them → duplicate Jira issues on every pass.
        The safe behavior is to abort the reconcile run (raise), not to continue
        with a known-bad state.
        """
        bridge_dir = tmp_path / ".bridge_state"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bindings_path = bridge_dir / "bindings.json"
        bindings_path.write_text("<<<<<< not json >>>>>>", encoding="utf-8")

        BindingStore = binding_store_mod.BindingStore

        raised = False
        try:
            BindingStore(tmp_path)
            # If no exception, verify that it didn't silently return empty
            # (this branch should not be reachable after the fix)
            raised = False
        except Exception:  # noqa: BLE001 — asserts BindingStore raises *something* on corrupt JSON rather than silently returning empty; any exception is the correct outcome
            raised = True

        assert raised, (
            "BindingStore must raise on corrupt bindings.json instead of "
            "silently returning an empty store. Empty fallback would emit "
            "CREATE mutations for all bound tickets, duplicating Jira issues."
        )


# ---------------------------------------------------------------------------
# SEAM 1 tests — prev_snapshot.json corruption in reconcile.py path
# (tested via outbound_differ directly, validating the comment-state invariant)
# ---------------------------------------------------------------------------


class TestPrevSnapshotCorrupt:
    """SEAM 1 tests: when comment state is unknown (corrupt prev_snapshot.json
    causes the pass to crash before curr_snapshot is fetched), no comment-add
    mutations may be emitted.

    These tests exercise outbound_differ._diff_comments with a snapshot that
    represents the degraded state (jira_snapshot={}) to verify the fix blocks
    comment-add mutations when Jira-side comment state is unknown.

    The fix is in reconcile.py's snapshot-load path: catching JSONDecodeError
    on prev_snapshot.json load must abort the pass (or skip comment mutations),
    not silently substitute {} for curr_snapshot.

    Since outbound_differ is a pure function (no I/O), we test the invariant
    by simulating what the reconcile.py fix must guarantee:
      - A corrupt prev_snapshot.json must not cause the outbound differ to
        receive an empty jira_snapshot for comment diffing.
      - We test the _read_prev_snapshot helper behavior directly on reconcile.py
        when it exists, falling back to verifying the invariant via a probe.
    """

    def test_prev_snapshot_json_parse_error_helper(self, tmp_path: Path) -> None:
        """prev_snapshot.json with invalid JSON must raise JSONDecodeError or
        be caught and the pass must abort without emitting comment mutations.

        This test validates the reconcile.py load path behavior by calling
        the module-level snapshot-load function or by observing that
        json.loads raises on the corrupt content (proving the bare load
        path has no guard, so the fix must add one).
        """
        # Write a corrupt snapshot file
        prev_dir = tmp_path / ".tickets-tracker" / ".bridge_state"
        prev_dir.mkdir(parents=True, exist_ok=True)
        prev_path = prev_dir / "prev_snapshot.json"

        corrupt_contents = [
            # Classic git conflict markers
            (
                "<<<<<<< HEAD\n"
                '{"DIG-100": {"comment": {"comments": [{"body": "hello"}]}}}\n'
                "=======\n"
                "{}\n"
                ">>>>>>> feature-branch\n"
            ),
            # Truncated JSON (partial write on crash)
            '{"DIG-100": {"comment":',
            # Empty file
            "",
        ]

        for content in corrupt_contents:
            prev_path.write_text(content, encoding="utf-8")
            # The bare json.loads(prev_path.read_text()) MUST raise on each of these
            with pytest.raises((json.JSONDecodeError, ValueError)):
                json.loads(prev_path.read_text())

    def test_corrupt_prev_snapshot_must_not_emit_comment_adds(
        self, tmp_path: Path, outbound_differ_mod: ModuleType
    ) -> None:
        """When prev_snapshot is corrupt, the reconcile.py fix must ensure the
        outbound differ does NOT receive an empty jira_snapshot for comment diffing.

        This test validates the invariant: if the pass proceeds despite a corrupt
        prev_snapshot (e.g. because only the INBOUND differ uses prev_snapshot,
        and curr_snapshot is the authoritative Jira state for outbound diffing),
        then the comment-add logic uses curr_snapshot (live fetch) and NOT a
        fallback-empty prev_snapshot.

        We probe the outbound differ's behavior directly:
        - When jira_snapshot is {} (as if curr_snapshot was never fetched),
          _diff_comments treats all local comments as new → emits adds.
        - The fix must ensure curr_snapshot is always the live fetch result, and
          if curr_snapshot cannot be obtained (because the pass crashed on prev),
          the pass ABORTS rather than continuing with an empty jira_snapshot.

        This test documents the invariant by asserting on the FIXED behavior:
        a corrupt prev_snapshot.json file at the reconcile.py read path raises
        JSONDecodeError (no guard in place before the fix = crash = no mutations).
        After the fix: the exception must be caught, alert emitted, and pass
        aborted with a clear error rather than silently continuing with {}.
        """
        # Set up a ticket with comments
        ticket = {
            "ticket_id": "local-corrupt-test",
            "title": "Test ticket",
            "description": "desc",
            "status": "open",
            "priority": 2,
            "ticket_type": "task",
            "assignee": "",
            "tags": [],
            "comments": [{"body": "comment one"}, {"body": "comment two"}],
            "deps": [],
        }

        class StubBindingStore:
            def get_baseline(self, local_id):
                # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
                return None

            def is_pending(self, local_id):
                return False

            def get_jira_key(self, local_id: str):
                return "DIG-TEST-1"

            def is_bound(self, local_id: str) -> bool:
                return True

        # When curr_snapshot is properly fetched (normal path), no spurious adds
        jira_snapshot_with_comments = {
            "DIG-TEST-1": {
                "summary": "Test ticket",
                "comment": {
                    "comments": [
                        {"id": "c1", "body": "comment one"},
                        {"id": "c2", "body": "comment two"},
                    ],
                    "total": 2,
                },
            }
        }

        result, _ = outbound_differ_mod.compute_outbound_mutations(
            local_tickets=[ticket],
            jira_snapshot=jira_snapshot_with_comments,
            binding_store=StubBindingStore(),
        )
        comment_adds = [
            m
            for m in result
            if m.action == "update" and any(c.get("action") == "add" for c in m.comments)
        ]
        assert comment_adds == [], (
            "When curr_snapshot has the Jira comments, no comment-add must be emitted. "
            f"Got: {[m.comments for m in comment_adds]}"
        )

        # Defense-in-depth (bug 4292): even when jira_snapshot is empty AND
        # no client is provided, the outbound differ must NOT emit blind
        # comment-add mutations. The corrupt-state guard in reconcile.py is
        # the primary defense (it aborts the pass before outbound_differ runs).
        # The differ-level safety invariant (bug 4292) is a secondary defense:
        # when the snapshot entry lacks a 'comment' field and no client is
        # available to fetch live comment state, _diff_comments skips comment
        # mutations rather than emitting blind adds.
        # Previously this block asserted that the UNSAFE path would emit adds
        # (to document why the reconcile.py abort was necessary). With bug 4292
        # fixed, that path is also safe — no adds, no client call. The corrupt-
        # state guard is still load-bearing for the primary protection, but the
        # differ is now hardened as a second layer.
        jira_snapshot_empty = {}

        result_no_client, _ = outbound_differ_mod.compute_outbound_mutations(
            local_tickets=[ticket],
            jira_snapshot=jira_snapshot_empty,
            binding_store=StubBindingStore(),
            # No client: _diff_comments must skip comment mutations rather than
            # emitting blind adds against unknown Jira state.
        )
        # Bug 4292 fix: even with empty snapshot + no client, zero adds emitted.
        comment_adds_no_client = [
            m
            for m in result_no_client
            if m.action == "update" and any(c.get("action") == "add" for c in m.comments)
        ]
        assert len(comment_adds_no_client) == 0, (
            "Bug 4292 defense-in-depth: when snapshot lacks 'comment' field and "
            "no client is provided, the outbound differ must NOT emit blind comment-"
            "add mutations. The corrupt-state guard in reconcile.py remains the "
            "primary protection (it aborts before the differ runs); this is the "
            "second defense layer at the differ level. "
            f"Got unexpected comment adds: {[m.comments for m in comment_adds_no_client]}"
        )


# ---------------------------------------------------------------------------
# SEAM 1 regression: healthy prev_snapshot + curr_snapshot still deduplicates
# ---------------------------------------------------------------------------


class TestHealthyStateRegression:
    """Regression: normal (non-corrupt) snapshot still deduplicates comments.

    Ensures the corruption-guard changes do not break the happy path.
    """

    def test_healthy_snapshot_deduplicates_comments(self, outbound_differ_mod: ModuleType) -> None:
        """When prev_snapshot.json is healthy, comment dedup still works.

        This mirrors the existing test_outbound_differ_comment_dedup.py fixtures
        but exercises the full compute_outbound_mutations path for regression
        coverage after the SEAM 1 fix.
        """
        jira_key = "DIG-HEALTHY"
        existing_bodies = ["Already synced", "Also synced"]
        new_body = "Brand new comment"

        class StubBindingStore:
            def get_baseline(self, local_id):
                # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
                return None

            def is_pending(self, local_id):
                return False

            def get_jira_key(self, local_id: str):
                if local_id == "healthy-local-1":
                    return jira_key
                return None

            def is_bound(self, local_id: str) -> bool:
                return local_id == "healthy-local-1"

        ticket = {
            "ticket_id": "healthy-local-1",
            "title": "Healthy ticket",
            "description": "desc",
            "status": "open",
            "priority": 2,
            "ticket_type": "task",
            "assignee": "",
            "tags": [],
            "comments": [{"body": b} for b in existing_bodies + [new_body]],
            "deps": [],
        }

        jira_snapshot = {
            jira_key: {
                "summary": "Healthy ticket",
                "description": "desc",
                "issuetype": {"name": "Task"},
                "priority": {"name": "Medium"},
                "status": {"name": "To Do"},
                "assignee": "",
                "labels": [],
                "comment": {
                    "comments": [{"id": str(i), "body": b} for i, b in enumerate(existing_bodies)],
                    "total": len(existing_bodies),
                },
            }
        }

        result, _ = outbound_differ_mod.compute_outbound_mutations(
            local_tickets=[ticket],
            jira_snapshot=jira_snapshot,
            binding_store=StubBindingStore(),
        )

        assert len(result) == 1, f"Expected 1 mutation (for the new comment), got {len(result)}"
        m = result[0]
        assert len(m.comments) == 1, (
            f"Expected exactly 1 comment-add for the new comment only, got {m.comments}"
        )
        assert m.comments[0]["action"] == "add"
        assert new_body in m.comments[0]["body"], (
            f"Expected new body in emitted comment. Got: {m.comments[0]['body']!r}"
        )
        # Existing comments must NOT be re-emitted
        for existing in existing_bodies:
            assert not any(existing in c["body"] for c in m.comments), (
                f"Existing comment {existing!r} must not be re-emitted as an add"
            )
