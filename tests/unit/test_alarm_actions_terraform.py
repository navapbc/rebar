"""Every CloudWatch alarm in ``infra/terraform/`` must notify somebody (ticket 9baf).

Two alarms — ``voter_errors`` (``monitoring_s4b.tf``) and ``replication_errors``
(``monitoring_s5.tf``) — shipped with NO ``alarm_actions``, so they transitioned
``OK -> ALARM`` and told nobody. Eleven of the repo's thirteen alarms wire the shared
``aws_sns_topic.alerts``; those two were the outliers. ``monitoring.tf`` names this exact
condition a "silent-alarm gap".

Two instances that both survived human review means the convention was not checkable, so this
is the check: parse EVERY ``aws_cloudwatch_metric_alarm`` block under ``infra/terraform/`` and
fail on any that does not assign ``alarm_actions``.

An alarm that legitimately should not notify must say so with an explicit marker comment
inside its block::

    # rebar:allow-actionless-alarm: <reason>

Silence never passes. This is an offline text-contract test on the committed IaC, following
``tests/unit/test_mirror_lock_terraform.py``; live confirmation is an apply-time observation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TF_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"

# The repo had 13 alarms when this guard was written. The floor exists so that a parser that
# silently matches NOTHING — after an HCL reformat, a directory move, or a regex slip — fails
# loudly instead of passing vacuously. A vacuous guard is the failure mode this guard exists
# to prevent, so it must not be able to fall to it itself. Raise this floor when alarms are
# added; never lower it without deleting alarms.
_MIN_EXPECTED_ALARMS = 13

_ALARM_RE = re.compile(
    r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(?P<name>[^"]+)"\s*\{',
)
_ACTIONS_ASSIGNMENT_RE = re.compile(r"^[ \t]*alarm_actions[ \t]*=", re.MULTILINE)
_OK_ACTIONS_ASSIGNMENT_RE = re.compile(r"^[ \t]*ok_actions[ \t]*=", re.MULTILINE)
_OPT_OUT_RE = re.compile(r"#\s*rebar:allow-actionless-alarm:\s*\S+")


def _mask_noncode(src: str) -> str:
    """Return ``src`` with comments, string bodies, and heredoc bodies blanked to spaces.

    Length and index alignment are preserved, so offsets into the mask are valid offsets into
    the original. Masking serves two purposes: brace counting cannot be thrown off by a brace
    inside a comment or an ``alarm_description`` heredoc, and an ``alarm_actions`` mention in
    PROSE cannot be mistaken for an assignment (``monitoring_eb6e.tf`` discusses ``ok_actions``
    in a comment, and ``monitoring.tf`` discusses an "EMPTY alarm_actions list").
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        # line comments: # ... and // ...
        if ch == "#" or (ch == "/" and i + 1 < n and src[i + 1] == "/"):
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        # block comments: /* ... */
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            while i < n and src[i] != "/":
                out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue
        # heredocs: <<EOT / <<-EOT ... terminator line
        heredoc = re.match(r"<<-?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)", src[i:])
        if heredoc:
            tag = heredoc.group("tag")
            j = src.find("\n", i)
            if j == -1:
                break
            for k in range(i, j):
                out[k] = " "
            i = j + 1
            term = re.compile(rf"^[ \t]*{re.escape(tag)}[ \t]*$", re.MULTILINE)
            m = term.search(src, i)
            end = m.end() if m else n
            for k in range(i, end):
                if src[k] != "\n":
                    out[k] = " "
            i = end
            continue
        # quoted strings
        if ch == '"':
            out[i] = " "
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    i += 1
                if i < n:
                    if src[i] != "\n":
                        out[i] = " "
                    i += 1
            if i < n:
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _alarm_blocks() -> list[tuple[str, str, str, str]]:
    """Every alarm block as ``(file_name, alarm_label, raw_text, masked_text)``."""
    blocks: list[tuple[str, str, str, str]] = []
    for tf_file in sorted(_TF_DIR.glob("*.tf")):
        src = tf_file.read_text()
        masked = _mask_noncode(src)
        assert len(masked) == len(src), f"mask desynchronised for {tf_file.name}"
        # Match the resource header on the RAW source (masking blanks quoted strings, so the
        # type/label would be unmatchable there) and brace-count on the MASK (so a brace in a
        # comment or heredoc cannot truncate the block). Indices are aligned by construction.
        for match in _ALARM_RE.finditer(src):
            start = match.end() - 1  # at the opening brace
            depth, idx = 0, start
            while idx < len(masked):
                if masked[idx] == "{":
                    depth += 1
                elif masked[idx] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                idx += 1
            assert depth == 0, (
                f"unbalanced braces parsing alarm {match.group('name')!r} in {tf_file.name}"
            )
            end = idx + 1
            blocks.append((tf_file.name, match.group("name"), src[start:end], masked[start:end]))
    return blocks


def test_parser_actually_finds_the_alarms() -> None:
    """ANTI-VACUITY: the guard below is meaningless if the parser matches nothing.

    This asserts the parser sees the alarms that exist, so a silent parse of zero blocks
    fails here rather than letting :func:`test_every_alarm_declares_alarm_actions` pass
    vacuously forever.
    """
    assert _TF_DIR.is_dir(), f"terraform directory not found: {_TF_DIR}"
    blocks = _alarm_blocks()
    assert len(blocks) >= _MIN_EXPECTED_ALARMS, (
        f"parsed only {len(blocks)} aws_cloudwatch_metric_alarm blocks under {_TF_DIR}, "
        f"expected at least {_MIN_EXPECTED_ALARMS}. Either alarms were deleted or the parser "
        f"stopped matching — the actions guard is vacuous until this is resolved."
    )
    # the block text really is a resource body, not an empty slice
    for file_name, label, raw, _masked in blocks:
        assert raw.startswith("{") and raw.endswith("}"), f"{file_name}:{label} sliced wrong"
        assert "alarm_name" in raw, f"{file_name}:{label} does not look like an alarm body"


def test_every_alarm_declares_alarm_actions() -> None:
    """No alarm may fire into an empty room without an explicit, documented opt-out."""
    offenders: list[str] = []
    for file_name, label, raw, masked in _alarm_blocks():
        if _ACTIONS_ASSIGNMENT_RE.search(masked):
            continue
        if _OPT_OUT_RE.search(raw):
            continue  # deliberately silent, and says why
        offenders.append(f"{file_name}:{label}")
    assert not offenders, (
        "CloudWatch alarm(s) declare no alarm_actions, so they transition OK -> ALARM and "
        "notify nobody: " + ", ".join(offenders) + ". Wire them to the shared topic with "
        "`alarm_actions = [aws_sns_topic.alerts.arn]` (the convention in monitoring.tf, "
        "monitoring_autodeploy.tf, and 9 others), or, if the alarm genuinely must stay "
        "silent, add `# rebar:allow-actionless-alarm: <reason>` inside its resource block."
    )


def test_every_alarm_declares_ok_actions() -> None:
    """A recovery must be as visible as the failure, or the alarm reads as stuck-firing.

    Same opt-out marker applies: an alarm allowed to be silent is allowed to be silent on
    both edges.
    """
    offenders: list[str] = []
    for file_name, label, raw, masked in _alarm_blocks():
        if _OK_ACTIONS_ASSIGNMENT_RE.search(masked):
            continue
        if _OPT_OUT_RE.search(raw):
            continue
        offenders.append(f"{file_name}:{label}")
    assert not offenders, (
        "CloudWatch alarm(s) declare no ok_actions, so a recovery is never announced and the "
        "alarm reads as permanently firing: " + ", ".join(offenders) + ". Add "
        "`ok_actions = [aws_sns_topic.alerts.arn]` alongside alarm_actions."
    )


def test_masking_ignores_prose_mentions_of_alarm_actions() -> None:
    """The parser must not accept a COMMENT about alarm_actions as an assignment.

    ``monitoring.tf`` contains the prose "created out-of-band with an EMPTY alarm_actions
    list"; if masking regressed, that sentence would satisfy the guard for its own file and
    silently excuse a real omission.
    """
    src = 'resource "aws_cloudwatch_metric_alarm" "x" {\n  # alarm_actions = [nope]\n}\n'
    masked = _mask_noncode(src)
    assert not _ACTIONS_ASSIGNMENT_RE.search(masked)
    # and a real assignment still registers
    real = 'resource "aws_cloudwatch_metric_alarm" "x" {\n  alarm_actions = [a]\n}\n'
    assert _ACTIONS_ASSIGNMENT_RE.search(_mask_noncode(real))


def test_opt_out_marker_requires_a_reason() -> None:
    """A bare marker with no reason must not silence the guard."""
    assert not _OPT_OUT_RE.search("# rebar:allow-actionless-alarm:")
    assert _OPT_OUT_RE.search("# rebar:allow-actionless-alarm: dashboard-only, see ticket X")
