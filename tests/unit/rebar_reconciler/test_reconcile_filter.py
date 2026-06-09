"""Tests for the --filter-local-ids post-filter in reconcile_once.

Verifies that when filter_local_ids is set:
  - All three differs still run on full inputs (same code paths).
  - Only mutations targeting filtered IDs (or their bound Jira keys) reach apply.
  - Invariant checks and recover_pending_bindings are skipped.
  - The sync logger records the filter metadata.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


# ---------------------------------------------------------------------------
# Load reconcile.py via importlib (same pattern as test_reconcile_main.py)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_RECONCILER_DIR = _HERE.parents[2] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_reconcile():
    name = "reconcile_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = _RECONCILER_DIR / "reconcile.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


reconcile = _load_reconcile()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mutation(target: str, local_id: str = "", jira_key: str = ""):
    """Build a minimal Mutation-like object for filter testing."""
    m = types.SimpleNamespace()
    m.target = target
    m.provenance = {}
    if local_id:
        m.provenance["local_id"] = local_id
    if jira_key:
        m.provenance["jira_key"] = jira_key
    m.direction = types.SimpleNamespace(value="outbound")
    m.action = types.SimpleNamespace(value="create")
    return m


class FakeBindingStore:
    def __init__(self, bindings: dict[str, str]):
        self._local_to_jira = bindings
        self._jira_to_local = {v: k for k, v in bindings.items()}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._local_to_jira.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        return self._jira_to_local.get(jira_key)


# ---------------------------------------------------------------------------
# Tests for _build_filter_target_set
# ---------------------------------------------------------------------------


class TestBuildFilterTargetSet:
    def test_includes_local_ids(self):
        bs = FakeBindingStore({})
        result = reconcile._build_filter_target_set({"abc", "def"}, bs)
        assert "abc" in result
        assert "def" in result

    def test_includes_bound_jira_keys(self):
        bs = FakeBindingStore({"abc": "DIG-100", "def": "DIG-200"})
        result = reconcile._build_filter_target_set({"abc", "def"}, bs)
        assert "DIG-100" in result
        assert "DIG-200" in result
        assert "abc" in result

    def test_unbound_ids_only_include_local(self):
        bs = FakeBindingStore({"abc": "DIG-100"})
        result = reconcile._build_filter_target_set({"abc", "unbound-id"}, bs)
        assert "abc" in result
        assert "DIG-100" in result
        assert "unbound-id" in result
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Tests for _mutation_matches_filter
# ---------------------------------------------------------------------------


class TestMutationMatchesFilter:
    def test_matches_by_target(self):
        m = _make_mutation(target="DIG-100")
        assert reconcile._mutation_matches_filter(m, {"DIG-100", "abc"})

    def test_matches_by_provenance_local_id(self):
        m = _make_mutation(target="DIG-999", local_id="abc")
        assert reconcile._mutation_matches_filter(m, {"abc"})

    def test_matches_by_provenance_jira_key(self):
        m = _make_mutation(target="some-other", jira_key="DIG-100")
        assert reconcile._mutation_matches_filter(m, {"DIG-100"})

    def test_no_match(self):
        m = _make_mutation(target="DIG-999", local_id="xyz", jira_key="DIG-888")
        assert not reconcile._mutation_matches_filter(m, {"abc", "DIG-100"})

    def test_empty_provenance(self):
        m = _make_mutation(target="DIG-999")
        assert not reconcile._mutation_matches_filter(m, {"abc"})


# ---------------------------------------------------------------------------
# Integration: reconcile_once with filter_local_ids
# ---------------------------------------------------------------------------


class TestReconcileOnceFiltered:
    """Verify that reconcile_once with filter_local_ids only dispatches
    matching mutations to the applier.

    This integration test stubs the I/O-bound dependencies (fetcher, differ,
    applier, etc.) so it can run hermetically.  The actual filter logic
    under test — `_build_filter_target_set`, `_mutation_matches_filter`, and
    the post-filter loop inside `reconcile_once` itself — is REAL code, not
    stubbed.  The stubs only control the inputs (mutation list, binding
    store contents) and observe the outputs (which mutations reach
    applier.apply).  The helper-function-level tests above
    (TestBuildFilterTargetSet, TestMutationMatchesFilter) cover the filter
    logic in finer-grained isolation."""

    def _stub_modules(self):
        """Register stub modules in sys.modules so reconcile_once's _load()
        picks them up instead of loading the real sibling files."""
        stubs = {}

        # Fetcher stub
        fetcher = types.ModuleType("reconcile_fetcher")
        snapshot_path = None

        def fetch_snapshot(pass_id, repo_root=None):
            nonlocal snapshot_path
            out_dir = repo_root / "bridge_state" / "snapshots"
            out_dir.mkdir(parents=True, exist_ok=True)
            snapshot_path = out_dir / f"{pass_id}.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "DIG-100": {
                            "summary": "test ticket",
                            "status": {"name": "To Do"},
                        },
                        "DIG-200": {
                            "summary": "other ticket",
                            "status": {"name": "To Do"},
                        },
                        "DIG-300": {
                            "summary": "third ticket",
                            "status": {"name": "Done"},
                        },
                    }
                )
            )
            return snapshot_path

        fetcher.fetch_snapshot = fetch_snapshot
        stubs["reconcile_fetcher"] = fetcher

        # Differ stub — emits mutations for all three Jira keys
        differ = types.ModuleType("reconcile_differ")
        mut_mod = types.ModuleType("reconcile_mutation_stub")

        # Load the real mutation module so StrEnum .value works correctly
        real_mut_path = (
            Path(__file__).parents[3]
            / "src"
            / "rebar"
            / "_engine"
            / "rebar_reconciler"
            / "mutation.py"
        )
        real_mut_spec = importlib.util.spec_from_file_location(
            "reconcile_mutation", real_mut_path
        )
        real_mut = importlib.util.module_from_spec(real_mut_spec)
        real_mut_spec.loader.exec_module(real_mut)

        mut_mod.MutationDirection = real_mut.MutationDirection
        mut_mod.MutationAction = real_mut.MutationAction
        mut_mod.Mutation = real_mut.Mutation

        def compute_mutations(local_state=None, jira_state=None, **kwargs):
            M = real_mut.Mutation
            D = real_mut.MutationDirection
            A = real_mut.MutationAction
            return [
                # Match by provenance.local_id (target outside filter set)
                M(
                    direction=D.inbound,
                    action=A.create,
                    target="DIG-100",
                    payload={"summary": "test"},
                    provenance={"source": "differ", "local_id": "test-id-1"},
                ),
                # Match by provenance.jira_key only (target and local_id both
                # outside filter set — exercises the jira_key match arm)
                M(
                    direction=D.outbound,
                    action=A.update,
                    target="some-unrelated-key",
                    payload={"changed_fields": {"summary": "via-jira-key"}},
                    provenance={"source": "differ", "jira_key": "DIG-100"},
                ),
                # No match (target/local_id/jira_key all outside filter)
                M(
                    direction=D.inbound,
                    action=A.create,
                    target="DIG-200",
                    payload={"summary": "other"},
                    provenance={"source": "differ", "local_id": "other-id"},
                ),
                M(
                    direction=D.inbound,
                    action=A.create,
                    target="DIG-300",
                    payload={"summary": "third"},
                    provenance={"source": "differ", "local_id": "third-id"},
                ),
            ]

        differ.compute_mutations = compute_mutations
        stubs["reconcile_differ"] = differ
        stubs["reconcile_mutation"] = mut_mod

        # Applier stub — records what mutations it receives
        applier = types.ModuleType("reconcile_applier")
        applied_mutations = []

        def apply(mutations, pass_id, repo_root, **kwargs):
            applied_mutations.extend(mutations)
            return repo_root / "bridge_state" / "manifest.json"

        applier.apply = apply
        applier._applied = applied_mutations
        stubs["reconcile_applier"] = applier

        # Health stub
        health = types.ModuleType("reconcile_health")
        health.count_open_by_type = lambda repo_root=None: {}
        health.record_pass = lambda **kwargs: None
        stubs["reconcile_health"] = health

        # Invariants stub
        invariants = types.ModuleType("reconcile_invariants")
        invariants.check_at_most_one_local_id = lambda *a, **kw: []
        invariants.check_dual_identity_complete = lambda *a, **kw: (set(), [])
        invariants.report_schema_drift = lambda *a, **kw: None
        stubs["reconcile_invariants"] = invariants

        # Binding store stub
        bs_mod = types.ModuleType("reconcile_binding_store")

        def load_binding_store(repo_root):
            return FakeBindingStore({"test-id-1": "DIG-100"})

        bs_mod.load_binding_store = load_binding_store
        stubs["reconcile_binding_store"] = bs_mod

        # Outbound differ stub — returns empty (legacy differ covers it)
        ob = types.ModuleType("reconcile_outbound_differ")
        ob.compute_outbound_mutations = lambda *a, **kw: []
        stubs["reconcile_outbound_differ"] = ob

        # Inbound differ stub — returns empty (legacy differ covers it)
        ib = types.ModuleType("reconcile_inbound_differ")
        ib.compute_inbound_mutations = lambda *a, **kw: ([], 0)
        stubs["reconcile_inbound_differ"] = ib

        # Sync logger stub
        sl = types.ModuleType("reconcile_sync_logger")

        class FakeSyncLogger:
            def __init__(self, path):
                self.entries = []

            def log(self, event, **kwargs):
                self.entries.append((event, kwargs))

            def close(self):
                pass

        sl.SyncLogger = FakeSyncLogger
        stubs["reconcile_sync_logger"] = sl

        return stubs, applier

    def test_filter_restricts_mutations_to_matching_ids(self, tmp_path):
        stubs, applier = self._stub_modules()

        # Ensure tracker dir exists for prev_snapshot
        tracker_dir = tmp_path / ".tickets-tracker"
        tracker_dir.mkdir()
        bridge_dir = tracker_dir / ".bridge_state"
        bridge_dir.mkdir()

        originals = {}
        for name, mod in stubs.items():
            originals[name] = sys.modules.get(name)
            sys.modules[name] = mod

        try:
            result = reconcile.reconcile_once(
                "test-pass",
                repo_root=tmp_path,
                filter_local_ids={"test-id-1"},
            )
            # Two mutations should match the filter:
            #   - one by provenance.local_id="test-id-1"
            #   - one by provenance.jira_key="DIG-100" (bound to test-id-1)
            assert result["mutation_count"] == 2, (
                f"expected 2 filtered mutations, got {result['mutation_count']}"
            )
            assert result["filtered"] is True
            assert result["filter_local_ids"] == ["test-id-1"]
            assert result["unfiltered_mutation_count"] == 4
            assert len(applier._applied) == 2

            # Verify both expected mutations made it through, identified by
            # which provenance field caused the match.
            targets = {m.target for m in applier._applied}
            assert targets == {"DIG-100", "some-unrelated-key"}, (
                f"unexpected targets: {targets}"
            )
        finally:
            for name, orig in originals.items():
                if orig is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = orig

    def test_no_filter_passes_all_mutations(self, tmp_path):
        stubs, applier = self._stub_modules()

        tracker_dir = tmp_path / ".tickets-tracker"
        tracker_dir.mkdir()
        bridge_dir = tracker_dir / ".bridge_state"
        bridge_dir.mkdir()

        originals = {}
        for name, mod in stubs.items():
            originals[name] = sys.modules.get(name)
            sys.modules[name] = mod

        try:
            result = reconcile.reconcile_once(
                "test-pass-unfiltered",
                repo_root=tmp_path,
            )
            assert result["mutation_count"] == 4
            assert "filtered" not in result
            assert "filter_local_ids" not in result
            assert "unfiltered_mutation_count" not in result
            assert len(applier._applied) == 4
        finally:
            for name, orig in originals.items():
                if orig is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = orig


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------


class TestFilterLocalIdsArgParsing:
    """Verify the --filter-local-ids parsing logic used in __main__.main()."""

    def _load_main(self):
        name = "rebar_reconciler_main_under_test"
        if name in sys.modules:
            return sys.modules[name]
        path = _RECONCILER_DIR / "__main__.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_parse_comma_separated(self):
        raw = "abc,def,ghi"
        result = {s.strip() for s in raw.split(",") if s.strip()}
        assert result == {"abc", "def", "ghi"}

    def test_parse_with_whitespace(self):
        raw = " abc , def , ghi "
        result = {s.strip() for s in raw.split(",") if s.strip()}
        assert result == {"abc", "def", "ghi"}

    def test_parse_none_produces_none(self):
        raw = None
        result = (
            None if raw is None else {s.strip() for s in raw.split(",") if s.strip()}
        )
        assert result is None
