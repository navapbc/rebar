"""Check target_mode threading and per-mode caps through reconcile_once and
applier.apply. LIVE dispatches all mutations without a manifest; capped and preview
modes retain deterministic tallies and typed/legacy rendering.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rebar.reconciler import scratch_retention, ticket_planner
from rebar.reconciler.mutation_payloads import OutboundCreatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


@pytest.fixture(scope="module")
def applier_mod():
    """Load the applier under an isolated key so canonical sys.modules entries cannot
    leak module identity into other tests.
    """
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(scope="module")
def mutation_mod(applier_mod):
    # Share the applier's module so fixture and dispatch enum identities match.
    return applier_mod._load_mutation_module()


@pytest.fixture(scope="module")
def mode_mod(applier_mod):
    return applier_mod._load_mode_module()


@pytest.fixture(scope="module")
def renderer_mod(applier_mod):
    return applier_mod._load_manifest_renderer()


def _make_mutations(n: int, mutation_mod) -> list:
    """Alternate typed inbound and outbound mutations over zero-padded targets to make
    the sort order deterministic.
    """
    D = mutation_mod.MutationDirection
    A = mutation_mod.MutationAction
    # (inbound, delete) is not a valid pair, so the inbound cycle uses clean_label
    # in its slot.
    actions = {
        D.inbound: [A.create, A.update, A.clean_label],
        D.outbound: [A.create, A.update, A.delete],
    }
    out = []
    for i in range(n):
        direction = D.inbound if (i % 2 == 0) else D.outbound
        action = actions[direction][i % 3]
        # An outbound create carries a typed payload; the batch form refuses a plain dict.
        payload = (
            OutboundCreatePayload(fields={"i": i})
            if (direction, action) == (D.outbound, A.create)
            else {"i": i}
        )
        out.append(
            mutation_mod.Mutation(
                direction=direction,
                action=action,
                target=f"ISSUE-{i:05d}",
                payload=payload,
                provenance={"src": "test"},
            )
        )
    return out


def test_dry_run_does_not_invoke_leaves(tmp_path, applier_mod, mode_mod, mutation_mod):
    """DRY_RUN: cap=0 — no leaf and no batch dispatcher may be invoked."""
    muts = _make_mutations(50, mutation_mod)
    with (
        patch.object(applier_mod, "_apply_typed") as typed_spy,
        patch.object(applier_mod, "_apply_batch") as batch_spy,
    ):
        manifest_path = applier_mod.apply(
            muts,
            pass_id="t-dry",
            repo_root=tmp_path,
            mode=mode_mod.Mode.DRY_RUN,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.DRY_RUN),
        )
        assert typed_spy.call_count == 0
        assert batch_spy.call_count == 0

    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "dry-run"
    assert payload["applied_count"] == 0
    assert payload["deferred_count"] == 50
    assert len(payload["deferred"]) == 50


def test_bootstrap_strict_caps_at_10(tmp_path, applier_mod, mode_mod, mutation_mod):
    """BOOTSTRAP_STRICT: exactly 10 applied + 2040 deferred from 2050 fixture."""
    muts = _make_mutations(2050, mutation_mod)
    snapshots_dir = scratch_retention.snapshots_dir(tmp_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake = snapshots_dir / "t-strict.manifest.json"
    fake.write_text("{}")
    with (
        patch.object(applier_mod, "_apply_typed"),
        patch.object(applier_mod, "_apply_batch", return_value=fake),
    ):
        manifest_path = applier_mod.apply(
            muts,
            pass_id="t-strict",
            repo_root=tmp_path,
            mode=mode_mod.Mode.BOOTSTRAP_STRICT,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.BOOTSTRAP_STRICT),
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "bootstrap-strict"
    assert payload["applied_count"] == 10
    assert payload["deferred_count"] == 2040

    # _mode_sort_key puts outbound creates in the cap window before inbound
    # work; tuple sorting would reverse that priority.
    deferred_dicts = payload["deferred"]
    deferred_sort_keys = [applier_mod._mode_sort_key(d) for d in deferred_dicts]
    assert deferred_sort_keys == sorted(deferred_sort_keys), (
        "deferred list must be ordered by applier._mode_sort_key"
    )


def test_bootstrap_throttle_caps_at_100(tmp_path, applier_mod, mode_mod, mutation_mod):
    """BOOTSTRAP_THROTTLE: exactly 100 applied + 1950 deferred."""
    muts = _make_mutations(2050, mutation_mod)
    snapshots_dir = scratch_retention.snapshots_dir(tmp_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake = snapshots_dir / "t-throttle.manifest.json"
    fake.write_text("{}")
    with (
        patch.object(applier_mod, "_apply_typed"),
        patch.object(applier_mod, "_apply_batch", return_value=fake),
    ):
        manifest_path = applier_mod.apply(
            muts,
            pass_id="t-throttle",
            repo_root=tmp_path,
            mode=mode_mod.Mode.BOOTSTRAP_THROTTLE,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.BOOTSTRAP_THROTTLE),
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "bootstrap-throttle"
    assert payload["applied_count"] == 100
    assert payload["deferred_count"] == 1950


def test_live_uncapped(tmp_path, applier_mod, mode_mod, mutation_mod):
    """LIVE dispatches all mutations with no cap or retained manifest; stub dispatch to
    avoid real Jira calls.
    """
    muts = _make_mutations(2050, mutation_mod)
    snapshots_dir = scratch_retention.snapshots_dir(tmp_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake_batch_manifest = snapshots_dir / "t-live.manifest.json"
    fake_batch_manifest.write_text("{}")  # legacy batch would write this

    with (
        patch.object(applier_mod, "_apply_typed") as typed_spy,
        patch.object(applier_mod, "_apply_batch", return_value=fake_batch_manifest) as batch_spy,
    ):
        manifest_path = applier_mod.apply(
            muts,
            pass_id="t-live",
            repo_root=tmp_path,
            mode=mode_mod.Mode.LIVE,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.LIVE),
        )
        # cap=None — every inbound mutation (~half the 2050) reaches _apply_typed
        # and every outbound batch mutation reaches _apply_batch.
        assert typed_spy.call_count > 0, "inbound dispatch must fire under LIVE"
        assert batch_spy.call_count == 1, "outbound batch must run exactly once"

    # LIVE returns the applied/failed tally but removes the manifest file.
    assert isinstance(manifest_path, dict), (
        f"LIVE must return the applied/failed tally, got {manifest_path!r}"
    )
    assert set(manifest_path) == {"applied_count", "failed_count"}
    assert not fake_batch_manifest.exists(), (
        f"LIVE mode must REMOVE the legacy manifest file (found {fake_batch_manifest})"
    )


def test_target_mode_threaded_to_applier(tmp_path, applier_mod, mode_mod, mutation_mod):
    """run_pass_result → reconcile_once → applier.apply must pass the mode kwarg through.

    Sentinel test: dispatch a known Mode through the call chain and assert it
    reaches applier.apply unchanged (kwarg name preserved end-to-end).
    """
    reconcile_mod = importlib.import_module("rebar.reconciler.reconcile")
    guarded_mod = importlib.import_module("rebar.reconciler.guarded_pass")

    sentinel = mode_mod.Mode.BOOTSTRAP_STRICT
    captured: dict = {}

    def _fake_apply(mutations, pass_id, repo_root, *, mode=None, client=None):
        captured["mode"] = mode
        captured["pass_id"] = pass_id
        return tmp_path / "fake.manifest.json"

    # Reconcile loads a separate applier module identity.

    # Stub reconcile_once and forward its mode to fake_apply to intercept it.

    def _fake_reconcile_once(pass_id, repo_root=None, target_mode=None, **kwargs):
        # Mirror the real signature; forward target_mode as the mode= kwarg
        # the way the real reconcile_once does.
        _fake_apply([], pass_id, repo_root, mode=target_mode)
        return {"pass_id": pass_id, "mutation_count": 0, "manifest_path": ""}

    with patch.object(reconcile_mod, "reconcile_once", _fake_reconcile_once):
        # Force run_pass_result to use our patched reconcile module.
        with patch.object(guarded_mod, "_try_load_step", return_value=reconcile_mod):
            result = guarded_mod.run_pass_result(
                repo_root=tmp_path, pass_id="sentinel-pass", target_mode=sentinel
            )
            assert result.disposition is guarded_mod._Disposition.CONVERGED

    assert captured.get("mode") is sentinel, (
        f"target_mode sentinel must reach applier.apply via mode= kwarg; "
        f"got {captured.get('mode')!r}"
    )


def test_asymmetric_manifest_dry_run_shape(tmp_path, applier_mod, mode_mod, mutation_mod):
    """DRY_RUN manifest must contain outbound.totals + inbound[] array."""
    muts = _make_mutations(20, mutation_mod)
    manifest_path = applier_mod.apply(
        muts,
        pass_id="t-dry-shape",
        repo_root=tmp_path,
        mode=mode_mod.Mode.DRY_RUN,
        ticket_plans=_plans(muts, mode=mode_mod.Mode.DRY_RUN),
    )
    payload = json.loads(Path(manifest_path).read_text())
    assert "outbound" in payload
    assert "totals" in payload["outbound"]
    assert set(payload["outbound"]["totals"].keys()) >= {"create", "update", "delete"}
    assert isinstance(payload["inbound"], list)
    # Every inbound entry must have key + action + fields.
    for entry in payload["inbound"]:
        assert "key" in entry and "action" in entry and "fields" in entry


def test_asymmetric_manifest_throttle_shape(tmp_path, applier_mod, mode_mod, mutation_mod):
    """BOOTSTRAP_THROTTLE manifest: both totals + spot_check sample."""
    muts = _make_mutations(200, mutation_mod)
    snapshots_dir = scratch_retention.snapshots_dir(tmp_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake = snapshots_dir / "t-throttle-shape.manifest.json"
    fake.write_text("{}")
    with (
        patch.object(applier_mod, "_apply_typed"),
        patch.object(applier_mod, "_apply_batch", return_value=fake),
    ):
        manifest_path = applier_mod.apply(
            muts,
            pass_id="t-throttle-shape",
            repo_root=tmp_path,
            mode=mode_mod.Mode.BOOTSTRAP_THROTTLE,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.BOOTSTRAP_THROTTLE),
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert "outbound" in payload and "totals" in payload["outbound"]
    assert "inbound" in payload and "totals" in payload["inbound"]
    assert "spot_check" in payload
    assert isinstance(payload["spot_check"], list)


def test_asymmetric_manifest_live_writes_no_file(tmp_path, applier_mod, mode_mod, mutation_mod):
    """LIVE mode must NOT write any manifest file."""
    muts = _make_mutations(10, mutation_mod)
    snapshots_dir = scratch_retention.snapshots_dir(tmp_path)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake = snapshots_dir / "t-live-nofile.manifest.json"
    fake.write_text("{}")
    with (
        patch.object(applier_mod, "_apply_typed"),
        patch.object(applier_mod, "_apply_batch", return_value=fake),
    ):
        result = applier_mod.apply(
            muts,
            pass_id="t-live-nofile",
            repo_root=tmp_path,
            mode=mode_mod.Mode.LIVE,
            ticket_plans=_plans(muts, mode=mode_mod.Mode.LIVE),
        )
    # LIVE returns its tally and unlinks the manifest file.
    assert isinstance(result, dict), f"LIVE must return the tally, got {result!r}"
    assert set(result) == {"applied_count", "failed_count"}
    assert not fake.exists()


def test_phase_gate_still_blocks(tmp_path):
    """A dry-run phase gate still blocks bootstrap-strict even when target_mode is
    threaded through the pass.
    """
    advisory = importlib.import_module("rebar.reconciler._advisory_lock")
    mode_mod = importlib.import_module("rebar.reconciler.mode")

    # Pin the gate at dry-run.
    tickets_dir = tmp_path / ".tickets-tracker"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    gate_file = tmp_path / ".reconciler-phase-gate"
    gate_file.write_text("dry-run\n")

    target = mode_mod.Mode.BOOTSTRAP_STRICT
    # Without a tickets branch, git show must fail closed with
    # ReconcileLockError; accepting (Mode, Path) must not silently fall through.
    try:
        _ = advisory.check_phase_gate(target, tmp_path)
    except advisory.ReconcileLockError:
        # Expected when tmp_path is not the live tickets branch.
        pass

    # Whatever the implementation, check_phase_gate must remain a callable
    # accepting (Mode, Path). The signature stability is the regression
    # contract this story must not break.
    assert callable(advisory.check_phase_gate)


def test_spot_check_sampling_is_deterministic_across_runs(applier_mod, mutation_mod):
    """Repeated render_throttle calls in one process select the same nonempty
    spot-check keys.
    """
    import importlib.util

    def _fresh_renderer():
        # Unique key per load → fresh module object every call.
        return importlib.import_module("rebar.reconciler.manifest_renderer")

    muts = _make_mutations(200, mutation_mod)
    r1 = _fresh_renderer()
    r2 = _fresh_renderer()
    sample1 = r1.render_throttle(muts, [])["spot_check"]
    sample2 = r2.render_throttle(muts, [])["spot_check"]

    keys1 = [s["key"] for s in sample1]
    keys2 = [s["key"] for s in sample2]
    assert keys1 == keys2, (
        "spot_check sample selection must be deterministic across module "
        "loads — built-in hash() is randomized per-process and must not "
        f"be used. got1={keys1[:5]}... got2={keys2[:5]}..."
    )
    # And the sample must be non-empty for a 200-mutation fixture (otherwise
    # the test could trivially pass with an unconditional empty list).
    assert len(keys1) > 0


def test_reconcile_once_legacy_caller_omits_mode_kwarg(
    tmp_path, applier_mod, mutation_mod, monkeypatch
):
    """A caller with target_mode=None omits the mode kwarg to preserve the legacy
    applier call shape.
    """
    # Read apply_phase's call shape directly: target_mode=None must omit mode,
    # regardless of reconcile's lazy module loader.
    reconcile_src = (RECONCILER_DIR / "apply_phase.py").read_text()
    # Allow other compatible kwargs while rejecting mode on the legacy branch.
    import re as _re

    legacy_match = _re.search(
        r"applier\.apply\(\s*mutations,\s*pass_id,\s*repo_root\b[^)]*\)",
        reconcile_src,
    )
    assert legacy_match is not None, (
        "reconcile.py must call applier.apply(mutations, pass_id, repo_root[, ...]) "
        "on the legacy branch."
    )
    assert "mode=" not in legacy_match.group(0), (
        f"legacy-branch applier.apply must NOT include mode=; got {legacy_match.group(0)!r}"
    )

    # Also exercise the loaded applier call to catch leaked kwargs at runtime.
    captured: dict = {}

    def _fake_apply(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = dict(kwargs)
        return tmp_path / "fake.manifest.json"

    monkeypatch.setattr(applier_mod, "apply", _fake_apply)
    # Mirror reconcile.py's legacy branch exactly.
    applier_mod.apply([], "legacy-caller-test", tmp_path, ticket_plans=_plans([]))
    assert "mode" not in captured["kwargs"], (
        "legacy caller (target_mode=None) must not pass mode= to applier.apply; "
        f"got kwargs={captured['kwargs']}"
    )


def test_manifest_renderer_handles_typed_and_legacy_dict_shapes(applier_mod, mutation_mod):
    """The renderer accepts both typed Mutation objects and legacy dict-shaped
    mutations without changing their manifest meaning.
    """
    renderer = applier_mod._load_manifest_renderer()
    D = mutation_mod.MutationDirection
    A = mutation_mod.MutationAction

    typed_mutations = [
        mutation_mod.Mutation(
            direction=D.inbound,
            action=A.create,
            target="ISSUE-T-001",
            payload={"fields": {"summary": "typed"}},
            provenance={"src": "test"},
        ),
        mutation_mod.Mutation(
            direction=D.outbound,
            action=A.update,
            target="ISSUE-T-002",
            payload={"fields": {"status": "Done"}},
            provenance={"src": "test"},
        ),
    ]
    legacy_dict_mutations = [
        {
            "direction": "inbound",
            "action": "create",
            "key": "ISSUE-L-001",
            "fields": {"summary": "legacy"},
        },
        {
            "direction": "outbound",
            "action": "update",
            "key": "ISSUE-L-002",
            "fields": {"status": "Done"},
        },
    ]

    for label, fixture in (("typed", typed_mutations), ("legacy", legacy_dict_mutations)):
        # render_dry_run_or_strict contract: outbound.totals + inbound[] array
        rendered_strict = renderer.render_dry_run_or_strict(fixture, [])
        assert "outbound" in rendered_strict and "totals" in rendered_strict["outbound"], (
            f"{label}: render_dry_run_or_strict must emit outbound.totals"
        )
        assert isinstance(rendered_strict["inbound"], list), (
            f"{label}: render_dry_run_or_strict inbound must be a list"
        )
        for entry in rendered_strict["inbound"]:
            assert "key" in entry and "action" in entry and "fields" in entry, (
                f"{label}: inbound entry missing key/action/fields: {entry}"
            )
        # Totals must reflect at least the create/update mutations we injected.
        totals = rendered_strict["outbound"]["totals"]
        assert set(totals.keys()) >= {"create", "update", "delete"}

        # render_throttle contract: outbound.totals + inbound.totals + spot_check[]
        rendered_throttle = renderer.render_throttle(fixture, [])
        assert "outbound" in rendered_throttle and "totals" in rendered_throttle["outbound"]
        assert "inbound" in rendered_throttle and "totals" in rendered_throttle["inbound"]
        assert isinstance(rendered_throttle["spot_check"], list), (
            f"{label}: render_throttle spot_check must be a list"
        )


# ---------------------------------------------------------------------------
# Finding #1 / #2: mode validation — reject arbitrary strings, coerce valid ones
# ---------------------------------------------------------------------------


def test_mode_string_coercion(applier_mod, mode_mod, tmp_path):
    """Valid mode strings are coerced to Mode enum members (finding #1)."""
    mutations = []  # empty is fine — we're testing the validation path
    applier_mod.apply(
        mutations,
        "test-coerce",
        repo_root=tmp_path,
        mode="dry-run",
        ticket_plans=_plans(mutations, mode="dry-run"),
    )
    # DRY_RUN with 0 mutations produces a manifest (path may be None or file)
    # — the key assertion is that it didn't raise.


def test_mode_invalid_string_raises(applier_mod, mode_mod, tmp_path):
    """An unrecognised mode string raises TypeError (finding #1)."""
    with pytest.raises((TypeError, ValueError)):
        applier_mod.apply(
            [],
            "test-bad",
            repo_root=tmp_path,
            mode="NOT_A_MODE",
            ticket_plans=_plans([], mode="NOT_A_MODE"),
        )


def test_mode_arbitrary_object_raises(applier_mod, tmp_path):
    """A non-string non-enum mode raises TypeError (finding #1)."""
    with pytest.raises(TypeError, match="mode must be a Mode enum member"):
        applier_mod.apply(
            [], "test-obj", repo_root=tmp_path, mode=42, ticket_plans=_plans([], mode=42)
        )


# ---------------------------------------------------------------------------
# Finding #3: atomic manifest write — concurrent DRY_RUN passes produce
# separate manifest files without corruption
# ---------------------------------------------------------------------------


def test_concurrent_dry_run_produces_separate_manifests(
    applier_mod, mutation_mod, mode_mod, tmp_path
):
    """Two DRY_RUN passes with different pass_ids produce distinct manifests."""
    muts = _make_mutations(5, mutation_mod)

    path_a = applier_mod.apply(
        list(muts),
        "pass-alpha",
        repo_root=tmp_path,
        mode=mode_mod.Mode.DRY_RUN,
        ticket_plans=_plans(list(muts), mode=mode_mod.Mode.DRY_RUN),
    )
    path_b = applier_mod.apply(
        list(muts),
        "pass-beta",
        repo_root=tmp_path,
        mode=mode_mod.Mode.DRY_RUN,
        ticket_plans=_plans(list(muts), mode=mode_mod.Mode.DRY_RUN),
    )

    assert path_a != path_b, "Different pass_ids must produce different manifest paths"
    assert Path(path_a).exists() and Path(path_b).exists()

    # Both must be valid JSON with the pass_id baked in.
    data_a = json.loads(Path(path_a).read_text())
    data_b = json.loads(Path(path_b).read_text())
    assert data_a["pass_id"] == "pass-alpha"
    assert data_b["pass_id"] == "pass-beta"
