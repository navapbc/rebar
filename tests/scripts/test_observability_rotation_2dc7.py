"""A vacuumed journal must not republish already-counted markers (bug 2dc7-31b7-ecbb-4cd2).

When journald vacuums (``SystemMaxUse``/``MaxRetentionSec``/``--vacuum-*``) the journal LOSES
history. Under the original design each counter recomputed a cumulative ``total`` by scanning
the whole journal and published ``total - prev``; a vacuum made ``total`` drop below the
persisted offset, the delta went negative, and the guard republished the whole remaining
``total`` as "new this interval" — a false page on the 1-datapoint ``review_interrupts`` alarm,
pointing at journal history that had just been deleted.

Since bug 1205 the total is accumulated forward from a persisted journald CURSOR instead of
recomputed, and a negative delta is no longer REPRESENTABLE: entries that vanish are simply
absent from the next read. What a vacuum can still do is discard the entry a cursor names, and
the recovery from that is what these tests now pin:

- the survivors are never re-announced as new, and
- the suppression is not sticky — the counter reseeds and the next interval publishes normally.

The dangerous recovery, and the one specifically excluded here, is abandoning the cursor and
re-reading the journal from the beginning: that is the unbounded scan whose removal is the
point of bug 1205, and it would fire exactly when the journal is largest.
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
        # replication_errors greps a log FILE and keeps the bare-total form; the journal-backed
        # counters carry `<total> <cursor>`, and the cursor here names an entry the vacuum has
        # already discarded (base below is far above it).
        if variable == "REPL_OFFSET_FILE":
            path.write_text(f"{seeded_offset}\n")
        else:
            path.write_text(f"{seeded_offset} 3\n")
        paths[variable] = path
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("[ERROR]\n" * _MARKERS_IN_JOURNAL)
    # journald has discarded the first 500 entries it ever held, so any cursor below that names
    # an entry it can no longer seek to — which is what a vacuum does to a persisted cursor.
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
    """A vacuumed journal must not re-announce its survivors as new markers."""
    env, paths = _environment(tmp_path, seeded_offset=99)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert result.returncode == 0
    published = _values(paths["aws_log"], target)
    # Never the survivors. The log-file counter still publishes its own 0; a journal counter
    # whose cursor was discarded cannot measure the interval at all and so publishes NOTHING,
    # which is the probe's contract for an unmeasurable reading — a plausible number here would
    # be indistinguishable from a healthy one.
    assert published in ([], [0]), published
    assert _MARKERS_IN_JOURNAL not in published
    if offset_variable != "REPL_OFFSET_FILE":
        # Reseeded, so the discarded cursor is not a permanent stall...
        total, cursor = paths[offset_variable].read_text().split()
        assert (total, cursor) == ("99", "542")


def test_markers_after_a_rotation_still_publish(tmp_path: Path) -> None:
    """Suppression must not be sticky: markers arriving after the reseed are counted normally."""
    env, paths = _environment(tmp_path, seeded_offset=99)

    first = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    # ...and one genuinely new marker lands in the interval after the reseed.
    journal = Path(env["JOURNAL_FILE"])
    journal.write_text(journal.read_text() + 'AUTODEPLOY_REVIEW_INTERRUPT {"ts": 2}\n')
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert (first.returncode, second.returncode) == (0, 0)
    # The vacuumed interval publishes nothing; the next one publishes the new marker and only it.
    assert _values(paths["aws_log"], "review_interrupts") == [1]
