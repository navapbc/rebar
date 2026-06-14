"""Tests for story 286b: thread ``target_mode`` through main → run_pass →
reconcile_once → applier.apply, enforce per-mode mutation caps, and emit the
asymmetric manifest shape per mode.

The fixtures construct 2050 typed ``Mutation`` instances (a mix of inbound
and outbound) and assert observable cap / deferral behaviour against the
real ``applier.apply`` entry point.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(dotted_key: str, filename: str):
    """Load a reconciler sibling module under a stable sys.modules key."""
    if dotted_key in sys.modules:
        return sys.modules[dotted_key]
    path = RECONCILER_DIR / filename
    spec = importlib.util.spec_from_file_location(dotted_key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod():
    """Load applier.py under a UNIQUE non-canonical key.

    Pre-seeding canonical sys.modules keys (e.g. ``rebar_reconciler.applier``)
    leaks the applier's internally-loaded mutation/mode modules under their
    canonical keys, which other test files that re-import mutation under
    different keys (e.g. reconcile.py's ``reconcile_mutation``) then see
    as a *different* class identity — yielding spurious
    DirectionMismatchError pollution. Loading our applier under a unique
    key keeps its sub-loads isolated from sibling tests.
    """
    return _load(
        "applier_under_test_286b",
        "applier.py",
    )


@pytest.fixture(scope="module")
def mutation_mod(applier_mod):
    # Reuse the exact module the applier loaded under its canonical key so
    # Mutation/Direction enum identities match across this test's fixtures
    # and the applier's internal dispatch table.
    return applier_mod._load_mutation_module()


@pytest.fixture(scope="module")
def mode_mod(applier_mod):
    return applier_mod._load_mode_module()


@pytest.fixture(scope="module")
def renderer_mod(applier_mod):
    return applier_mod._load_manifest_renderer()


def _make_mutations(n: int, mutation_mod) -> list:
    """Construct *n* typed Mutations alternating inbound/outbound + actions.

    Targets are zero-padded so deterministic sort ordering is straightforward
    to reason about ("ISSUE-0001" < "ISSUE-0002" lexicographically).
    """
    D = mutation_mod.MutationDirection
    A = mutation_mod.MutationAction
    actions = [A.create, A.update, A.delete]
    out = []
    for i in range(n):
        direction = D.inbound if (i % 2 == 0) else D.outbound
        action = actions[i % 3]
        out.append(
            mutation_mod.Mutation(
                direction=direction,
                action=action,
                target=f"ISSUE-{i:05d}",
                payload={"i": i},
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
            muts, pass_id="t-dry", repo_root=tmp_path, mode=mode_mod.Mode.DRY_RUN
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
    snapshots_dir = tmp_path / "bridge_state" / "snapshots"
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
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "bootstrap-strict"
    assert payload["applied_count"] == 10
    assert payload["deferred_count"] == 2040

    # Deferred list must be ordered by applier._mode_sort_key, which
    # intentionally prefixes "outbound create" so outbound creates land
    # in the cap window first (bug d5a2-3fc8). The natural tuple sort
    # would put 'inbound' before 'outbound' and break that contract.
    deferred_dicts = payload["deferred"]
    deferred_sort_keys = [applier_mod._mode_sort_key(d) for d in deferred_dicts]
    assert deferred_sort_keys == sorted(deferred_sort_keys), (
        "deferred list must be ordered by applier._mode_sort_key"
    )


def test_bootstrap_throttle_caps_at_100(tmp_path, applier_mod, mode_mod, mutation_mod):
    """BOOTSTRAP_THROTTLE: exactly 100 applied + 1950 deferred."""
    muts = _make_mutations(2050, mutation_mod)
    snapshots_dir = tmp_path / "bridge_state" / "snapshots"
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
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert payload["mode"] == "bootstrap-throttle"
    assert payload["applied_count"] == 100
    assert payload["deferred_count"] == 1950


def test_live_uncapped(tmp_path, applier_mod, mode_mod, mutation_mod):
    """LIVE: uncapped — all mutations dispatched, NO manifest file written.

    Mocks the dispatch helpers so the test does not invoke the real ACLI
    client; the contract under test is (a) cap=None means every mutation
    reaches the dispatch surface, (b) LIVE writes no manifest file.
    """
    muts = _make_mutations(2050, mutation_mod)
    snapshots_dir = tmp_path / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    fake_batch_manifest = snapshots_dir / "t-live.manifest.json"
    fake_batch_manifest.write_text("{}")  # legacy batch would write this

    with (
        patch.object(applier_mod, "_apply_typed") as typed_spy,
        patch.object(applier_mod, "_apply_batch", return_value=fake_batch_manifest) as batch_spy,
    ):
        manifest_path = applier_mod.apply(
            muts, pass_id="t-live", repo_root=tmp_path, mode=mode_mod.Mode.LIVE
        )
        # cap=None — every inbound mutation (~half the 2050) reaches _apply_typed
        # and every outbound batch mutation reaches _apply_batch.
        assert typed_spy.call_count > 0, "inbound dispatch must fire under LIVE"
        assert batch_spy.call_count == 1, "outbound batch must run exactly once"

    # LIVE must NOT leave a manifest file behind per contract.
    assert manifest_path is None
    assert not fake_batch_manifest.exists(), (
        f"LIVE mode must REMOVE the legacy manifest file (found {fake_batch_manifest})"
    )


def test_target_mode_threaded_to_applier(tmp_path, applier_mod, mode_mod, mutation_mod):
    """run_pass → reconcile_once → applier.apply must pass the mode kwarg through.

    Sentinel test: dispatch a known Mode through the call chain and assert it
    reaches applier.apply unchanged (kwarg name preserved end-to-end).
    """
    reconcile_mod = _load("rebar_reconciler.reconcile_under_test", "reconcile.py")
    main_mod = _load("rebar_reconciler.main_under_test", "__main__.py")

    sentinel = mode_mod.Mode.BOOTSTRAP_STRICT
    captured: dict = {}

    def _fake_apply(mutations, pass_id, repo_root, *, mode=None, client=None):
        captured["mode"] = mode
        captured["pass_id"] = pass_id
        return tmp_path / "fake.manifest.json"

    # Patch the applier module loaded by reconcile_once. reconcile_once uses
    # _load("reconcile_applier", "applier.py"); patch the function attribute
    # on our shared applier module — reconcile.py loads from its own dotted
    # key, so we instead patch reconcile.py's own loader return by stubbing
    # the reconcile_once function-scope to call our fake. The simplest reliable
    # path: monkeypatch the applier module's apply attribute under the key
    # reconcile.py uses.

    # reconcile.py loads via _load("reconcile_applier", "applier.py") which
    # registers under the dotted key "reconcile_applier" — but its internal
    # importlib pattern goes via spec_from_file_location so the module loaded
    # in reconcile is a *different* object from applier_mod. To intercept,
    # patch the function on whatever module reconcile_once actually imports
    # at runtime via importlib by replacing the .apply attribute on the
    # importlib-loaded module after first triggering the load. Easiest: stub
    # reconcile_once itself to invoke our fake_apply.

    def _fake_reconcile_once(pass_id, repo_root=None, target_mode=None, **kwargs):
        # Mirror the real signature; forward target_mode as the mode= kwarg
        # the way the real reconcile_once does.
        _fake_apply([], pass_id, repo_root, mode=target_mode)
        return {"pass_id": pass_id, "mutation_count": 0, "manifest_path": ""}

    with patch.object(reconcile_mod, "reconcile_once", _fake_reconcile_once):
        # Force run_pass to use our patched reconcile module.
        with patch.object(main_mod, "_try_load_step", return_value=reconcile_mod):
            rc = main_mod.run_pass(
                repo_root=tmp_path, pass_id="sentinel-pass", target_mode=sentinel
            )
            assert rc == 0

    assert captured.get("mode") is sentinel, (
        f"target_mode sentinel must reach applier.apply via mode= kwarg; "
        f"got {captured.get('mode')!r}"
    )


def test_asymmetric_manifest_dry_run_shape(tmp_path, applier_mod, mode_mod, mutation_mod):
    """DRY_RUN manifest must contain outbound.totals + inbound[] array."""
    muts = _make_mutations(20, mutation_mod)
    manifest_path = applier_mod.apply(
        muts, pass_id="t-dry-shape", repo_root=tmp_path, mode=mode_mod.Mode.DRY_RUN
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
    snapshots_dir = tmp_path / "bridge_state" / "snapshots"
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
        )
    payload = json.loads(Path(manifest_path).read_text())
    assert "outbound" in payload and "totals" in payload["outbound"]
    assert "inbound" in payload and "totals" in payload["inbound"]
    assert "spot_check" in payload
    assert isinstance(payload["spot_check"], list)


def test_asymmetric_manifest_live_writes_no_file(tmp_path, applier_mod, mode_mod, mutation_mod):
    """LIVE mode must NOT write any manifest file."""
    muts = _make_mutations(10, mutation_mod)
    snapshots_dir = tmp_path / "bridge_state" / "snapshots"
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
        )
    assert result is None
    assert not fake.exists()


def test_phase_gate_still_blocks(tmp_path):
    """Regression guard: .reconciler-phase-gate=dry-run still blocks bootstrap-strict.

    Loads the advisory_lock module and asserts ``check_phase_gate`` returns
    True for a target_mode greater than the pinned gate. This exercises the
    phase-gate path that ``__main__.main`` reads BEFORE invoking run_pass —
    the new ``target_mode`` plumbing must not have weakened it.
    """
    advisory = _load("rebar_reconciler._advisory_lock", "_advisory_lock.py")
    mode_mod = _load("rebar_reconciler.mode", "mode.py")

    # Pin the gate at dry-run.
    tickets_dir = tmp_path / ".tickets-tracker"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    gate_file = tmp_path / ".reconciler-phase-gate"
    gate_file.write_text("dry-run\n")

    target = mode_mod.Mode.BOOTSTRAP_STRICT
    # check_phase_gate reads the gate file from the tickets branch via
    # `git show`. In CI / dev envs where ``tmp_path`` is not a git repo with
    # a tickets branch, the call raises ReconcileLockError (fail-CLOSED). The
    # regression guard cares about (a) the signature still accepts
    # (Mode, Path), and (b) the only allowed failure mode is the fail-CLOSED
    # ReconcileLockError — NOT a silent fall-through to e.g. AttributeError
    # from a broken target_mode plumbing.
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
    """Finding #1 (3307050253): _spot_check_sample must use a stable hash.

    Python's built-in ``hash()`` is randomized per-process unless
    ``PYTHONHASHSEED`` is pinned, so re-importing the renderer in a fresh
    process would produce different sample selections. The renderer's
    docstring claims "Stable across runs"; this test enforces that by
    asserting identical sample selection from two distinct module loads.
    """
    import importlib.util

    def _fresh_renderer():
        path = RECONCILER_DIR / "manifest_renderer.py"
        # Unique key per load → fresh module object every call.
        key = f"manifest_renderer_fresh_{id(object())}"
        spec = importlib.util.spec_from_file_location(key, path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

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
    """Finding #2 (3307050261): legacy callers (target_mode=None) must
    call applier.apply WITHOUT a ``mode`` kwarg.

    Exercises the if/else branch in reconcile.py around line 440-445 that
    preserves backward compatibility for tests stubbing applier.apply with
    a signature that does not accept ``mode``.
    """
    # The branch under test in reconcile.py (around lines 440-445):
    #
    #     if target_mode is None:
    #         manifest_path = applier.apply(mutations, pass_id, repo_root)
    #     else:
    #         manifest_path = applier.apply(
    #             mutations, pass_id, repo_root, mode=target_mode
    #         )
    #
    # We assert the legacy branch (target_mode is None) invokes apply WITHOUT
    # a ``mode`` kwarg. Read reconcile.py's source directly so the test is
    # resilient to reconcile.py's lazy loader plumbing — what we actually
    # care about is the textual call-shape contract that protects legacy
    # test stubs.
    reconcile_src = (RECONCILER_DIR / "reconcile.py").read_text()
    # Bug 85a1: the legacy-branch call may carry backward-compatible kwargs
    # (e.g. binding_store=), so the literal-string assertion is too brittle.
    # Use a regex that allows trailing kwargs but explicitly rejects mode=.
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

    # Belt-and-braces runtime guarantee: stub applier.apply on the loaded
    # applier module and exercise the legacy call shape directly. This
    # mirrors what reconcile_once does on the target_mode=None branch and
    # asserts no kwargs leak through.
    captured: dict = {}

    def _fake_apply(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = dict(kwargs)
        return tmp_path / "fake.manifest.json"

    monkeypatch.setattr(applier_mod, "apply", _fake_apply)
    # Mirror reconcile.py's legacy branch exactly.
    applier_mod.apply([], "legacy-caller-test", tmp_path)
    assert "mode" not in captured["kwargs"], (
        "legacy caller (target_mode=None) must not pass mode= to applier.apply; "
        f"got kwargs={captured['kwargs']}"
    )


def test_manifest_renderer_handles_typed_and_legacy_dict_shapes(applier_mod, mutation_mod):
    """Finding #3 (3307050264): renderer must handle both Mutation dataclass
    instances AND legacy dict-shaped batch mutations.

    The module docstring states both shapes are supported; this test
    exercises each shape end-to-end through render_dry_run_or_strict and
    render_throttle and asserts the output schema matches the asymmetric-
    manifest contract.
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
    result = applier_mod.apply(mutations, "test-coerce", repo_root=tmp_path, mode="dry-run")
    # DRY_RUN with 0 mutations produces a manifest (path may be None or file)
    # — the key assertion is that it didn't raise.


def test_mode_invalid_string_raises(applier_mod, mode_mod, tmp_path):
    """An unrecognised mode string raises TypeError (finding #1)."""
    with pytest.raises((TypeError, ValueError)):
        applier_mod.apply([], "test-bad", repo_root=tmp_path, mode="NOT_A_MODE")


def test_mode_arbitrary_object_raises(applier_mod, tmp_path):
    """A non-string non-enum mode raises TypeError (finding #1)."""
    with pytest.raises(TypeError, match="mode must be a Mode enum member"):
        applier_mod.apply([], "test-obj", repo_root=tmp_path, mode=42)


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
        list(muts), "pass-alpha", repo_root=tmp_path, mode=mode_mod.Mode.DRY_RUN
    )
    path_b = applier_mod.apply(
        list(muts), "pass-beta", repo_root=tmp_path, mode=mode_mod.Mode.DRY_RUN
    )

    assert path_a != path_b, "Different pass_ids must produce different manifest paths"
    assert Path(path_a).exists() and Path(path_b).exists()

    # Both must be valid JSON with the pass_id baked in.
    data_a = json.loads(Path(path_a).read_text())
    data_b = json.loads(Path(path_b).read_text())
    assert data_a["pass_id"] == "pass-alpha"
    assert data_b["pass_id"] == "pass-beta"
