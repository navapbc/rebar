"""Process-level contracts for the connected Jira probe preflight."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

from _subprocess_env import SubprocessEnv, subprocess_env

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Probe:
    name: str
    path: Path
    opt_in: str


PROBES = (
    Probe(
        name="validation",
        path=ROOT / "scripts" / "jira-pressure-test" / "e2e_validation_probe.sh",
        opt_in="REBAR_E2E_VALIDATION_PROBE",
    ),
    Probe(
        name="field-validation",
        path=ROOT / "scripts" / "jira-pressure-test" / "e2e_field_validation_probe.sh",
        opt_in="REBAR_FIELD_VALIDATION_PROBE",
    ),
)


@dataclass(frozen=True)
class ProbeWorkspace:
    root: Path
    engine: Path
    python: Path
    ticket_cli: Path
    events: Path
    bin_dir: Path


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _write_python_stub(path: Path) -> None:
    _write_executable(
        path,
        """
        if [[ "$*" == *"get_myself"* ]]; then
            printf 'python\t%s\t%s\tjira-read\n' "$0" "$PWD" >> "$PROBE_TEST_EVENTS"
            printf 'Probe User\n'
            exit 0
        fi
        if [[ "$*" == *"rebar_reconciler"* ]]; then
            printf 'python\t%s\t%s\timport\n' "$0" "$PWD" >> "$PROBE_TEST_EVENTS"
            rc="${PROBE_TEST_IMPORT_RC:-0}"
            if [ "$rc" -ne 0 ]; then
                printf 'rebar_reconciler import rejected by probe fixture\n' >&2
            fi
            exit "$rc"
        fi
        printf 'python\t%s\t%s\tunexpected\n' "$0" "$PWD" >> "$PROBE_TEST_EVENTS"
        exit 96
        """,
    )


def _write_ticket_stub(path: Path) -> None:
    _write_executable(
        path,
        """
        printf 'ticket\t%s\t%s\t%s\n' "$0" "$PWD" "$*" >> "$PROBE_TEST_EVENTS"
        exit 97
        """,
    )


def _workspace(tmp_path: Path) -> ProbeWorkspace:
    repo = tmp_path / "checkout"
    engine = repo / "src" / "rebar" / "_engine"
    engine.mkdir(parents=True)
    (engine / "rebar_reconciler").mkdir()

    python = repo / ".venv" / "bin" / "python"
    ticket_cli = repo / ".venv" / "bin" / "rebar"
    events = tmp_path / "events.tsv"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_python_stub(python)
    _write_ticket_stub(ticket_cli)
    _write_executable(
        bin_dir / "git",
        """
        if [ "$*" = "rev-parse --show-toplevel" ]; then
            printf '%s\n' "$PROBE_TEST_REPO_ROOT"
            exit 0
        fi
        printf 'unexpected git invocation %s\n' "$*" >&2
        exit 95
        """,
    )
    return ProbeWorkspace(repo, engine, python, ticket_cli, events, bin_dir)


def _environment(workspace: ProbeWorkspace, probe: Probe) -> SubprocessEnv:
    env = subprocess_env()
    for name in (
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT",
        "REBAR_E2E_VALIDATION_PROBE",
        "REBAR_FIELD_VALIDATION_PROBE",
        "REBAR_ENGINE_DIR",
        "REBAR_TICKET_CLI",
    ):
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{workspace.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PROBE_TEST_REPO_ROOT": str(workspace.root),
            "PROBE_TEST_EVENTS": str(workspace.events),
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "probe@example.test",
            "JIRA_API_TOKEN": "secret-for-subprocess-fixture",
            "JIRA_PROJECT": "REB",
            probe.opt_in: "1",
        }
    )
    return env


def _run(probe: Probe, env: SubprocessEnv) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(probe.path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _events(workspace: ProbeWorkspace) -> list[str]:
    if not workspace.events.exists():
        return []
    return workspace.events.read_text(encoding="utf-8").splitlines()


def test_each_probe_uses_checkout_tools_after_preflight(tmp_path: Path) -> None:
    failures: list[str] = []
    for probe in PROBES:
        workspace = _workspace(tmp_path / probe.name)
        completed = _run(probe, _environment(workspace, probe))
        events = _events(workspace)

        if completed.returncode != 97:
            failures.append(
                f"{probe.name} returned {completed.returncode} instead of reaching the ticket stub"
            )
        if not events or not events[0].startswith(
            f"python\t{workspace.python}\t{workspace.engine}\timport"
        ):
            failures.append(
                f"{probe.name} did not check the reconciler import with the checkout Python"
            )
        if not events or not events[-1].startswith(f"ticket\t{workspace.ticket_cli}\t"):
            failures.append(f"{probe.name} did not reach the checkout ticket CLI after preflight")
        if any(line.startswith("ticket\t") for line in events[:-1]):
            failures.append(f"{probe.name} invoked the ticket CLI before preflight completed")

    assert not failures, "\n".join(failures)


def test_missing_requirements_stop_both_probes_before_mutation(tmp_path: Path) -> None:
    failures: list[str] = []
    required = (
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT",
        "probe-opt-in",
    )
    for probe in PROBES:
        for requirement in required:
            workspace = _workspace(tmp_path / probe.name / requirement)
            env = _environment(workspace, probe)
            missing_name = probe.opt_in if requirement == "probe-opt-in" else requirement
            env.pop(missing_name)

            completed = _run(probe, env)
            output = completed.stdout + completed.stderr
            events = _events(workspace)
            if completed.returncode == 0:
                failures.append(f"{probe.name} accepted missing {missing_name}")
            if missing_name not in output:
                failures.append(f"{probe.name} did not identify missing {missing_name}")
            if any(line.startswith("ticket\t") for line in events):
                failures.append(f"{probe.name} mutated a ticket while {missing_name} was absent")

    assert not failures, "\n".join(failures)


def test_missing_checkout_tools_and_failed_import_stop_before_mutation(tmp_path: Path) -> None:
    failures: list[str] = []
    for probe in PROBES:
        missing_python = _workspace(tmp_path / probe.name / "missing-python")
        missing_python.python.unlink()
        completed = _run(probe, _environment(missing_python, probe))
        output = completed.stdout + completed.stderr
        if completed.returncode == 0 or str(missing_python.python) not in output:
            failures.append(f"{probe.name} did not reject the missing checkout Python")
        if any(line.startswith("ticket\t") for line in _events(missing_python)):
            failures.append(f"{probe.name} reached ticket mutation without the checkout Python")

        missing_ticket = _workspace(tmp_path / probe.name / "missing-ticket")
        missing_ticket.ticket_cli.unlink()
        completed = _run(probe, _environment(missing_ticket, probe))
        output = completed.stdout + completed.stderr
        if completed.returncode == 0 or str(missing_ticket.ticket_cli) not in output:
            failures.append(f"{probe.name} did not reject the missing checkout ticket CLI")
        if any(line.startswith("ticket\t") for line in _events(missing_ticket)):
            failures.append(f"{probe.name} reached ticket mutation without the checkout ticket CLI")

        failed_import = _workspace(tmp_path / probe.name / "failed-import")
        env = _environment(failed_import, probe)
        env["PROBE_TEST_IMPORT_RC"] = "42"
        completed = _run(probe, env)
        output = completed.stdout + completed.stderr
        if completed.returncode == 0 or "rebar_reconciler" not in output:
            failures.append(f"{probe.name} did not report the failed reconciler import")
        if any(line.startswith("ticket\t") for line in _events(failed_import)):
            failures.append(
                f"{probe.name} reached ticket mutation after the failed reconciler import"
            )

    assert not failures, "\n".join(failures)


def test_probe_specific_opt_ins_are_not_interchangeable(tmp_path: Path) -> None:
    failures: list[str] = []
    for probe, other in zip(PROBES, reversed(PROBES), strict=True):
        workspace = _workspace(tmp_path / probe.name)
        env = _environment(workspace, probe)
        env.pop(probe.opt_in)
        env[other.opt_in] = "1"

        completed = _run(probe, env)
        output = completed.stdout + completed.stderr
        if completed.returncode == 0 or probe.opt_in not in output:
            failures.append(f"{probe.name} accepted {other.opt_in} in place of {probe.opt_in}")
        if any(line.startswith("ticket\t") for line in _events(workspace)):
            failures.append(f"{probe.name} mutated a ticket under the other probe opt-in")

    assert not failures, "\n".join(failures)


def test_engine_and_ticket_overrides_remain_supported(tmp_path: Path) -> None:
    failures: list[str] = []
    for probe in PROBES:
        workspace = _workspace(tmp_path / probe.name)
        alternate_engine = workspace.root / "alternate-engine"
        alternate_engine.mkdir()
        alternate_ticket = workspace.root / "alternate-bin" / "rebar"
        _write_ticket_stub(alternate_ticket)
        env = _environment(workspace, probe)
        env["REBAR_ENGINE_DIR"] = str(alternate_engine)
        env["REBAR_TICKET_CLI"] = str(alternate_ticket)

        completed = _run(probe, env)
        events = _events(workspace)
        if completed.returncode != 97:
            failures.append(f"{probe.name} did not reach the ticket override")
        if not events or not events[0].startswith(
            f"python\t{workspace.python}\t{alternate_engine}\timport"
        ):
            failures.append(f"{probe.name} did not check imports from REBAR_ENGINE_DIR")
        if not events or not events[-1].startswith(f"ticket\t{alternate_ticket}\t"):
            failures.append(f"{probe.name} did not use REBAR_TICKET_CLI")

    assert not failures, "\n".join(failures)


def test_preflight_finishes_before_the_first_mutating_command(tmp_path: Path) -> None:
    failures: list[str] = []
    for probe in PROBES:
        workspace = _workspace(tmp_path / probe.name)
        completed = _run(probe, _environment(workspace, probe))
        events = _events(workspace)
        import_positions = [
            index for index, event in enumerate(events) if event.endswith("\timport")
        ]
        ticket_positions = [
            index for index, event in enumerate(events) if event.startswith("ticket\t")
        ]

        if completed.returncode != 97 or len(import_positions) != 1 or len(ticket_positions) != 1:
            failures.append(
                f"{probe.name} did not cross preflight and stop at the first ticket mutation"
            )
            continue
        if import_positions[0] > ticket_positions[0]:
            failures.append(f"{probe.name} checked imports after ticket mutation began")
        if probe.name == "field-validation":
            jira_read_positions = [
                index for index, event in enumerate(events) if event.endswith("\tjira-read")
            ]
            if len(jira_read_positions) != 1 or jira_read_positions[0] > ticket_positions[0]:
                failures.append(
                    f"{probe.name} did not finish its Jira identity read before ticket mutation"
                )

    assert not failures, "\n".join(failures)


def _load_external_probe(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_jira_connection_without_project(monkeypatch) -> None:
    monkeypatch.setenv("JIRA_URL", "https://jira.example.test")
    monkeypatch.setenv("JIRA_USER", "probe@example.test")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    monkeypatch.delenv("JIRA_PROJECT", raising=False)


def test_link_primitive_probe_requires_explicit_project(monkeypatch) -> None:
    module = _load_external_probe(
        ROOT / "tests" / "external" / "test_link_sync_live.py",
        "test_link_sync_live_project_gate",
    )
    _set_jira_connection_without_project(monkeypatch)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/fake/acli")

    assert module._live_jira_ready() is False


def test_link_roundtrip_probe_requires_explicit_project(monkeypatch) -> None:
    module = _load_external_probe(
        ROOT / "tests" / "external" / "test_link_sync_roundtrip_live.py",
        "test_link_sync_roundtrip_project_gate",
    )
    _set_jira_connection_without_project(monkeypatch)
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/fake/acli")

    assert module._live_jira_ready() is False
