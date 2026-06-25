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
import tomllib

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _allowlisted_path_for(code: str) -> str | None:
    """Return a concrete source path under a per-file-ignores entry that suppresses *code*
    (a still-un-swept package), or None if the allowlist no longer exempts any src/ package
    for that code (e.g. after the close-out empties it). Reads the live pyproject so the
    test tracks the shrinking allowlist instead of hardcoding a package that gets swept."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    ignores = data["tool"]["ruff"]["lint"]["per-file-ignores"]
    for glob, codes in ignores.items():
        if code in codes and glob.startswith("src/") and "plan_review" not in glob:
            # Turn a glob like "src/rebar/_commands/*" or "src/rebar/mcp_server.py" into a
            # concrete file path the ignore matches.
            base = glob.rstrip("*")
            return base if base.endswith(".py") else base + "_probe.py"
    return None


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
            # --no-cache: never write a .ruff_cache into the checkout (cwd=REPO_ROOT);
            # the repo-isolation guard fails the run if a test leaks a new entry.
            "--no-cache",
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
    allowlist is in effect. Skips once the allowlist no longer exempts any src/ package
    (the close-out empties it), since there is then no allowlisted path to probe."""
    ble_path = _allowlisted_path_for("BLE001")
    if ble_path is None:
        pytest.skip("BLE001 allowlist is empty (close-out reached) — nothing to suppress")
    assert "BLE001" not in _ruff_codes(ble_path), f"expected {ble_path} to suppress BLE001"

    t201_path = _allowlisted_path_for("T201")
    if t201_path is not None:
        assert "T201" not in _ruff_codes(t201_path), f"expected {t201_path} to suppress T201"


def test_exemplar_package_is_clean() -> None:
    """The real llm/plan_review/ package has no BLE001/T201 violations."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",  # don't leak a .ruff_cache into the checkout (cwd=REPO_ROOT)
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
