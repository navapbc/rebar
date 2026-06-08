#!/usr/bin/env python3
"""audit_dd3_mutation_caps — Per-pass mutation count cap verifier.

Pass-log format
---------------
The --pass-log file contains one JSON object per line (JSON Lines).
Each line represents a single reconciler pass and must include:

    {
        "phase": "<phase-name>",        // string — e.g. "bootstrap-strict"
        "pass_index": <int>,            // GLOBAL 1-based pass sequence counter
                                         // (1–8 across the full bootstrap rollout,
                                         // NOT phase-relative). Cap rules below
                                         // depend on this global ordering.
        "mutation_count": <int>,        // mutations applied during this pass
        "timestamp": "<ISO-8601>"       // e.g. "2026-05-24T10:00:00Z"
    }

Cap rules
---------
- Passes 1–5  (strict)   → cap = 10
- Passes 6–8  (throttle) → cap = 100

Output artifact
---------------
<artifacts-dir>/<phase>/dd3.json:

    {
        "phase": "<phase>",
        "passes": [
            {"n": <int>, "cap": <int>, "count": <int>, "within_cap": <bool>},
            ...
        ],
        "overall_pass": <bool>
    }

Exit codes
----------
0  — all passes within cap
5  — one or more passes exceeded cap
2  — phase gate check failed (propagated from audit_dd4_phase_gate.sh)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Cap rules
# ---------------------------------------------------------------------------

_STRICT_PASS_RANGE = range(1, 6)   # 1–5 inclusive
_THROTTLE_PASS_RANGE = range(6, 9)  # 6–8 inclusive
_STRICT_CAP = 10
_THROTTLE_CAP = 100


def _cap_for_pass(n: int) -> int:
    """Return the mutation cap for pass number *n* (1-based)."""
    if n in _STRICT_PASS_RANGE:
        return _STRICT_CAP
    if n in _THROTTLE_PASS_RANGE:
        return _THROTTLE_CAP
    # Passes outside defined ranges fall back to throttle cap.
    return _THROTTLE_CAP


# ---------------------------------------------------------------------------
# Phase-gate check
# ---------------------------------------------------------------------------

def _run_phase_gate(phase: str) -> None:
    """Invoke audit_dd4_phase_gate.sh; exit with its return-code on failure."""
    gate_script = Path(__file__).resolve().parent / "audit_dd4_phase_gate.sh"
    result = subprocess.run([str(gate_script), phase], check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Pass-log parsing
# ---------------------------------------------------------------------------

def _parse_pass_log(pass_log_path: Path) -> list[dict]:
    """Parse a JSON-Lines pass-log file.

    Raises FileNotFoundError if the file does not exist.
    Raises KeyError / ValueError if a record is missing required fields.
    """
    if not pass_log_path.exists():
        raise FileNotFoundError(f"pass-log not found: {pass_log_path}")

    records: list[dict] = []
    with pass_log_path.open() as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno}: invalid JSON — {exc}") from exc
            # Required fields — raise immediately on missing keys so the audit
            # fails closed rather than silently treating absent counts as 0.
            for key in ("phase", "pass_index", "mutation_count", "timestamp"):
                if key not in obj:
                    raise KeyError(
                        f"line {lineno}: required key '{key}' missing from pass record"
                    )
            records.append(obj)
    return records


# ---------------------------------------------------------------------------
# Core cap-check logic
# ---------------------------------------------------------------------------

def check_caps(records: list[dict]) -> tuple[list[dict], bool]:
    """Apply cap rules to pass records.

    Returns (passes_list, overall_pass) where passes_list contains per-pass
    dicts with keys {n, cap, count, within_cap}.
    """
    passes = []
    overall = True
    for rec in records:
        n = int(rec["pass_index"])
        count = int(rec["mutation_count"])
        cap = _cap_for_pass(n)
        within = count <= cap
        if not within:
            overall = False
        passes.append({"n": n, "cap": cap, "count": count, "within_cap": within})
    return passes, overall


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _default_artifacts_dir() -> str:
    """Compute the default artifacts directory from the git repo root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    top = (result.stdout or "").strip() or "."
    return str(Path(top) / ".reconciler-audit-artifacts")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify per-pass mutation counts against phase caps."
    )
    parser.add_argument(
        "--pass-log",
        required=True,
        metavar="PATH",
        help="Path to the JSON-Lines pass-log file.",
    )
    parser.add_argument(
        "--phase",
        required=True,
        metavar="PHASE",
        help="Reconciler phase name (e.g. bootstrap-strict).",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        metavar="DIR",
        help="Root directory for audit artifacts (default: <repo-root>/.reconciler-audit-artifacts).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # 1. Phase-gate check — exits non-zero if gate not advanced.
    _run_phase_gate(args.phase)

    # 2. Parse pass-log.
    pass_log_path = Path(args.pass_log)
    records = _parse_pass_log(pass_log_path)

    # 3. Apply cap rules.
    passes, overall_pass = check_caps(records)

    # 4. Write artifact.
    artifacts_dir = Path(args.artifacts_dir if args.artifacts_dir is not None else _default_artifacts_dir())
    out_dir = artifacts_dir / args.phase
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dd3.json"
    payload = {"phase": args.phase, "passes": passes, "overall_pass": overall_pass}
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    # 5. Exit code.
    return 0 if overall_pass else 5


if __name__ == "__main__":
    sys.exit(main())
