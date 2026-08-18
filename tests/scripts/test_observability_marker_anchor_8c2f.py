"""Marker counters match the emitted RECORD, not the token as prose (bug 8c2f-8377-5044-4650).

``observability.sh`` counted markers with a bare substring grep over the review-bot's journal —
the same stream the bot writes its LLM review output to. On 2026-08-12 a review of
``observability.sh`` itself enumerated the marker names in prose, and the probe counted that one
line as both a voter error and a merge-change error, firing two alarms at 23:07Z against a
review-bot that was voting normally throughout. The merge-change counter had never seen a real
error in the entire retained journal; its only count in history was that false positive.

Every countable marker is emitted as ``<TOKEN> {json}`` at the start of the journal message
(``voter.py`` prints ``"VOTER_ERROR " + json.dumps(record)`` to stderr; ``autodeploy.sh``'s
``marker()`` does ``printf '%s %s\\n' "$TOKEN" "<json>"``). These tests drive the REAL script over
a synthetic journal and assert prose naming a marker counts 0 while a genuine record counts 1.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

# (metric, records the journal must yield per emitted set — review_interrupts matches both reasons)
_MARKER_METRICS = (
    ("voter_errors", 1),
    ("review_bot_merge_change_errors", 1),
    ("deploy_errors", 1),
    ("deploy_deferrals", 1),
    ("review_interrupts", 2),
    ("review_interrupts_bound_exceeded", 1),
    ("review_interrupts_signal_unavailable", 1),
)

_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "INTERRUPT_BOUND_OFFSET_FILE",
    "INTERRUPT_SIGNAL_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)

# The verbatim review-bot LLM output from 2026-08-12T23:02:26Z that fired both alarms. It reviews
# observability.sh, so it names the marker vocabulary as prose inside a sentence.
_REVIEW_PROSE = (
    "...mits matching markers for every target (VOTER_ERROR, MERGE_CHANGE_ERROR, "
    "AUTODEPLOY_ERROR, AUTODEPLOY_DEFERRED, gerrit_to_platform error, both interrupt reasons) "
    "and REPL_LOG is populated. The rolled-up review_interrupts per_interval=2 arithmetic is "
    "consistent (total 14 seeded, next run +2)"
)
# Prose that BEGINS with a marker token but carries no record — the anchor's other half.
_LINE_START_PROSE = (
    "VOTER_ERROR is the marker the probe greps for; AUTODEPLOY_REVIEW_INTERRUPT carries a reason"
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _record(token: str, reason: str = "x") -> str:
    """Reproduce the real emission shape: the token, a space, then the JSON record."""
    payload = json.dumps({"ts": 1786518175, "reason": reason, "detail": "target=x in_flight=2"})
    return f"{token} {payload}"


def _real_markers() -> list[str]:
    return [
        _record("VOTER_ERROR"),
        _record("MERGE_CHANGE_ERROR"),
        _record("AUTODEPLOY_ERROR"),
        _record("AUTODEPLOY_DEFERRED"),
        _record("AUTODEPLOY_REVIEW_INTERRUPT", reason="bound-exceeded"),
        _record("AUTODEPLOY_REVIEW_INTERRUPT", reason="signal-unavailable"),
    ]


def _environment(
    tmp_path: Path, journal_lines: list[str]
) -> tuple[dict[str, str], dict[str, Path]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    _stub(
        bin_dir,
        "curl",
        f"""
        for a in "$@"; do
          case "$a" in
            *projects/rebar/branches/main*)
              printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
          esac
        done
        case "$*" in *http_code*) printf '200'; exit 0 ;; esac
        printf 'dummy-token'; exit 0
        """,
    )
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')
    journal = tmp_path / "journal.txt"
    journal.write_text("".join(f"{line}\n" for line in journal_lines))
    _stub(bin_dir, "journalctl", 'cat "$JOURNAL_FILE"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths = {name: offsets / name.lower() for name in _OFFSET_VARIABLES}
    # Explicit 0: an absent offset is a cold start, which seeds and publishes 0 regardless of the
    # pattern (bug e2a6-9ee4-8d5c-4290) and would mask what these tests measure.
    for path in paths.values():
        path.write_text("0\n")
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("")

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(repl_log),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    return env, {"aws_log": aws_log, "journal": journal, **paths}


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts:
            continue
        if parts[parts.index("--metric-name") + 1] != metric:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)


def test_review_prose_naming_the_markers_is_not_counted(tmp_path: Path) -> None:
    """The verbatim line that fired both alarms must count as nothing at all."""
    env, paths = _environment(tmp_path, [_REVIEW_PROSE])

    result = _run(env)

    assert result.returncode == 0
    for metric, _per_set in _MARKER_METRICS:
        assert _values(paths["aws_log"], metric) == [0], metric


def test_line_start_prose_without_a_record_is_not_counted(tmp_path: Path) -> None:
    """Anchoring alone is not enough: prose can begin with the token, so the record shape counts."""
    env, paths = _environment(tmp_path, [_LINE_START_PROSE])

    result = _run(env)

    assert result.returncode == 0
    assert _values(paths["aws_log"], "voter_errors") == [0]
    assert _values(paths["aws_log"], "review_interrupts") == [0]


def test_real_markers_are_still_counted(tmp_path: Path) -> None:
    """The tightened pattern must not cost a single genuine marker."""
    env, paths = _environment(tmp_path, _real_markers())

    result = _run(env)

    assert result.returncode == 0
    for metric, per_set in _MARKER_METRICS:
        assert _values(paths["aws_log"], metric) == [per_set], metric


def test_real_markers_are_counted_alongside_the_prose(tmp_path: Path) -> None:
    """A journal holding both must publish exactly the real ones — the 221-vs-222 case."""
    env, paths = _environment(tmp_path, [_REVIEW_PROSE, *_real_markers(), _LINE_START_PROSE])

    result = _run(env)

    assert result.returncode == 0
    for metric, per_set in _MARKER_METRICS:
        assert _values(paths["aws_log"], metric) == [per_set], metric


def test_offsets_inflated_by_false_counts_self_heal(tmp_path: Path) -> None:
    """The banked false counts need no manual correction on the box.

    The live offsets are inflated by the false positives (voter 222 against 221 real markers,
    merge-change 1 against 0). Once the pattern tightens, the total drops BELOW the offset — a
    negative delta, which the lost-history clamp (bug 2dc7-31b7-ecbb-4cd2) publishes as 0 while
    re-basing the offset to the true count. One probe run, no box action.
    """
    env, paths = _environment(tmp_path, [_REVIEW_PROSE, _record("VOTER_ERROR")])
    paths["VOTER_OFFSET_FILE"].write_text("2\n")  # counted the prose line as a second error
    paths["MERGE_OFFSET_FILE"].write_text("1\n")  # its only count in history was the prose

    result = _run(env)

    assert result.returncode == 0
    assert _values(paths["aws_log"], "voter_errors") == [0]
    assert _values(paths["aws_log"], "review_bot_merge_change_errors") == [0]
    assert paths["VOTER_OFFSET_FILE"].read_text().strip() == "1"
    assert paths["MERGE_OFFSET_FILE"].read_text().strip() == "0"
