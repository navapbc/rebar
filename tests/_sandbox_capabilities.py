"""Shared test-harness compatibility helpers for sandboxed agent hosts."""

# mechanism-ok: test_helper tests/_sandbox_capabilities.py — 985f-0ec5-a61c-46ee

from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _subprocess_env import subprocess_env

_GIT_CONFIG_INJECTION_PREFIXES = (
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_CONFIG_PARAMETERS",
)

_COUNTED_SKIPS_ATTR = "_rebar_counted_skips"
_COUNTED_SKIPS_FROM_WORKERS_ATTR = "_rebar_counted_skips_from_workers"


@dataclass(frozen=True)
class CapabilityProbe:
    """Result of an operation-based capability probe."""

    available: bool
    reason: str = ""


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def scrub_ambient_git_config(env: MutableMapping[str, str]) -> None:
    """Drop command-scope git config injected by the ambient host."""
    for name in list(env):
        if name.startswith(_GIT_CONFIG_INJECTION_PREFIXES):
            del env[name]


def configure_repo_git_identity(
    repo: Path, *, email: str = "t@example.invalid", name: str = "T"
) -> None:
    """Write a repo-local git identity for a caller-owned repository."""
    env = subprocess_env()
    scrub_ambient_git_config(env)
    for key, value in (("user.email", email), ("user.name", name)):
        subprocess.run(
            ["git", "-C", str(repo), "config", "--local", key, value],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )


def semgrep_settings_file_probe(
    *,
    binary: str,
    settings_file: Path,
    runner: ProcessRunner = subprocess.run,
    base_env: Mapping[str, str] | None = None,
) -> CapabilityProbe:
    """Probe whether semgrep honors writable user-file env vars on this host."""
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        rule_file = settings_file.with_name("semgrep-probe-rule.yml")
        target_file = settings_file.with_name("semgrep_probe_target.py")
        rule_file.write_text(
            "\n".join(
                [
                    "rules:",
                    "  - id: rebar-sandbox-probe",
                    "    pattern: rebar_sandbox_probe",
                    "    message: probe",
                    "    severity: INFO",
                    "    languages: [python]",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        target_file.write_text("print('rebar sandbox probe')\n", encoding="utf-8")
    except OSError as exc:
        return CapabilityProbe(False, str(exc))
    env = dict(os.environ if base_env is None else base_env)
    env["SEMGREP_SETTINGS_FILE"] = str(settings_file)
    env["SEMGREP_LOG_FILE"] = str(settings_file.with_name("semgrep.log"))
    try:
        completed = runner(
            [binary, "--config", str(rule_file), "--json", "--quiet", str(target_file)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        return CapabilityProbe(False, str(exc))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return CapabilityProbe(False, detail or f"{binary} exited {completed.returncode}")
    return CapabilityProbe(True)


def configure_semgrep_settings_file(
    repo_root: Path,
    *,
    env: MutableMapping[str, str] = os.environ,
) -> CapabilityProbe | None:
    """Point semgrep at a writable settings file without changing ``HOME``."""
    existing_settings = env.get("SEMGREP_SETTINGS_FILE")
    if existing_settings:
        env.setdefault("SEMGREP_LOG_FILE", str(Path(existing_settings).with_name("semgrep.log")))
        return CapabilityProbe(True)
    binary = shutil.which("opengrep") or shutil.which("semgrep")
    if binary is None:
        return None
    settings_file = (
        repo_root / ".rebar" / "scratch" / f"pytest-sandbox-{os.getpid()}" / "semgrep.yml"
    )
    probe = semgrep_settings_file_probe(
        binary=binary,
        settings_file=settings_file,
        base_env=env,
    )
    if probe.available:
        env["SEMGREP_SETTINGS_FILE"] = str(settings_file)
        env["SEMGREP_LOG_FILE"] = str(settings_file.with_name("semgrep.log"))
    return probe


def process_table_probe(
    *,
    runner: ProcessRunner = subprocess.run,
    cmd: Sequence[str] = ("ps", "-A", "-o", "pid="),
) -> CapabilityProbe:
    """Probe whether the host allows read-only process-table enumeration."""
    try:
        completed = runner(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return CapabilityProbe(False, str(exc))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return CapabilityProbe(False, detail or f"{cmd[0]} exited {completed.returncode}")
    if not completed.stdout.strip():
        return CapabilityProbe(False, "process table probe returned no rows")
    return CapabilityProbe(True)


def _counter_on(config: pytest.Config, attr: str) -> Counter[str]:
    counter = getattr(config, attr, None)
    if counter is None:
        counter = Counter()
        setattr(config, attr, counter)
    return counter


def record_counted_skip(config: pytest.Config, reason: str) -> None:
    """Record one expected sandbox skip for terminal-summary accounting."""
    _counter_on(config, _COUNTED_SKIPS_ATTR)[reason] += 1


def publish_counted_skips(config: pytest.Config) -> None:
    """Publish worker-local skip counts through xdist's ``workeroutput``."""
    workeroutput = getattr(config, "workeroutput", None)
    if isinstance(workeroutput, dict):
        workeroutput["rebar_counted_skips"] = dict(_counter_on(config, _COUNTED_SKIPS_ATTR))


def collect_counted_skips_from_worker(
    config: pytest.Config, workeroutput: Mapping[str, Any]
) -> None:
    """Aggregate one xdist worker's counted skips on the controller."""
    raw = workeroutput.get("rebar_counted_skips")
    if isinstance(raw, dict):
        _counter_on(config, _COUNTED_SKIPS_FROM_WORKERS_ATTR).update(
            {str(reason): int(count) for reason, count in raw.items()}
        )


def report_counted_skips(
    terminalreporter: Any,
    config: pytest.Config,
) -> None:
    """Emit one suite-level counted-skip summary, including xdist workers."""
    counts = Counter(_counter_on(config, _COUNTED_SKIPS_ATTR))
    counts.update(_counter_on(config, _COUNTED_SKIPS_FROM_WORKERS_ATTR))
    total = sum(counts.values())
    if not total:
        return
    terminalreporter.write_sep("-", f"sandbox compatibility skips: {total}")
    for reason, count in sorted(counts.items()):
        terminalreporter.write_line(f"{reason}: {count}")
