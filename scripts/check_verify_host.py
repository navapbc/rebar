"""Refuse ``make verify`` on a host that cannot give the same verdict as CI.

The suite exercises two host capabilities that CI runners always have and that an agent
sandbox can deny: AF_UNIX sockets (a private ``ssh-agent``, the network guard's local-IPC
allowance) and outbound HTTPS (isolated ``python -m build`` fetching its backend from the
package index, and ``semgrep --validate`` fetching its rule lints from semgrep.dev). Without
them about 20 tests fail, and some pass falsely, after a 25-minute run. These probes run
the same operations in about 10 seconds and refuse up front instead.

Exit 0 with no output when every probe passes; otherwise print one line per missing
capability plus a closing line and exit 1. There is no bypass: a host that can run the
suite passes the probes.
"""

# mechanism-ok: ci_gate scripts/check_verify_host.py — gaudy-shamefaced-hapuka verify host check
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ProbeResult = tuple[bool, str]
Runner = Callable[..., subprocess.CompletedProcess[str]]

#: Per-probe wall-clock bound. Both network probes finish in about 2 s on a capable host.
PROBE_TIMEOUT_SECONDS = 300

_PROBE_PROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "rebar-verify-host-probe"
version = "0"
"""

_PROBE_RULE = """\
rules:
  - id: rebar-verify-host-probe
    pattern-regex: rebar-verify-host-probe
    languages: [generic]
    message: probe
    severity: INFO
"""


def unix_socket_probe(socket_factory: Callable[..., socket.socket] = socket.socket) -> ProbeResult:
    """Bind, listen on and connect to an AF_UNIX socket in a fresh temp dir.

    A platform without AF_UNIX passes: the tests that need it skip there.
    """
    if not hasattr(socket, "AF_UNIX"):
        return True, ""
    with tempfile.TemporaryDirectory(prefix="rvh-") as tmp:
        path = os.path.join(tmp, "s")
        server = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            server.listen(1)
            client = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(path)
            finally:
                client.close()
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            server.close()
    return True, ""


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"exit {proc.returncode}"


def package_index_probe(runner: Runner = subprocess.run) -> ProbeResult:
    """Build a throwaway project with the command ``tests/unit/test_build_provenance.py`` runs.

    ``python -m build`` (sdist, then a wheel from it) creates an isolated environment and
    installs the hatchling backend from the package index, with the inherited environment
    and pip cache, so the probe sees the same network and cache state as those tests.
    """
    with tempfile.TemporaryDirectory(prefix="rvh-") as tmp:
        project = Path(tmp) / "project"
        (project / "rebar_verify_host_probe").mkdir(parents=True)
        (project / "rebar_verify_host_probe" / "__init__.py").write_text("", encoding="utf-8")
        (project / "pyproject.toml").write_text(_PROBE_PROJECT, encoding="utf-8")
        try:
            proc = runner(
                [sys.executable, "-m", "build", "--outdir", str(Path(tmp) / "out"), str(project)],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return False, f"python -m build did not finish within {PROBE_TIMEOUT_SECONDS} s"
    if proc.returncode != 0:
        return False, f"python -m build failed: {_tail(proc)}"
    return True, ""


def semgrep_validate_probe(
    runner: Runner = subprocess.run, which: Callable[[str], str | None] = shutil.which
) -> ProbeResult:
    """Run ``--validate`` on a one-rule config with the detector backend's flags.

    Without an engine on PATH the detector tests skip, so there is nothing to probe. The
    settings and log files go to the temp dir, mirroring the test session's own redirect
    (``configure_semgrep_settings_file``), so only the rule-lint download can fail it.
    """
    # Same preference order as rebar.grounding.engine_b._OPENGREP_CANDIDATES.
    engine = which("opengrep") or which("semgrep")
    if engine is None:
        return True, ""
    with tempfile.TemporaryDirectory(prefix="rvh-") as tmp:
        rule = Path(tmp) / "rule.yml"
        rule.write_text(_PROBE_RULE, encoding="utf-8")
        env = {
            **os.environ,
            "SEMGREP_SETTINGS_FILE": str(Path(tmp) / "settings.yml"),
            "SEMGREP_LOG_FILE": str(Path(tmp) / "semgrep.log"),
        }
        try:
            proc = runner(
                [engine, "--validate", "--config", str(rule), "--metrics=off"],
                capture_output=True, text=True, env=env, timeout=PROBE_TIMEOUT_SECONDS,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return False, f"{engine} --validate did not finish within {PROBE_TIMEOUT_SECONDS} s"
    if proc.returncode != 0:
        return False, f"{engine} --validate failed: {_tail(proc)}"
    return True, ""


PROBES: tuple[tuple[str, Callable[[], ProbeResult]], ...] = (
    ("AF_UNIX sockets (ssh-agent, network-guard local IPC)", unix_socket_probe),
    ("outbound HTTPS to the package index (isolated python -m build)", package_index_probe),
    ("outbound HTTPS to semgrep.dev (semgrep --validate rule lints)", semgrep_validate_probe),
)


def main(probes: tuple[tuple[str, Callable[[], ProbeResult]], ...] = PROBES) -> int:
    """Run every probe; report each missing capability; return the exit code."""
    missing: list[str] = []
    for capability, probe in probes:
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 — an unexpected probe error is a missing capability
            ok, detail = False, f"probe raised {type(exc).__name__}: {exc}"
        if not ok:
            missing.append(f"verify-host: missing {capability}: {detail}")
    if not missing:
        return 0
    for line in missing:
        print(line, file=sys.stderr)
    print(
        "verify-host: make verify needs these capabilities to give the same verdict as CI. "
        "Run it where they are available (for an agent sandbox, outside the sandbox); "
        "see docs/local-dev-env.md 'Where make verify can run'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
