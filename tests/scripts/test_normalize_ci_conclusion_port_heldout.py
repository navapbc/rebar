"""HELD-OUT parity/adversarial suite for the normalize_ci_conclusion port (ticket 9d07).

Not shown to the implementer. Pins byte-level parity quirks of the shell original:
case-sensitivity, FAILURE_OBSERVED exact-match "true", unset-defaults, stdout format,
exit-0-on-anomaly, stdlib-only single-file portability, .sh fully deleted, and the
workflow's trusted sparse-checkout shape (cone-mode false) surviving the swap.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_REPO = Path(__file__).resolve().parents[2]
_PY = _REPO / "scripts" / "normalize_ci_conclusion.py"
_SH = _REPO / "scripts" / ("normalize_ci_conclusion" + ".sh")
_WORKFLOW = _REPO / ".github" / "workflows" / "gerrit-verify.yaml"

_VALID = {"success", "failure", "cancelled"}


def _mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("normalize_ci_conclusion_h", _PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_PY)],
        env={**env, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


# ── shell-parity quirks ──────────────────────────────────────────────────────────


def test_conclusion_matching_is_case_sensitive() -> None:
    """The shell `case` matched lowercase literals only: 'SUCCESS'/'Success' are
    unrecognized values -> fail-closed failure (never naively lowercased into success)."""
    m = _mod()
    for weird in ("SUCCESS", "Success", "FAILURE", "Cancelled", "SKIPPED"):
        assert m.normalize(weird, "false") == "failure", weird


def test_failure_observed_matches_exact_true_only() -> None:
    """The shell compared [ "$fo" = "true" ] exactly: 'TRUE', '1', 'yes' are NOT true."""
    m = _mod()
    for not_true in ("TRUE", "1", "yes", " true", "true "):
        assert m.normalize("skipped", not_true) == "success", not_true
    assert m.normalize("skipped", "true") == "failure"


def test_failure_observed_unset_defaults_false() -> None:
    """Env contract: FAILURE_OBSERVED absent -> ${FAILURE_OBSERVED:-false}."""
    cp = _run({"CONCLUSION": "skipped"})
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "success"


def test_conclusion_unset_fails_closed() -> None:
    cp = _run({"FAILURE_OBSERVED": "false"})
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "failure"


def test_stdout_is_exactly_vote_plus_newline() -> None:
    """printf '%s\\n' parity: nothing but the vote and one trailing newline on stdout."""
    for conclusion, expected in (("success", "success"), ("", "failure")):
        cp = _run({"CONCLUSION": conclusion, "FAILURE_OBSERVED": "false"})
        assert cp.stdout == f"{expected}\n", repr(cp.stdout)


def test_exit_zero_even_on_anomaly_rows() -> None:
    """Anomalies map to a failure VOTE, not a crashed process (the vote must be cast)."""
    for env in (
        {"CONCLUSION": "weird-value", "FAILURE_OBSERVED": "true"},
        {"CONCLUSION": "", "FAILURE_OBSERVED": ""},
        {"CONCLUSION": "😀", "FAILURE_OBSERVED": "false"},
    ):
        cp = _run(env)
        assert cp.returncode == 0, (env, cp.stderr)
        assert cp.stdout.strip() in _VALID


def test_domain_invariant_fuzz() -> None:
    m = _mod()
    for conclusion in (
        "success",
        "failure",
        "cancelled",
        "skipped",
        "",
        "timed_out",
        "neutral",
        "action_required",
        "  success",
        "success\n",
        "None",
    ):
        for fo in ("true", "false", "", "garbage"):
            assert m.normalize(conclusion, fo) in _VALID, (conclusion, fo)


# ── portability + landing shape ──────────────────────────────────────────────────


def test_entry_point_is_stdlib_only() -> None:
    """The workflow runs the single sparse-checked-out file with no package install:
    every top-level import must be stdlib."""
    tree = ast.parse(_PY.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    non_stdlib = {n for n in names if n not in sys.stdlib_module_names}
    assert not non_stdlib, f"non-stdlib imports: {sorted(non_stdlib)}"


def test_shell_original_is_deleted() -> None:
    assert not _SH.exists(), "the .sh must be deleted in the same change (no orphan)"


def test_no_repo_reference_to_deleted_shell_remains() -> None:
    """No test/workflow/script still points at the deleted .sh path."""
    hits: list[str] = []
    needle = "normalize_ci_conclusion" + ".sh"  # split so this file doesn't self-match
    for base in (_REPO / "tests", _REPO / ".github", _REPO / "scripts", _REPO / "Makefile"):
        paths = [base] if base.is_file() else list(base.rglob("*"))
        for p in paths:
            if not p.is_file() or p.suffix in {".pyc"} or "__pycache__" in p.parts:
                continue
            try:
                if needle in p.read_text():
                    hits.append(str(p.relative_to(_REPO)))
            except (UnicodeDecodeError, OSError):
                continue
    assert not hits, f"stale references to the deleted .sh: {hits}"


def test_workflow_sparse_checkout_fetches_only_the_py(tmp_path: Path) -> None:
    """The trusted-fetch step's sparse-checkout names the .py path and keeps
    cone-mode disabled (a single-FILE pattern needs cone-mode: false)."""
    text = _WORKFLOW.read_text()
    assert "sparse-checkout: scripts/normalize_ci_conclusion.py" in text
    assert "sparse-checkout-cone-mode: false" in text


def test_repointed_contract_test_drives_python() -> None:
    """The pre-existing subprocess contract test must target the .py (porting oracle
    re-pointed, its table preserved — not deleted, not still driving the .sh)."""
    contract = _REPO / "tests" / "scripts" / "test_normalize_ci_conclusion.py"
    assert contract.exists(), "the original contract test must survive the port"
    text = contract.read_text()
    assert "normalize_ci_conclusion.py" in text
    assert ("normalize_ci_conclusion" + ".sh") not in text
    # its key rows survive
    for row in ('"skipped"', '"cancelled"', '"weird-value"'):
        assert row in text, f"case-table row {row} lost in the re-point"
