"""Structural registry-coverage test for applier._LEAVES.

Iterates every (direction, action) entry in the _LEAVES dispatch table and
asserts each leaf has a real body. The test fails today (5 of 12 leaves are
no-op stubs returning ``ApplyResult(direction, action, {})``); it MUST pass
once story bd19-d744-b8c7-4079 is implemented.

A leaf "has a real body" iff EITHER:
  * the function's source contains ≥10 effective code lines
    (non-blank, non-comment), OR
  * the source contains a regex match for an external-effect call
    (`client.`, `ticket.`, `.write_text`, `.append`, `subprocess.run`,
    `_call_with_retry`).

The combination is intentional: simple wrapper leaves (e.g. inbound
clean_label that just iterates and calls client.remove_label) pass the regex
arm; meatier leaves pass the line-count arm; pure no-op stubs that just
return an empty ApplyResult fail both.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location(
        "leaves_coverage_applier", APPLIER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["leaves_coverage_applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


_EFFECT_RE = re.compile(
    r"client\.|ticket\.|\.write_text|\.append|subprocess\.run|_call_with_retry"
)


def _effective_line_count(src: str) -> int:
    """Count non-blank, non-comment lines in *src*, excluding the def header and docstring."""
    lines = src.splitlines()
    # Drop def line itself.
    if lines and lines[0].lstrip().startswith("def "):
        lines = lines[1:]
    # Drop module/function docstring block if first non-blank line is a triple-quote.
    body = []
    in_doc = False
    doc_delim = None
    for raw in lines:
        line = raw.strip()
        if in_doc:
            if doc_delim and doc_delim in line:
                in_doc = False
            # Skip docstring lines entirely.
            continue
        if not body and (line.startswith('"""') or line.startswith("'''")):
            doc_delim = line[:3]
            # Single-line docstring?
            if len(line) > 3 and line.endswith(doc_delim) and line != doc_delim:
                continue
            in_doc = True
            continue
        body.append(line)
    effective = 0
    for line in body:
        if not line:
            continue
        if line.startswith("#"):
            continue
        effective += 1
    return effective


def test_every_leaf_has_real_body(applier):
    """Every entry in _LEAVES must point at a non-stub function body."""
    leaves = applier._LEAVES
    assert leaves, "_LEAVES registry is empty"

    failures: list[str] = []
    for key, fn in leaves.items():
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError) as exc:
            failures.append(f"{key}: source unavailable ({exc})")
            continue
        eff_lines = _effective_line_count(src)
        has_effect = bool(_EFFECT_RE.search(src))
        if eff_lines < 10 and not has_effect:
            failures.append(
                f"{key} -> {fn.__name__}: "
                f"effective_lines={eff_lines}, has_effect_call={has_effect} — "
                "appears to be a no-op stub"
            )

    if failures:
        msg = "Leaves with no-op stub bodies:\n  " + "\n  ".join(failures)
        pytest.fail(msg)
