"""Non-`site/` debris on the Gerrit DATA volume is censused and published (task 3e92).

The `/var/gerrit` disk-fill incident (bug 5a05-0e52-33a0-48bf, wakeful-ordinary-chicken)
was 65% one-off investigation evidence: two
`epoch-probe-*` dumps of ~5.2G each under
`/var/gerrit/rebar-quiet-window-evidence/`, written by ad-hoc operator/agent shell rather
than by any rebar process. `rebar/host:disk_used_percent` reports how full the volume is and
structurally cannot report what it is full of, so the debris read as ordinary repository
growth until a human ran `du` mid-incident.

rebar cannot PREVENT that write — no rebar code path produced it — so the remediation is
detection: `observability.sh` §2c sums every top-level entry under `$DATA_MOUNT` that is not
`site` or `lost+found` and publishes `rebar/host:data_disk_debris_bytes` every tick. These
tests drive the REAL `observability.sh` over a synthetic mount point and assert it detects
planted debris, stays at 0 on a `site/`-only tree, and publishes NOTHING when the mount is
absent (so the alarm's `treat_missing_data = "breaching"` pages instead of a fabricated 0
asserting a volume nobody observed was clean).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from _journald_stub import JOURNALCTL_EMULATOR, TIMEOUT_STUB
from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40

METRIC = "data_disk_debris_bytes"

_OFFSET_VARIABLES = (
    "REPL_OFFSET_FILE",
    "VOTER_OFFSET_FILE",
    "MERGE_OFFSET_FILE",
    "DEPLOY_OFFSET_FILE",
    "DEFER_OFFSET_FILE",
    "INTERRUPT_OFFSET_FILE",
    "INTERRUPT_BOUND_OFFSET_FILE",
    "INTERRUPT_SIGNAL_OFFSET_FILE",
    "DISK_PRESSURE_OFFSET_FILE",
    "DISK_PRESSURE_PERSIST_OFFSET_FILE",
    "G2P_OFFSET_FILE",
)


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def _environment(tmp_path: Path, data_mount: Path) -> tuple[dict[str, str], Path, Path]:
    """Drive the real script with network/journal/logging stubbed but `du` REAL.

    `du` is deliberately NOT stubbed: the census's whole job is to measure bytes on a real
    filesystem, and a stubbed `du` would let the assertions pass over a size the script
    never actually computed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    logger_log = tmp_path / "logger.log"

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
    _stub(bin_dir, "logger", 'printf \'%s\\n\' "$*" >> "$LOGGER_LOG"; exit 0')
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    journal = tmp_path / "journal.txt"
    journal.write_text("")
    _stub(bin_dir, "journalctl", JOURNALCTL_EMULATOR)
    _stub(bin_dir, "timeout", TIMEOUT_STUB)

    offsets = tmp_path / "offsets"
    offsets.mkdir()
    for name in _OFFSET_VARIABLES:
        (offsets / name.lower()).write_text("0\n")

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "LOGGER_LOG": str(logger_log),
            "JOURNAL_FILE": str(journal),
            "REPL_LOG": str(tmp_path / "replication.log"),
            "DATA_MOUNT": str(data_mount),
            **{name: str(offsets / name.lower()) for name in _OFFSET_VARIABLES},
        }
    )
    (tmp_path / "replication.log").write_text("")
    return env, aws_log, logger_log


def _values(log: Path, metric: str) -> list[int]:
    values: list[int] = []
    if not log.exists():
        return values
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


def _plant(directory: Path, kib: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dump.bin").write_bytes(b"\0" * (kib * 1024))


def _clean_mount(tmp_path: Path) -> Path:
    """A data volume holding only what legitimately belongs on it."""
    mount = tmp_path / "var-gerrit"
    _plant(mount / "site" / "git" / "rebar.git", 256)
    (mount / "site" / "logs").mkdir(parents=True)
    (mount / "lost+found").mkdir()
    return mount


def test_clean_site_only_volume_publishes_zero(tmp_path: Path) -> None:
    """`site/` and `lost+found` are the volume's job — a large `site/` is never debris."""
    mount = _clean_mount(tmp_path)

    env, aws_log, logger_log = _environment(tmp_path, mount)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == [0]
    assert "debris_bytes=0" in logger_log.read_text()


def test_planted_evidence_directory_is_detected_and_named(tmp_path: Path) -> None:
    """The incident's shape: an evidence dump beside `site/` is counted and named."""
    mount = _clean_mount(tmp_path)
    _plant(mount / "rebar-quiet-window-evidence" / "epoch-probe-20260826T101500Z", 512)
    env, aws_log, logger_log = _environment(tmp_path, mount)

    result = _run(env)

    assert result.returncode == 0
    published = _values(aws_log, METRIC)
    assert len(published) == 1
    # >= the planted payload (directory entries add their own blocks), and well under the
    # 256 KiB of legitimate `site/` content plus the payload — proving `site/` was excluded.
    assert 512 * 1024 <= published[0] < 700 * 1024
    logged = logger_log.read_text()
    assert "rebar-quiet-window-evidence" in logged
    assert "investigation output does not belong here" in logged


def test_multiple_debris_entries_are_summed(tmp_path: Path) -> None:
    """Two dumps, one metric: the census reports the volume's total non-`site/` footprint."""
    mount = _clean_mount(tmp_path)
    _plant(mount / "rebar-quiet-window-evidence", 256)
    _plant(mount / "profiling-scratch", 256)
    env, aws_log, logger_log = _environment(tmp_path, mount)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC)[0] >= 512 * 1024
    logged = logger_log.read_text()
    assert "rebar-quiet-window-evidence" in logged
    assert "profiling-scratch" in logged


def test_hidden_debris_is_not_missed(tmp_path: Path) -> None:
    """A dotted directory is still debris; a glob that skipped it would be a silent hole."""
    mount = _clean_mount(tmp_path)
    _plant(mount / ".epoch-probe-scratch", 256)
    env, aws_log, logger_log = _environment(tmp_path, mount)

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC)[0] >= 256 * 1024
    assert ".epoch-probe-scratch" in logger_log.read_text()


def test_absent_mount_publishes_nothing_rather_than_a_reassuring_zero(tmp_path: Path) -> None:
    """No datapoint when the volume is unmountable: silence pages, a fabricated 0 would not.

    The alarm is `treat_missing_data = "breaching"`, so publishing 0 for a volume we could
    not observe would actively suppress the page for a WORSE fault than the one this metric
    names.
    """
    env, aws_log, _ = _environment(tmp_path, tmp_path / "not-mounted")

    result = _run(env)

    assert result.returncode == 0
    assert _values(aws_log, METRIC) == []
