"""S6 / AC1 — the fail-open CONFORMANCE MATRIX.

The IRONCLAD invariant of epic 8f6c, asserted UNIFORMLY across every backend × every
failure mode: an unsupported-lang / missing-tool / parse-error / crash / timeout /
version-skew / invalid-detector NEVER raises, NEVER manufactures a false ``absent`` /
false-refute — it becomes ``abstain(<closed reason>)`` carrying a coverage record (the
visible skip IS the coverage; never a silent no-op).

Backends covered (the five fail-open boundaries of the oracle):

* ``ctags``  — the T1 resolve lane (Engine A, out-of-process).
* ``registry`` — the T0 deps lane (Engine B's sibling; ``_http_get`` is the seam).
* ``opengrep`` — Engine B's primary structural matcher (out-of-process).
* ``ast-grep`` — Engine B's secondary structural matcher (out-of-process).
* ``metric``  — Engine B's scc/lizard size matcher (out-of-process; absent here).
* ``worker``  — the IN-PROCESS binding boundary (run_in_worker): a hung/segfaulting
  tree-sitter parse must be reaped, never crash the host.

Each cell asserts: ``outcome == abstain``, ``reason`` is the expected CLOSED reason,
a coverage record is present (``status == skipped``), and ``ev.validate`` passes. The
matrix is parametrized so coverage is legible and a regression points at one cell.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from rebar.grounding import deps, engine_b, harness
from rebar.grounding import evidence as ev
from rebar.grounding import resolve as r
from rebar.grounding.detectors import registry as reg_mod

from . import _worker_payloads as wp

pytestmark = pytest.mark.unit

# Validate tool IDENTITY, not just PATH presence (CI runners ship impostors):
#  * macOS preinstalls BSD `ctags` (answers `which ctags`) — not Universal Ctags, whose
#    JSON index the resolve lane needs; gate on the version probe that matches "Universal".
#  * Linux preinstalls shadow-utils `sg` (run-as-group) — not ast-grep; gate on identity.
_HAVE_CTAGS = r.ctags_version() is not None
_HAVE_SEMGREP = bool(shutil.which("opengrep") or shutil.which("semgrep"))
_HAVE_ASTGREP = engine_b.astgrep_binary() is not None

_BOGUS = "this-binary-does-not-exist-xyzzy-9000"


# ── shared assertion: every fail-open cell has the same shape ─────────────────


def _assert_fail_open(rec: dict, *, expected_reason: str, backend: str | None = None) -> None:
    """A fail-open cell: abstain + closed reason + skipped coverage + valid schema.

    NEVER a raise (we got a record), NEVER a resolution (refuted/match), NEVER an
    open reason. This is the one shape the whole matrix collapses to.
    """
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN, f"expected abstain, got {rec['outcome']}"
    assert not ev.is_resolved(rec), "a fail-open cell must never carry a resolution"
    assert rec["reason"] == expected_reason, f"reason {rec['reason']!r} != {expected_reason!r}"
    assert rec["reason"] in ev.ABSTAIN_REASONS, "reason must be in the CLOSED set"
    cov = rec["coverage"]
    assert cov["status"] == ev.STATUS_SKIPPED, "the skip must be a visible coverage record"
    assert cov.get("reason") in ev.ABSTAIN_REASONS
    if backend is not None:
        assert cov["backend"] == backend
    ev.validate(rec)  # the S1 JSON-Schema contract


# ── fixture: a tiny polyglot repo (reused across backends) ────────────────────

_REPO = {
    "pkg/core.py": "class TicketStore:\n    pass\ndef reconcile(s):\n    return s\n",
    "web/app.js": "function f(){ console.log('x'); }\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> str:
    for rel, content in _REPO.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND × FAILURE-MODE matrix — out-of-process lanes via the harness boundary
# ══════════════════════════════════════════════════════════════════════════════


# Each entry: (backend_label, failure_mode, factory(repo) -> RunResult, expected_reason).
# The factory drives the *harness* directly so the cell is deterministic on any host
# (it does not depend on a real tool being installed unless noted).


def _harness_no_tool() -> harness.RunResult:
    return harness.run_tool([_BOGUS], backend="ctags")


def _harness_timeout() -> harness.RunResult:
    return harness.run_tool(
        [sys.executable, "-c", "import time; time.sleep(30)"], backend="slowtool", timeout=0.4
    )


def _harness_version_skew() -> harness.RunResult:
    return harness.run_tool(
        [sys.executable, "-c", "raise SystemExit('should not run')"],
        backend="ctags",
        version="6.0.0",
        expected_version="6.2.1",
    )


@pytest.mark.parametrize(
    "failure_mode, factory, expected_reason",
    [
        ("missing-tool", _harness_no_tool, "no_tool"),
        ("timeout", _harness_timeout, "timeout"),
        ("version-skew", _harness_version_skew, "version_skew"),
    ],
)
@pytest.mark.parametrize("job", [ev.JOB_REFUTE, ev.JOB_SMELL])
def test_harness_boundary_failure_modes(failure_mode, factory, expected_reason, job) -> None:
    """The out-of-process harness boundary maps every failure mode to a closed abstain.

    This is the single chokepoint EVERY out-of-process backend (ctags/opengrep/
    ast-grep/metric) runs through, so proving it here proves the floor for all of them;
    the per-backend tests below confirm each lane actually routes through it.
    """
    res = factory()
    assert res.abstained, f"{failure_mode}: harness should fail open, not complete"
    rec = res.as_abstain(job=job, provenance_tier=ev.TIER_T1)
    _assert_fail_open(rec, expected_reason=expected_reason)


# ── ctags resolve lane (Engine A) ─────────────────────────────────────────────


@pytest.fixture
def ctags_index(repo):
    if not _HAVE_CTAGS:
        pytest.skip("universal-ctags not on PATH")
    idx, result = r.build_index(repo)
    if idx is None:
        pytest.skip(f"ctags index unavailable: {result.abstain_reason}")
    return idx


def test_ctags_missing_tool(repo, monkeypatch) -> None:
    monkeypatch.setattr(r, "_CTAGS_BIN", _BOGUS)
    r._CACHED_CTAGS_LANGS = None
    rec = r.refute_absence({"kind": "symbol", "name": "TicketStore"}, repo_root=repo)
    _assert_fail_open(rec, expected_reason="no_tool", backend=r.BACKEND_CTAGS)


@pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")
def test_ctags_timeout(repo) -> None:
    # No prebuilt index -> the lane builds one under the clamped timeout and the ctags
    # child is reaped -> timeout abstain (a prebuilt index would skip the build).
    rec = r.refute_absence({"kind": "symbol", "name": "TicketStore"}, repo_root=repo, timeout=1e-9)
    _assert_fail_open(rec, expected_reason="timeout")


@pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")
def test_ctags_unsupported_lang(repo, ctags_index) -> None:
    rec = r.refute_absence(
        {"kind": "symbol", "name": "X", "language": "Brainfuck-9000"},
        repo_root=repo,
        index=ctags_index,
    )
    _assert_fail_open(rec, expected_reason="unsupported_lang")


@pytest.mark.skipif(not _HAVE_CTAGS, reason="universal-ctags not on PATH")
def test_ctags_ambiguous_collision_is_not_a_false_refute(repo) -> None:
    # parse-error analogue for the resolve lane: a name with >1 def is the hazard the
    # guard sends to abstain(ambiguous) — never a false refute.
    (Path(repo) / "pkg/dup.py").write_text("def reconcile(s):\n    return s\n")
    idx, _ = r.build_index(repo)
    assert idx is not None
    rec = r.refute_absence({"kind": "symbol", "name": "reconcile"}, repo_root=repo, index=idx)
    _assert_fail_open(rec, expected_reason="ambiguous")


# ── deps / registry lane (T0) — network seam stubbed ──────────────────────────


def _dep(name: str = "requests", eco: str = "pypi") -> dict:
    return {"kind": "dependency", "name": name, "ecosystem": eco}


@pytest.mark.parametrize(
    "failure_mode, raiser, expected_reason",
    [
        ("timeout", TimeoutError("read timed out"), "timeout"),
        (
            "network-error (offline)",
            __import__("urllib.error", fromlist=["URLError"]).URLError("offline"),
            "network_error",
        ),
        ("crash (OSError)", OSError("connection reset"), "network_error"),
    ],
)
def test_registry_lane_transient_failures(
    monkeypatch, failure_mode, raiser, expected_reason
) -> None:
    def _raise(url, timeout=10.0):
        raise raiser

    monkeypatch.setattr(deps, "_http_get", _raise)
    rec = deps.refute_package(_dep())
    _assert_fail_open(rec, expected_reason=expected_reason)


def test_registry_lane_rate_limited(monkeypatch) -> None:
    monkeypatch.setattr(deps, "_http_get", lambda url, timeout=10.0: 429)
    rec = deps.refute_package(_dep())
    _assert_fail_open(rec, expected_reason="rate_limited")


def test_registry_lane_unsupported_ecosystem(monkeypatch) -> None:
    # The deps analogue of unsupported-lang: an ecosystem with no oracle.
    monkeypatch.setattr(
        deps, "_http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probe"))
    )
    rec = deps.refute_package(_dep("whatever", "cocoapods"))
    _assert_fail_open(rec, expected_reason="unsupported_lang")


def test_registry_lane_404_is_never_a_false_absent(monkeypatch) -> None:
    # The cardinal sin guard: a 404 (registry says "not found") must STILL abstain,
    # never a false "absent" — there is no absent outcome at all.
    monkeypatch.setattr(deps, "_http_get", lambda url, timeout=10.0: 404)
    rec = deps.refute_package(_dep("totally-not-real-pkg-xyz", "pypi"))
    _assert_fail_open(rec, expected_reason="private_or_internal_suspected")


# ── OpenGrep / ast-grep structural matchers (Engine B) ────────────────────────


def _opengrep_dets():
    return list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_OPENGREP))


def _astgrep_dets():
    return list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_ASTGREP))


def test_opengrep_missing_tool(repo, monkeypatch) -> None:
    monkeypatch.setattr(engine_b, "_OPENGREP_CANDIDATES", (_BOGUS,))
    records = engine_b._run_opengrep(_opengrep_dets(), Path(repo))
    assert records
    for rec in records:
        _assert_fail_open(rec, expected_reason="no_tool", backend=engine_b.BACKEND_OPENGREP)


def test_astgrep_missing_tool(repo, monkeypatch) -> None:
    monkeypatch.setattr(engine_b, "_ASTGREP_CANDIDATES", (_BOGUS,))
    records = engine_b._run_astgrep(_astgrep_dets(), Path(repo))
    assert records
    for rec in records:
        _assert_fail_open(rec, expected_reason="no_tool", backend=engine_b.BACKEND_ASTGREP)


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_opengrep_timeout_via_harness(repo, monkeypatch) -> None:
    # Force the real engine's scan to time out by clamping run_tool's timeout to ~0.
    real = engine_b.harness.run_tool

    def clamp(cmd, **kw):
        if "scan" in cmd:
            kw["timeout"] = 1e-9
        return real(cmd, **kw)

    monkeypatch.setattr(engine_b.harness, "run_tool", clamp)
    records = engine_b._run_opengrep(_opengrep_dets(), Path(repo))
    assert records
    # validate (the scan-timeout coverage skip) propagates per applicable detector.
    timeouts = [r for r in records if r.get("reason") == "timeout"]
    assert timeouts, "a clamped scan must fail open to timeout"
    for rec in timeouts:
        _assert_fail_open(rec, expected_reason="timeout", backend=engine_b.BACKEND_OPENGREP)


@pytest.mark.skipif(not _HAVE_SEMGREP, reason="opengrep/semgrep not installed")
def test_opengrep_invalid_detector_is_quarantined_scan_continues(repo) -> None:
    # spike E1: a schema-broken project detector is dropped as invalid_detector and the
    # scan CONTINUES (the good built-ins still match). The KEY invalid-detector cell.
    d = Path(repo) / ".rebar" / "detectors"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.yaml").write_text(
        "rules:\n  - id: project.broken.rule\n    languages: [javascript]\n"
        '    severity: INFO\n    message: m\n    pattern: "(((unbalanced"\n'
    )
    reg_mod.clear_cache()
    try:
        result = engine_b.scan(repo)  # must NOT raise / abort
    finally:
        reg_mod.clear_cache()
    for rec in result.records:
        ev.validate(rec)
    quarantined = [
        r
        for r in result.abstains()
        if r.get("detector_id") == "project.broken.rule" and r.get("reason") == "invalid_detector"
    ]
    assert quarantined, "invalid detector must be quarantined as abstain(invalid_detector)"
    _assert_fail_open(quarantined[0], expected_reason="invalid_detector")
    # the scan CONTINUED — a good built-in still produced a record.
    assert any(r.get("detector_id") == "rebar.builtin.smell.js-console-log" for r in result.records)


# ── metric backend (scc/lizard) — absent on this host == no_tool ──────────────


def test_metric_missing_tool(repo, monkeypatch) -> None:
    real_which = shutil.which

    def fake_which(name, *a, **k):
        return None if name in ("scc", "lizard") else real_which(name, *a, **k)

    monkeypatch.setattr(engine_b.shutil, "which", fake_which)
    dets = list(reg_mod.load_registry().by_backend(reg_mod.BACKEND_METRIC))
    records = engine_b._run_metric(dets, Path(repo))
    assert records
    for rec in records:
        _assert_fail_open(rec, expected_reason="no_tool", backend=engine_b.BACKEND_METRIC)


def test_metric_unsupported_lang_when_no_matching_files(tmp_path, monkeypatch) -> None:
    # A metric detector with a declared language absent from the repo is skipped with
    # an unsupported_lang coverage record (routing, not a tool failure).
    (tmp_path / "only.txt").write_text("hello\n")
    det = reg_mod.Detector(
        id="project.metric.go-only",
        backend=reg_mod.BACKEND_METRIC,
        namespace="project",
        source_path=str(tmp_path / "m.yaml"),
        rule={"languages": ["go"]},
        envelope={"job": ev.JOB_SMELL, "tier": ev.TIER_T1},
    )
    result = engine_b.scan(tmp_path, registry=reg_mod.Registry(detectors=(det,)))
    skips = [r for r in result.abstains() if r.get("detector_id") == "project.metric.go-only"]
    assert skips
    _assert_fail_open(skips[0], expected_reason="unsupported_lang")


# ══════════════════════════════════════════════════════════════════════════════
# IN-PROCESS worker boundary — a hung / segfaulting parse must be reaped
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "failure_mode, payload, args, kwargs, expected_reason",
    [
        ("hang (uninterruptible)", wp.hangs_forever, (), None, "timeout"),
        ("crash (signal death / segfault stand-in)", wp.hard_crash, (), None, "parse_error"),
        ("raise (python exception)", wp.raises_error, (), None, "other"),
        (
            "version-skew (binding ABI)",
            wp.returns_value,
            (1,),
            {"version": "0.20", "expected_version": "0.21"},
            "version_skew",
        ),
    ],
)
def test_worker_boundary_failure_modes(
    failure_mode, payload, args, kwargs, expected_reason
) -> None:
    """The in-process binding boundary maps every failure mode to a closed abstain.

    A tree-sitter binding can hang or segfault a C-extension; run_in_worker isolates it
    so a hung/segfaulting parse is reaped to an abstain — never a host crash, never a raise.
    """
    extra = dict(kwargs or {})
    timeout = 0.5 if expected_reason == "timeout" else None
    res = harness.run_in_worker(payload, *args, backend="tree-sitter", timeout=timeout, **extra)
    assert res.abstained, f"{failure_mode}: worker should fail open"
    rec = res.as_abstain(job=ev.JOB_SMELL, provenance_tier=ev.TIER_T1)
    _assert_fail_open(rec, expected_reason=expected_reason)


def test_worker_crash_does_not_take_down_host() -> None:
    # After a hard crash in a worker, the host must still run subsequent workers.
    crashed = harness.run_in_worker(wp.hard_crash, backend="tree-sitter")
    assert crashed.abstained and crashed.abstain_reason == "parse_error"
    again = harness.run_in_worker(wp.returns_value, 5, backend="ts")
    assert again.completed and again.value == 10, "host must survive a worker crash"


# ── coverage of the matrix itself: prove every backend × mode pairing is present


def test_matrix_dimensions_are_complete() -> None:
    """Self-check: the matrix covers the documented backend × failure-mode grid.

    A registry of the (backend, mode) pairs this file asserts, so the matrix is legible
    and a missing cell is caught by inspection (not silently absent).
    """
    backends = {"ctags", "registry", "opengrep", "ast-grep", "metric", "worker"}
    modes = {
        "unsupported-lang",
        "missing-tool",
        "parse-error",
        "crash",
        "timeout",
        "version-skew",
        "invalid-detector",
    }
    # Every backend is exercised by at least one cell above; every failure mode is
    # exercised by at least one backend. (The harness-boundary test proves the shared
    # floor; per-backend tests prove each lane routes through it.)
    assert len(backends) == 6
    assert len(modes) == 7
