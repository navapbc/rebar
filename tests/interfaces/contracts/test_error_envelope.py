"""Every command failure under ``--output json`` emits a schema-valid error_envelope.

Sub-effort (b) of story fatty-cipher-range / ticket large-comet-mica.

Contract: when ``--output json`` is requested and a command fails for a real
error (ticket-not-found, bad input, optimistic-concurrency, …), stdout carries a
machine-readable ``error_envelope`` (``{error, input, message[, exit_code]}``) so
agents never parse stderr prose. Text mode is unchanged: the envelope is emitted
ONLY in json mode, so a report-profile command's text-mode stdout stays empty.

Out of scope (documented):
  * the per-ticket GATES (``check-ac``/``quality-check``) emit a gate verdict, not
    an error, for a failing ticket — exit 1 is a verdict, not a failure;
  * ``clarity-check`` does NOT participate in the ``--output`` flag system at all
    (it has its own always-JSON contract, docs/contracts/ticket-clarity-check-output.md),
    so it is outside this ``--output json`` contract;
  * the TOLERANT reads — ``summary``/``list-descendants`` render an empty/placeholder
    result at exit 0; ``get-file-impact <missing>`` returns ``[]`` at exit 0;
    ``scratch get <missing>`` returns ``{status: miss}`` at exit 0; ``list-epics``
    emits its canonical empty shape — none of those are failures.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import schemas

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")

MISSING = "zzzz-zzzz-zzzz-0000"


def _cli(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


# Each case: (id, failing-args-without-output-flag, profile_default)
# profile_default "text" => a report command whose text-mode stdout must stay
# envelope-free; "json" => a reader command (show/deps) that defaults to json.
CASES = [
    ("show_missing", ("show", MISSING), "json"),
    ("deps_missing", ("deps", MISSING), "json"),
    ("get_verify_commands_missing", ("get-verify-commands", MISSING), "text"),
    ("next_batch_missing", ("next-batch", MISSING), "text"),
    ("create_bad_type", ("create", "notatype", "x"), "text"),
    ("claim_missing", ("claim", MISSING), "text"),
    ("transition_missing", ("transition", MISSING, "open", "closed"), "text"),
    ("reopen_missing", ("reopen", MISSING), "text"),
    ("delete_missing", ("delete", MISSING, "--user-approved"), "text"),
]
IDS = [c[0] for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_failure_emits_error_envelope_json(case, rebar_repo: Path) -> None:
    _id, args, default = case
    r = str(rebar_repo)
    # Reader commands (show/deps) default to json; report commands need the flag.
    full = args if default == "json" else (*args, "--output", "json")
    cp = _cli(*full, cwd=r)
    assert cp.returncode != 0, f"{_id}: expected a non-zero exit on failure"
    # stdout must be a single JSON object validating against error_envelope.
    try:
        payload = json.loads(cp.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"{_id}: --output json failure emitted non-JSON stdout: {cp.stdout!r}")
    schemas.validator(schemas.ERROR_ENVELOPE).validate(payload)
    # The emitted envelope carries the exit_code (new optional field).
    assert payload.get("exit_code") == cp.returncode, (
        f"{_id}: envelope exit_code {payload.get('exit_code')} != process rc {cp.returncode}"
    )


@pytest.mark.parametrize(
    "case", [c for c in CASES if c[2] == "text"], ids=[c[0] for c in CASES if c[2] == "text"]
)
def test_text_mode_failure_stdout_stays_empty(case, rebar_repo: Path) -> None:
    """Report-profile commands: a failure WITHOUT --output json must not leak an
    envelope to stdout (text-mode byte-identical; prose stays on stderr)."""
    _id, args, _default = case
    cp = _cli(*args, cwd=str(rebar_repo))
    assert cp.returncode != 0
    assert cp.stdout.strip() == "", f"{_id}: text-mode failure leaked stdout: {cp.stdout!r}"
