"""Story da25 (epic sure-foyer-aroma): the deterministic conflict-marker DET criterion.

Pins: the detector registers on the opengrep backend in `generic` (polyglot) mode under the
dedicated `rebar.builtin.smell.conflict-markers.` prefix (NOT the fail-closed security prefix); a
real opengrep/semgrep scan MATCHES the unambiguous merge-block delimiters (`<<<<<<<`, `>>>>>>>`,
the diff3 `|||||||`) on a changed file and does NOT match the FP-guarded negatives (a bare
`=======` separator inside a string / heredoc / Markdown underline); and the consumer keeps the
verdict ADVISORY (never auto-BLOCK, fail-open) for this criterion.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PREFIX = "rebar.builtin.smell.conflict-markers."
CRITERION = "conflict-markers"

# opengrep runs via the semgrep fallback binary; skip the real-scan tests when neither is present.
_HAS_ENGINE = shutil.which("opengrep") is not None or shutil.which("semgrep") is not None

# A committed merge conflict: both block delimiters (and here the `=======` separator too).
POSITIVE_PY = """\
def f():
<<<<<<< HEAD
    return 1
=======
    return 2
>>>>>>> feature-branch
"""

# The diff3 base marker (`|||||||`) also delimits a conflict block.
POSITIVE_DIFF3 = """\
value =
<<<<<<< ours
    1
||||||| base
    0
=======
    2
>>>>>>> theirs
"""

# FP guards: a bare `=======` is NOT a conflict — it is a string literal, a heredoc divider, a
# comment rule, or a Markdown H1 underline. None of these carry the block delimiters, so none fire.
NEGATIVE_PY = '''\
SEP = "======="
DOC = """
Section Title
=======
body text
"""
x = 7  # ======= divider, not a conflict
'''

NEGATIVE_MD = """\
Heading
=======

Some prose with an equals rule above (Markdown H1 underline).
"""


# ── registration + routing ───────────────────────────────────────────────────────────────────
def test_conflict_marker_detector_registers_generic_on_opengrep():
    from rebar.grounding.detectors import BACKEND_OPENGREP, load_registry

    cm = [d for d in load_registry() if d.id.startswith(PREFIX)]
    assert len(cm) == 1
    assert all(d.backend == BACKEND_OPENGREP for d in cm)
    assert all(d.dimension == "smell_generic" for d in cm)
    # `generic` (polyglot) language so it scans every file type, not one language.
    assert "generic" in cm[0].languages
    body = Path("src/rebar/grounding/detectors/builtin/smell_conflict_markers.yaml").read_text()
    assert PREFIX in body


def test_conflict_markers_is_a_dedicated_advisory_det_criterion():
    # Routed to its OWN criterion, NOT swept into the fail-closed `high-critical-security` prefix,
    # and ADVISORY (blocking_enabled False / fail_mode open) so it never auto-blocks.
    from rebar.llm.code_review import registry

    dm = registry.det_criteria()
    assert CRITERION in dm
    assert dm[CRITERION]["fail_mode"] == "open"
    _threshold, blocking = registry.threshold_for([CRITERION])
    assert blocking is False
    routed = registry.criterion_for_detector(f"{PREFIX}git-merge-conflict", dm)
    assert routed == CRITERION


# ── real scan: positive matches + negative FP guards ─────────────────────────────────────────
@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_positive_conflict_markers_match(tmp_path):
    from rebar.llm.code_review.detectors import run_detectors

    (tmp_path / "bad.py").write_text(POSITIVE_PY)
    (tmp_path / "diff3.py").write_text(POSITIVE_DIFF3)
    out = run_detectors(changed_files=["bad.py", "diff3.py"], repo_root=str(tmp_path))
    cm = out.get(CRITERION, {"matches": []})
    matched_files = {(m.get("location") or {}).get("file") for m in cm["matches"]}
    assert matched_files == {"bad.py", "diff3.py"}


@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_negative_bare_equals_does_not_match(tmp_path):
    from rebar.llm.code_review.detectors import run_detectors

    (tmp_path / "ok.py").write_text(NEGATIVE_PY)
    (tmp_path / "ok.md").write_text(NEGATIVE_MD)
    out = run_detectors(changed_files=["ok.py", "ok.md"], repo_root=str(tmp_path))
    cm = out.get(CRITERION, {"matches": []})
    # A bare `=======` (string / heredoc / Markdown underline) is FP-prone and NOT matched.
    assert cm["matches"] == []


@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_conflict_marker_match_stays_advisory(tmp_path, monkeypatch):
    # A match on this criterion must NOT force a BLOCK (advisory posture). Isolate the consumer to
    # only this criterion so an unrelated fail-closed security abstain does not colour the result.
    from rebar.llm.code_review import detectors, registry

    only = {
        CRITERION: {
            "detector": {"id_prefix": PREFIX},
            "fail_mode": "open",
        }
    }
    monkeypatch.setattr(registry, "det_criteria", lambda: only)

    (tmp_path / "bad.py").write_text(POSITIVE_PY)
    verdict = {"verdict": "PASS"}
    detectors.apply_failclosed(verdict, changed_files=["bad.py"], repo_root=str(tmp_path))
    assert verdict["verdict"] == "PASS"  # advisory — a match never auto-blocks
    notes = verdict.get("coverage", {}).get("security_detectors", [])
    note = next(n for n in notes if n["criterion"] == CRITERION)
    assert note["reason"] == "detector-finding"
    assert note["blocking"] is False
    assert note["count"] >= 1


@pytest.mark.skipif(not _HAS_ENGINE, reason="opengrep/semgrep not installed")
def test_clean_diff_produces_no_finding(tmp_path):
    from rebar.llm.code_review.detectors import run_detectors

    (tmp_path / "clean.py").write_text("def f():\n    return 1 + 2\n")
    out = run_detectors(changed_files=["clean.py"], repo_root=str(tmp_path))
    assert out.get(CRITERION, {"matches": []})["matches"] == []
