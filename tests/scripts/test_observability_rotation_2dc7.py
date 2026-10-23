"""Check journal-cursor recovery after a vacuum discards its saved entry.

Surviving markers must not be republished. The counter reseeds, then publishes
new markers once on the next interval. Recovery must not restart an unbounded
scan from the journal's beginning. The log-file counter retains its separate
bare-total behavior.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from _journald_stub import JOURNALCTL_EMULATOR, TIMEOUT_STUB
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40
_MARKERS_IN_JOURNAL = 7
# (metric published to CloudWatch, env var naming its persisted offset file)
_TARGETS = (
    ("replication_errors", "REPL_OFFSET_FILE"),
    ("voter_errors", "VOTER_OFFSET_FILE"),
    ("review_bot_merge_change_errors", "MERGE_OFFSET_FILE"),
    ("deploy_errors", "DEPLOY_OFFSET_FILE"),
    ("deploy_deferrals", "DEFER_OFFSET_FILE"),
    ("review_interrupts", "INTERRUPT_OFFSET_FILE"),
    ("g2p_dispatch_errors", "G2P_OFFSET_FILE"),
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _environment(tmp_path: Path, seeded_offset: int) -> tuple[dict[str, str], dict[str, Path]]:
    """Stub the box out from under the script and seed every offset file identically."""
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
    _stub(
        bin_dir,
        "journalctl",
        f"""
        if [ ! -s "$JOURNAL_FILE" ]; then
          for _ in {{1..{_MARKERS_IN_JOURNAL}}}; do
            printf '%s\\n' 'VOTER_ERROR {{"ts": 1}}' 'MERGE_CHANGE_ERROR {{"ts": 1}}' \\
              'AUTODEPLOY_ERROR {{"ts": 1}}' 'AUTODEPLOY_DEFERRED {{"ts": 1}}' \\
              'AUTODEPLOY_REVIEW_INTERRUPT {{"ts": 1}}' 'gerrit_to_platform error' \\
              >> "$JOURNAL_FILE"
          done
        fi
        """
        + JOURNALCTL_EMULATOR,
    )
    _stub(bin_dir, "timeout", TIMEOUT_STUB)
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths: dict[str, Path] = {}
    for _metric, variable in _TARGETS:
        path = offsets / variable.lower()
        # The log counter keeps a bare total; journal counters carry a total and
        # a cursor already discarded by the vacuum.
        if variable == "REPL_OFFSET_FILE":
            path.write_text(f"{seeded_offset}\n")
        else:
            path.write_text(f"{seeded_offset} 3\n")
        paths[variable] = path
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("[ERROR]\n" * _MARKERS_IN_JOURNAL)
    # Discard the first 500 entries so the saved cursor cannot be sought.
    base_file = tmp_path / "journal-base"
    base_file.write_text("500\n")

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "JOURNAL_FILE": str(tmp_path / "journal.txt"),
            "JOURNAL_BASE_FILE": str(base_file),
            "REPL_LOG": str(repl_log),
            **{name: str(path) for name, path in paths.items()},
        }
    )
    return env, {"aws_log": aws_log, **paths}


def _values(log: Path, target: str) -> list[int]:
    values: list[int] = []
    for line in log.read_text().splitlines():
        parts = line.split()
        if "--metric-name" not in parts or target not in parts:
            continue
        values.append(int(parts[parts.index("--value") + 1]))
    return values


@pytest.mark.parametrize(("target", "offset_variable"), _TARGETS)
def test_shrinking_journal_publishes_zero_not_the_remaining_total(
    tmp_path: Path, target: str, offset_variable: str
) -> None:
    """Do not re-announce surviving markers after a vacuum."""
    env, paths = _environment(tmp_path, seeded_offset=99)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert result.returncode == 0
    published = _values(paths["aws_log"], target)
    # The log counter publishes 0; journal counters with lost cursors publish
    # nothing because that interval cannot be measured. Never count survivors.
    assert published in ([], [0]), published
    assert _MARKERS_IN_JOURNAL not in published
    if offset_variable != "REPL_OFFSET_FILE":
        # Reseeding prevents a permanent cursor stall.
        total, cursor = paths[offset_variable].read_text().split()
        assert (total, cursor) == ("99", "542")


def test_markers_after_a_rotation_still_publish(tmp_path: Path) -> None:
    """Count new markers after reseeding."""
    env, paths = _environment(tmp_path, seeded_offset=99)

    first = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    # Add one new marker after reseeding.
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + 'AUTODEPLOY_REVIEW_INTERRUPT {"ts": 2}\n')
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert (first.returncode, second.returncode) == (0, 0)
    # The lost-cursor interval is silent; the next counts only the new marker.
    assert _values(paths["aws_log"], "review_interrupts") == [1]
