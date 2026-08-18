"""A shrinking journal must not republish already-counted markers (bug 2dc7-31b7-ecbb-4cd2).

Every cumulative marker counter in ``observability.sh`` publishes ``total - prev``. When
journald vacuums (``SystemMaxUse``/``MaxRetentionSec``/``--vacuum-*``) the journal LOSES
history, ``total`` drops below the persisted offset, and the delta goes negative. The old
guard republished the whole remaining ``total`` as "new this interval", double-counting
markers that had already been counted and already alarmed on — a false page on the
1-datapoint ``review_interrupts`` alarm, pointing at journal history that was just deleted.

These tests drive the real script with an offset seeded ABOVE the marker count (the
shrinking-journal simulation) and assert the published value is 0, and that the counter
re-bases to the smaller journal so genuinely-new markers still publish afterwards.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
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
        for _ in {{1..{_MARKERS_IN_JOURNAL}}}; do
          printf '%s\\n' 'VOTER_ERROR {{"ts": 1}}' 'MERGE_CHANGE_ERROR {{"ts": 1}}'
          printf '%s\\n' 'AUTODEPLOY_ERROR {{"ts": 1}}' 'AUTODEPLOY_DEFERRED {{"ts": 1}}'
          printf '%s\\n' 'AUTODEPLOY_REVIEW_INTERRUPT {{"ts": 1}}'
          printf '%s\\n' 'gerrit_to_platform error'
        done
        """,
    )
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    paths: dict[str, Path] = {}
    for _metric, variable in _TARGETS:
        path = offsets / variable.lower()
        path.write_text(f"{seeded_offset}\n")
        paths[variable] = path
    repl_log = tmp_path / "replication.log"
    repl_log.write_text("[ERROR]\n" * _MARKERS_IN_JOURNAL)

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
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
    # Offset far above the journal's marker count: the journal lost history it once had.
    env, paths = _environment(tmp_path, seeded_offset=99)

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert result.returncode == 0
    # 0, NOT _MARKERS_IN_JOURNAL — the survivors were already counted under the old offset.
    assert _values(paths["aws_log"], target) == [0]
    # Re-based to the shrunken journal, so the suppression lasts exactly one interval.
    assert paths[offset_variable].read_text().strip() == str(_MARKERS_IN_JOURNAL)


def test_markers_after_a_rotation_still_publish(tmp_path: Path) -> None:
    """Suppression must not be sticky: new markers after the reset are counted normally."""
    env, paths = _environment(tmp_path, seeded_offset=99)

    subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    # The journal is unchanged between runs, so run two sees a genuine zero delta; drop the
    # offset by one to stand in for a single marker arriving after the rotation re-based it.
    paths["INTERRUPT_OFFSET_FILE"].write_text(f"{_MARKERS_IN_JOURNAL - 1}\n")
    second = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert second.returncode == 0
    assert _values(paths["aws_log"], "review_interrupts") == [0, 1]
