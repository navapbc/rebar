"""Every CloudWatch alarm in ``infra/terraform/`` must notify somebody (ticket 9baf).

Two alarms — ``voter_errors`` (``monitoring_s4b.tf``) and ``replication_errors``
(``monitoring_s5.tf``) — shipped with NO ``alarm_actions``, so they transitioned
``OK -> ALARM`` and told nobody. Eleven of the repo's thirteen alarms wire the shared
``aws_sns_topic.alerts``; those two were the outliers. ``monitoring.tf`` names this exact
condition a "silent-alarm gap".

Two instances that both survived human review means the convention was not checkable, so this
is the check: parse EVERY ``aws_cloudwatch_metric_alarm`` block under ``infra/terraform/`` and
fail on any that does not assign ``alarm_actions``. The same guard also asserts that
host-published disk alarms treat missing data as breaching, so a dead publisher pages.

An alarm that legitimately should not notify must say so with an explicit marker comment
inside its block::

    # rebar:allow-actionless-alarm: <reason>

Silence never passes.

The same file also guards the DEAD-PUBLISHER case (ticket bff5-9163-cddd-4158). Every
``rebar/host`` metric is an offset-delta published unconditionally by
``infra/scripts/observability.sh`` on a 5-minute timer: a healthy period publishes ``0``, not
nothing. So ``treat_missing_data = "notBreaching"`` on such an alarm does not mean "quiet when
healthy" — it means "quiet when the publisher is DEAD", which is the one state the alarm most
needs to announce. Every alarm in the ``rebar/host`` namespace must therefore treat missing
data as breaching, unless its block carries::

    # rebar:allow-missing-data-notbreaching: <reason>

Both markers require a non-empty reason; a bare marker is an error, not an opt-out.

This is an offline text-contract test on the committed IaC, following
``tests/unit/test_mirror_lock_terraform.py``; live confirmation is an apply-time observation.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TF_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"

# The repo had 13 alarms when this guard was written. The floor exists so that a parser that
# silently matches NOTHING — after an HCL reformat, a directory move, or a regex slip — fails
# loudly instead of passing vacuously. A vacuous guard is the failure mode this guard exists
# to prevent, so it must not be able to fall to it itself. Raise this floor when alarms are
# added; never lower it without deleting alarms.
_MIN_EXPECTED_ALARMS = 24

_ALARM_RE = re.compile(
    r'resource\s+"aws_cloudwatch_metric_alarm"\s+"(?P<name>[^"]+)"\s*\{',
)
_ACTIONS_ASSIGNMENT_RE = re.compile(r"^[ \t]*alarm_actions[ \t]*=", re.MULTILINE)
_OK_ACTIONS_ASSIGNMENT_RE = re.compile(r"^[ \t]*ok_actions[ \t]*=", re.MULTILINE)
_TREAT_MISSING_DATA_RE = re.compile(
    r'^[ \t]*treat_missing_data[ \t]*=[ \t]*"(?P<value>[^"]+)"', re.MULTILINE
)
_OPT_OUT_RE = re.compile(r"#\s*rebar:allow-actionless-alarm:\s*\S+")
_MISSING_DATA_OPT_OUT_RE = re.compile(r"#\s*rebar:allow-missing-data-notbreaching:\s*\S+")

# Host-published alarms had 13 rebar/host blocks when this guard was written. Same
# anti-vacuity role as _MIN_EXPECTED_ALARMS: a scope filter that silently matches nothing
# makes the guard below pass for free.
_MIN_EXPECTED_HOST_ALARMS = 19


def _quoted_attr(raw: str, masked: str, attr: str) -> str | None:
    """Value of a top-level quoted attribute, ignoring any mention of it in a comment.

    The assignment is located on the MASK (so a commented-out or prose ``namespace =`` line
    cannot match) and the value is read back from the RAW text at the same offset (masking
    blanks string bodies, so the value is unreadable there). Indices are aligned by
    construction — see :func:`_mask_noncode`.
    """
    # Stop the mask-side match AT the ``=``: everything after it is blanked in the mask, so a
    # greedy trailing ``[ \t]*`` would run past the opening quote in the aligned raw text.
    assign = re.compile(rf"^[ \t]*{re.escape(attr)}[ \t]*=", re.MULTILINE)
    for hit in assign.finditer(masked):
        value = re.match(r'[ \t]*"([^"]*)"', raw[hit.end() :])
        if value is not None:
            return value.group(1)
    return None


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


def test_host_published_disk_alarms_treat_missing_data_as_breaching() -> None:
    """A stopped host-published disk metric must page instead of clearing healthy."""
    watched = {
        ("monitoring.tf", "gerrit_data_disk_high"),
        ("monitoring_autodeploy.tf", "root_disk_pressure"),
        # The gate-scratch pair (story aa40-cbda-ee38-481c). ADR 0112 records the
        # missing-data property as an obligation carried by the ADR rather than by any one
        # story, and asks for a test that PINS it instead of trusting the copy-paste — this
        # named set is that pin, so a later edit to either alarm fails here by name.
        ("monitoring_autodeploy.tf", "gate_scratch_disk_high"),
        ("monitoring_autodeploy.tf", "gate_scratch_unmounted"),
        # The Docker generator trio (story 9183-aaae-667d-45e6). Same ADR 0112 obligation,
        # and the same reason to PIN rather than trust the copy-paste — but with one extra
        # edge these three have and the others do not: observability.sh 2f publishes them
        # ONLY on a successful measurement, so their missing-data state is a real, reachable
        # runtime condition (a `du` that could not run, a wedged docker daemon) rather than
        # only the dead-host case. `notBreaching` here would render exactly "the probe can no
        # longer see the disk" as health.
        ("monitoring_autodeploy.tf", "docker_storage_cap_high"),
        ("monitoring_autodeploy.tf", "docker_buildkit_cache_high"),
        ("monitoring_autodeploy.tf", "docker_unaccounted_bytes"),
    }
    found: set[tuple[str, str]] = set()
    offenders: list[str] = []

    for file_name, label, raw, _masked in _alarm_blocks():
        key = (file_name, label)
        if key not in watched:
            continue
        found.add(key)
        match = _TREAT_MISSING_DATA_RE.search(raw)
        if match is None or match.group("value") != "breaching":
            offenders.append(
                f"{file_name}:{label} treats missing data as "
                f"{match.group('value') if match else 'unset'}"
            )

    assert found == watched
    assert not offenders, (
        "Disk metrics are published by the host under disk pressure; if the host or "
        "publisher dies, missing data must enter ALARM and invoke alarm_actions, not "
        "clear as OK: " + ", ".join(offenders)
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


def _host_alarm_blocks() -> list[tuple[str, str, str, str]]:
    """Alarm blocks whose top-level ``namespace`` is the host-published ``rebar/host``.

    Scoped deliberately: ``monitoring_eb6e.tf``'s Bedrock alarm reads an AWS-PUBLISHED metric
    that is genuinely absent when nothing invokes the model, so notBreaching is correct there.
    It declares its namespaces inside nested ``metric`` blocks and none at the top level, so it
    is excluded by construction rather than by an allowlist.
    """
    return [
        block
        for block in _alarm_blocks()
        if _quoted_attr(block[2], block[3], "namespace") == "rebar/host"
    ]


def test_parser_actually_finds_the_host_alarms() -> None:
    """ANTI-VACUITY for the dead-publisher guard: the rebar/host filter must select alarms."""
    host = _host_alarm_blocks()
    assert len(host) >= _MIN_EXPECTED_HOST_ALARMS, (
        f"selected only {len(host)} alarms in the rebar/host namespace, expected at least "
        f"{_MIN_EXPECTED_HOST_ALARMS}. Either alarms were deleted or the namespace filter "
        f"stopped matching — the missing-data guard is vacuous until this is resolved."
    )


def test_host_alarms_treat_missing_data_as_breaching() -> None:
    """A dead publisher must page, not read as health (ticket bff5-9163-cddd-4158).

    Every ``rebar/host`` counter is an offset-delta that ``infra/scripts/observability.sh``
    publishes UNCONDITIONALLY on its 5-minute timer — the healthy path publishes ``0``, so the
    metric is continuously present. Missing data therefore does not mean "nothing happened";
    it means the probe, the timer, or the host is dead. ``notBreaching`` renders exactly that
    outage as OK.
    """
    offenders: list[str] = []
    for file_name, label, raw, masked in _host_alarm_blocks():
        if _MISSING_DATA_OPT_OUT_RE.search(raw):
            continue  # deliberately fails open, and says why
        value = _quoted_attr(raw, masked, "treat_missing_data")
        if value != "breaching":
            offenders.append(f"{file_name}:{label} treats missing data as {value or 'unset'}")
    assert not offenders, (
        "rebar/host alarm(s) treat missing data as healthy, so a dead publisher (probe crash, "
        "stopped timer, dead host) silently clears them to OK: " + ", ".join(offenders) + ". "
        "The healthy path publishes 0 (observability.sh publishes every counter delta "
        'unconditionally), so set `treat_missing_data = "breaching"` — and widen the alarm to '
        "evaluation_periods >= 3 so ordinary timer jitter does not flap it. If an alarm "
        "genuinely must fail open, add `# rebar:allow-missing-data-notbreaching: <reason>` "
        "inside its resource block naming what else covers the dead-publisher case."
    )


def _int_attr(masked: str, name: str) -> int | None:
    """Read an unquoted integer attribute, or ``None`` when the block does not set it."""
    match = re.search(rf"^[ \t]*{name}[ \t]*=[ \t]*(\d+)", masked, re.MULTILINE)
    return int(match.group(1)) if match else None


def test_breaching_host_alarms_are_wide_enough_not_to_flap() -> None:
    """``breaching`` on a single datapoint turns ordinary timer jitter into a page.

    A live 2-hour sample of the 5-minute probe showed ~22 of 24 periods present: isolated
    missing periods are NORMAL. With ``breaching``, each one is a breaching datapoint, so
    BOTH halves of the window have to be wide enough. ``evaluation_periods >= 3`` alone is
    not: ``3 / datapoints_to_alarm = 1`` still latches on the first jittered interval, and it
    is the M-of-N count, not the window length, that decides. Require ``>= 2`` of both, the
    ``root_disk_pressure`` 300/3/2 shape.

    An UNSET ``datapoints_to_alarm`` is not a hole: CloudWatch then evaluates N-of-N, so the
    effective count equals ``evaluation_periods`` and is stricter than any explicit value. It
    is read that way here rather than treated as missing.
    """
    offenders: list[str] = []
    for file_name, label, raw, masked in _host_alarm_blocks():
        if _quoted_attr(raw, masked, "treat_missing_data") != "breaching":
            continue
        periods = _int_attr(masked, "evaluation_periods") or 0
        # Unset datapoints_to_alarm means N-of-N in CloudWatch, i.e. the whole window.
        datapoints = _int_attr(masked, "datapoints_to_alarm")
        effective = periods if datapoints is None else datapoints
        if periods < 3 or effective < 2:
            offenders.append(
                f"{file_name}:{label} evaluation_periods={periods or 'unset'} "
                f"datapoints_to_alarm={datapoints if datapoints is not None else 'unset (=N)'}"
            )
    assert not offenders, (
        "alarm(s) treat missing data as breaching but latch on too narrow a window, so a "
        "single jittered probe interval pages: " + ", ".join(offenders) + ". Widen to "
        "evaluation_periods >= 3 AND datapoints_to_alarm >= 2, matching root_disk_pressure's "
        "period=300 / evaluation_periods=3 / datapoints_to_alarm=2."
    )


def test_the_flap_guard_rejects_a_single_datapoint_latch() -> None:
    """ANTI-VACUITY: the guard's own arithmetic must reject 3/1 and accept an unset 3/N."""
    assert _int_attr("  datapoints_to_alarm = 1\n", "datapoints_to_alarm") == 1
    assert _int_attr("  evaluation_periods = 3\n", "datapoints_to_alarm") is None


def test_missing_data_opt_out_marker_requires_a_reason() -> None:
    """A bare missing-data marker with no reason must not silence the guard."""
    assert not _MISSING_DATA_OPT_OUT_RE.search("# rebar:allow-missing-data-notbreaching:")
    assert _MISSING_DATA_OPT_OUT_RE.search(
        "# rebar:allow-missing-data-notbreaching: the heartbeat canary owns the dead case"
    )


def test_quoted_attr_ignores_commented_assignments() -> None:
    """A commented-out attribute must not be read as the block's real value."""
    raw = (
        'resource "aws_cloudwatch_metric_alarm" "x" {\n'
        '  # namespace = "AWS/Bedrock"\n'
        '  namespace = "rebar/host"\n'
        "}\n"
    )
    assert _quoted_attr(raw, _mask_noncode(raw), "namespace") == "rebar/host"


# AWS rejects an alarm whose description exceeds 1024 characters, but it does so at
# APPLY time against the live API — `terraform validate` and the HCL-shape guards above
# both pass, and so does CI, which never plans against AWS. Change 2565 landed an alarm
# with a description of 1236 raw / 1180 after `<<-` dedent -- over the cap either way,
# so unappliable. It reached main through LLM-Review and a green Verified vote, and
# surfaced only when an operator ran `terraform plan` (bug 9ea3-7d07-ea55-4496).
_AWS_ALARM_DESCRIPTION_MAX = 1024

_HEREDOC_DESCRIPTION_RE = re.compile(
    r"alarm_description\s*=\s*<<(?P<squash>-?)(?P<tag>\w+)\n(?P<body>.*?)\n\s*(?P=tag)",
    re.DOTALL,
)

#: The QUOTED form is not hypothetical — monitoring.tf uses it three times
#: (the two EC2 status-check alarms and gerrit_data_disk_high). A guard that
#: matched only the heredoc form would silently skip exactly those.
_QUOTED_DESCRIPTION_RE = re.compile(r'alarm_description\s*=\s*"(?P<body>(?:[^"\\]|\\.)*)"')


def _rendered_description(match: re.Match[str]) -> str:
    """The string terraform actually sends to AWS.

    ``<<-`` strips the common leading indentation, so measuring the RAW heredoc body
    over-counts by the indent on every line. Verified against the live alarm
    ``rebar-bedrock-invoke-client-errors``: 1037 raw, 981 dedented, and AWS reports 982.
    Counting raw would have failed this valid alarm.
    """
    body = match.group("body")
    return textwrap.dedent(body) if match.group("squash") else body


def test_alarm_descriptions_fit_the_aws_limit() -> None:
    """Every heredoc alarm_description stays under AWS's 1024-character cap.

    Without this, an over-long description is only caught by a live `terraform plan`,
    i.e. after the change has already merged.
    """
    too_long: list[str] = []
    measured = 0
    for file_name, label, raw, _masked in _alarm_blocks():
        heredoc = _HEREDOC_DESCRIPTION_RE.search(raw)
        quoted = _QUOTED_DESCRIPTION_RE.search(raw)
        if heredoc is not None:
            rendered = _rendered_description(heredoc)
        elif quoted is not None:
            rendered = quoted.group("body")
        else:
            continue
        measured += 1
        if len(rendered) > _AWS_ALARM_DESCRIPTION_MAX:
            too_long.append(f"{file_name}:{label} description is {len(rendered)} chars")

    assert not too_long, (
        "alarm_description exceeds AWS's 1024-character limit, so `terraform apply` "
        "will REJECT these alarms: " + ", ".join(sorted(too_long))
    )
    # Anti-vacuity floor: if the regexes stop matching (an HCL reformat, a regex
    # slip), every block would `continue` and this test would pass having checked
    # nothing — which is how the defect it guards against reached main in the first
    # place. The tree has 18 heredoc and 3 quoted descriptions today.
    assert measured >= 18, (
        f"only {measured} alarm_description values were measured; the regexes have "
        "stopped matching and this guard is passing vacuously"
    )
