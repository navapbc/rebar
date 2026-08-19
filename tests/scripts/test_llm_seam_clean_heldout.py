"""Held-out seam-clean + live-per-call proof for RP-04 C3d (llm subsystem cutover).

Observable contract, not internal structure. Two properties:

1. **Seam-clean** — once the llm subsystem's below-seam ambient config reads are cut to
   the approved seam (routed through an owned ``config.py`` resolver) or annotated as
   bounded owned-in-place env-control (``# read-via:`` markers), the config-ownership gate
   (``scripts/check_config_ownership.py``) must report **zero** findings for the 19 source
   files this slice owns, AND their ``LEGACY_EXCEPTIONS`` entries must be gone. The
   owned-in-place allowlist is CAPPED at exactly four logical reads = four ``# read-via:``
   marked lines, confined to ``gate_context.py`` (1), ``workflow/gate_ops.py`` (1),
   ``workflow/executor.py`` (1), ``workflow/interpreter.py`` (1). Every other owned file
   must carry ZERO markers — so an implementer cannot satisfy the gate by blanket-marking
   reads instead of cutting them. RED before the cutover.

2. **Live-per-call timing** — the gate ref/source defaults are resolved LIVE per operation
   (``REBAR_GATE_REF``/``REBAR_GATE_SOURCE`` > ``[snapshot]`` > default, re-read each
   call). The cut must route them through an owned resolver that STILL reads live per call,
   so a mid-operation env override is observed. A compose-once cut would silently lose the
   override. This guard is GREEN before and after the cut; it goes RED only if liveness is
   broken. Asserted through the stable public API ``gate_source.default_ref/default_source``
   (survives the internal resolver rename the cut performs).
"""

from __future__ import annotations

import re

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on
# sys.path (the CI-proven pattern; a ``scripts.`` package prefix does not resolve under
# the full-suite import mode).
import check_config_ownership as gate
import config_ownership_exceptions as exceptions
import pytest

# The 19 files this slice owns, as paths relative to ``src/rebar/`` (the form the gate
# emits and the exception registry stores).
_OWNED_FILES = (
    "llm/code_review/workflow_ops.py",
    "llm/completion.py",
    "llm/enrich_drain.py",
    "llm/gate_context.py",
    "llm/gate_source.py",
    "llm/plan_review/__init__.py",
    "llm/plan_review/det_floor.py",
    "llm/plan_review/drift_floor.py",
    "llm/plan_review/pin_health.py",
    "llm/plan_review/sizing.py",
    "llm/plan_review/workflow_ops.py",
    "llm/plan_review/xcheck.py",
    "llm/usage_log.py",
    "llm/workflow/completion_banking.py",
    "llm/workflow/criterion_preview.py",
    "llm/workflow/executor.py",
    "llm/workflow/gate_dispatch.py",
    "llm/workflow/gate_ops.py",
    "llm/workflow/interpreter.py",
)

# The enumerated owned-in-place allowlist: file -> exact number of ``# read-via:`` marked
# lines it may carry. Everything else must be ZERO.
_MARKER_BUDGET = {
    "llm/gate_context.py": 1,  # REBAR_GATE_ALLOW_UNGATED subsystem kill-switch
    "llm/workflow/gate_ops.py": 1,  # REBAR_VERIFY_PREFETCH kill-switch toggle
    "llm/workflow/executor.py": 1,  # os.environ[...] workflow ${VAR} interpolation
    "llm/workflow/interpreter.py": 1,  # os.environ[...] workflow ${VAR} interpolation
}
_TOTAL_MARKER_CAP = 4

_MARKER_RE = re.compile(r"#\s*read-via:")
_SRC = gate.REPO_ROOT / "src" / "rebar"


def _gate_findings_for_owned() -> list[str]:
    return [f for f in gate.check(_SRC) if any(name in f for name in _OWNED_FILES)]


def _marker_count(relpath: str) -> int:
    text = (_SRC / relpath).read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if _MARKER_RE.search(line))


def test_no_legacy_exceptions_remain_for_owned_files() -> None:
    remaining = [
        (e["path"], e["symbol"]) for e in exceptions.LEGACY_EXCEPTIONS if e["path"] in _OWNED_FILES
    ]
    assert remaining == [], (
        "C3d cutover must remove every LEGACY_EXCEPTIONS entry for the llm files; "
        f"still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = _gate_findings_for_owned()
    assert findings == [], (
        "config-ownership gate must report zero findings for the llm files after the "
        f"cutover; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_owned_files() -> None:
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _OWNED_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the owned files; got: {globbed}"


def test_read_via_markers_are_bounded_and_confined() -> None:
    per_file = {rel: _marker_count(rel) for rel in _OWNED_FILES}
    unexpected = {rel: n for rel, n in per_file.items() if n and rel not in _MARKER_BUDGET}
    assert unexpected == {}, (
        "owned-in-place markers are confined to the enumerated allowlist; unexpected "
        f"markers in: {unexpected}"
    )
    wrong = {
        rel: (per_file[rel], want) for rel, want in _MARKER_BUDGET.items() if per_file[rel] != want
    }
    assert wrong == {}, (
        "each allowlisted file must carry exactly its marked-line budget "
        f"(got (actual, want)): {wrong}"
    )
    total = sum(per_file.values())
    assert total == _TOTAL_MARKER_CAP, (
        f"the owned-in-place allowlist caps at {_TOTAL_MARKER_CAP} marked lines; got {total}"
    )


def test_gate_ref_is_read_live_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A mid-operation REBAR_GATE_REF override must be observed on the NEXT call — the cut
    must keep the resolver live-per-call (a compose-once cut caches the first value)."""
    from rebar.llm import gate_source

    root = str(tmp_path)  # no [snapshot] table -> env > default
    monkeypatch.setenv("REBAR_GATE_REF", "refs/for/alpha")
    assert gate_source.default_ref(root) == "refs/for/alpha"
    monkeypatch.setenv("REBAR_GATE_REF", "refs/for/omega")
    assert gate_source.default_ref(root) == "refs/for/omega", (
        "default_ref must re-read REBAR_GATE_REF live per call after the cut"
    )


def test_gate_source_is_read_live_per_call(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Same live-per-call contract for REBAR_GATE_SOURCE."""
    from rebar.llm import gate_source

    root = str(tmp_path)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    first = gate_source.default_source(root)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    second = gate_source.default_source(root)
    assert (first, second) == ("local", "attested"), (
        "default_source must re-read REBAR_GATE_SOURCE live per call after the cut; "
        f"got {(first, second)}"
    )
