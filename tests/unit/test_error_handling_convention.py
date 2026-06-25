"""Lint-gate proving test for the error-handling convention (epic ring-gun-jot).

Universal `make lint` passing does NOT by itself prove the BLE001/T201 rules actually
*fire* on the exemplar package vs. merely being added to `select` (plan-review advisory
fc67). These tests prove the gate is wired correctly by feeding ruff synthetic code via
`--stdin-filename` so the configured `per-file-ignores` allowlist is applied by path:

* a broad `except Exception` + `print()` on a `llm/plan_review/` path is FLAGGED
  (the exemplar is fully gated — not exempt);
* the same code on an allowlisted (not-yet-swept) path is SUPPRESSED;
* the real `llm/plan_review/` package is itself clean for BLE001 + T201.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Synthetic offender: a blind except (BLE001) and a print (T201).
_OFFENDING_SRC = (
    "def f():\n    try:\n        pass\n"
    "    except Exception:\n        print('x')\n        return 1\n"
)


def _ruff_codes(stdin_filename: str) -> set[str]:
    """Run ruff (BLE001+T201) on the offending source AS IF it lived at *stdin_filename*,
    returning the set of rule codes reported. `--stdin-filename` makes ruff apply the
    `per-file-ignores` allowlist by path without writing into the tree."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "BLE001,T201",
            "--output-format",
            "concise",
            "--stdin-filename",
            stdin_filename,
            "-",
        ],
        input=_OFFENDING_SRC,
        cwd=str(_REPO_ROOT),
        text=True,
        capture_output=True,
    )
    out = proc.stdout + proc.stderr
    return {code for code in ("BLE001", "T201") if code in out}


def test_exemplar_path_is_gated() -> None:
    """A broad-except + print on a plan_review path is flagged — the exemplar is NOT exempt."""
    codes = _ruff_codes("src/rebar/llm/plan_review/_probe.py")
    assert codes == {"BLE001", "T201"}, f"expected both rules to fire on exemplar, got {codes}"


def test_allowlisted_path_is_suppressed() -> None:
    """The same code on a not-yet-swept (allowlisted) path is suppressed — the shrinking
    allowlist is in effect."""
    codes = _ruff_codes("src/rebar/_store/_probe.py")
    assert codes == set(), f"expected the allowlist to suppress both rules, got {codes}"


def test_exemplar_package_is_clean() -> None:
    """The real llm/plan_review/ package has no BLE001/T201 violations."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "BLE001,T201",
            "src/rebar/llm/plan_review/",
        ],
        cwd=str(_REPO_ROOT),
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, f"plan_review not BLE001/T201-clean:\n{proc.stdout}{proc.stderr}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
