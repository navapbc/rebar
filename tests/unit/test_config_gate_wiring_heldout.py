"""Held-out red-proofs for the config-gate wiring parity (RP-04 S7.2, ticket 735b).

WITHHELD from the implementation subagent. These prove the parity assertions in
test_ci_workflow_parity.py have TEETH: fed synthetic edited copies of the Makefile / the
reusable workflow, the same invariant must FLIP (detect the removal / the double-run),
so a silently dropped gate invocation cannot pass unnoticed.

Assertions target observable text-invariants of the shipped config files only.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"
_BAT_YML = _ROOT / ".github" / "workflows" / "_build-and-test.yml"
_OWNERSHIP = "scripts/check_config_ownership.py"
_READS = "scripts/check_config_reads.py"


def _lint_target_body(makefile_text: str) -> str:
    lines = makefile_text.splitlines()
    body: list[str] = []
    in_target = False
    for line in lines:
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    return "\n".join(body)


def _lint_wires(makefile_text: str, script: str) -> bool:
    return script in _lint_target_body(makefile_text)


def _strip_line_containing(text: str, needle: str) -> str:
    return "\n".join(line for line in text.splitlines() if needle not in line)


# --- baseline: the real tree currently satisfies the invariant ------------------------- #


def test_baseline_makefile_wires_both_gates() -> None:
    mk = _MAKEFILE.read_text(encoding="utf-8")
    assert _lint_wires(mk, _OWNERSHIP)
    assert _lint_wires(mk, _READS)


# --- teeth: removing an invocation from a synthetic Makefile is detected --------------- #


def test_parity_detects_removed_ownership_gate() -> None:
    mk = _MAKEFILE.read_text(encoding="utf-8")
    edited = _strip_line_containing(mk, _OWNERSHIP)
    assert not _lint_wires(edited, _OWNERSHIP), (
        "the parity invariant did not notice the ownership gate vanish from `make lint`"
    )
    # the OTHER gate must still register — the check is per-gate, not all-or-nothing.
    assert _lint_wires(edited, _READS)


def test_parity_detects_removed_field_consumption_gate() -> None:
    mk = _MAKEFILE.read_text(encoding="utf-8")
    edited = _strip_line_containing(mk, _READS)
    assert not _lint_wires(edited, _READS), (
        "the parity invariant did not notice the field-consumption gate vanish from `make lint`"
    )
    assert _lint_wires(edited, _OWNERSHIP)


def test_removing_a_gate_line_only_affects_the_lint_target() -> None:
    # A gate invocation placed in some other target must NOT satisfy the lint-target check.
    mk = _MAKEFILE.read_text(encoding="utf-8")
    body = _lint_target_body(mk)
    forged = mk.replace(body, _strip_line_containing(body, _OWNERSHIP))
    forged += f"\nsomeothertarget:\n\tpython {_OWNERSHIP}\n"
    assert not _lint_wires(forged, _OWNERSHIP)


# --- teeth: CI must keep inheriting the gates via `make lint` --------------------------- #


def test_ci_loses_the_gates_if_make_lint_is_dropped() -> None:
    bat = _BAT_YML.read_text(encoding="utf-8")
    assert "make lint" in bat  # baseline
    edited = _strip_line_containing(bat, "make lint")
    assert "make lint" not in edited, (
        "dropping `make lint` from the reusable workflow must be detectable — that is how CI "
        "inherits both config gates"
    )


# --- teeth: a re-introduced standalone step (double-run) is detectable ------------------ #


def test_reintroduced_standalone_config_read_step_is_detectable() -> None:
    bat = _BAT_YML.read_text(encoding="utf-8")
    assert _READS not in bat  # baseline: it moved to `make lint`
    forged = bat + f"\n      - name: Config-read gate\n        run: python {_READS}\n"
    assert _READS in forged, (
        "a re-introduced standalone field-consumption step (which would double-run the gate) "
        "must be detectable by the same invariant"
    )
