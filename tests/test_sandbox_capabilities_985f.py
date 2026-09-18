"""Sandbox compatibility probes for bug 985f-0ec5-a61c-46ee."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _subprocess_env import subprocess_env

pytest_plugins = ("pytester",)


def test_scrub_ambient_git_config_removes_command_scope_identity() -> None:
    from _sandbox_capabilities import scrub_ambient_git_config

    env = subprocess_env(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="user.email",
        GIT_CONFIG_VALUE_0="harness@example.invalid",
        GIT_CONFIG_KEY_1="safe.bareRepository",
        GIT_CONFIG_VALUE_1="explicit",
        GIT_CONFIG_PARAMETERS="'user.name=Harness'",
    )

    scrub_ambient_git_config(env)

    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env


def test_pytest_scrubs_command_scope_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "harness@example.invalid")

    from _sandbox_capabilities import scrub_ambient_git_config

    scrub_ambient_git_config(os.environ)

    has_count = "GIT_CONFIG_COUNT" in os.environ
    has_key = "GIT_CONFIG_KEY_0" in os.environ
    has_value = "GIT_CONFIG_VALUE_0" in os.environ
    assert not has_count
    assert not has_key
    assert not has_value


def test_semgrep_settings_probe_reports_unavailable_against_denial_stub(tmp_path: Path) -> None:
    from _sandbox_capabilities import semgrep_settings_file_probe

    def denied(_cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise PermissionError("Operation not permitted")

    probe = semgrep_settings_file_probe(
        binary=sys.executable,
        settings_file=tmp_path / "settings.yml",
        runner=denied,
    )

    assert not probe.available
    assert "Operation not permitted" in probe.reason


def test_semgrep_settings_probe_reports_unavailable_on_nonzero_return(
    tmp_path: Path,
) -> None:
    from _sandbox_capabilities import semgrep_settings_file_probe

    def fails(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 7, "", "settings denied")

    probe = semgrep_settings_file_probe(
        binary=sys.executable,
        settings_file=tmp_path / "settings.yml",
        runner=fails,
    )

    assert not probe.available
    assert probe.reason == "settings denied"


def test_semgrep_settings_probe_reports_available_when_settings_path_works(tmp_path: Path) -> None:
    from _sandbox_capabilities import semgrep_settings_file_probe

    seen_env: dict[str, str] = {}
    seen_cmd: list[str] = []

    def succeeds(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmd.extend(cmd)
        seen_env.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    settings_file = tmp_path / "semgrep-settings" / "settings.yml"
    probe = semgrep_settings_file_probe(
        binary=sys.executable,
        settings_file=settings_file,
        runner=succeeds,
    )

    assert probe.available
    assert "--version" not in seen_cmd
    assert "--config" in seen_cmd
    assert seen_env["SEMGREP_SETTINGS_FILE"] == str(settings_file)
    assert seen_env["SEMGREP_LOG_FILE"] == str(settings_file.with_name("semgrep.log"))
    assert settings_file.parent.is_dir()


def test_configure_semgrep_settings_file_sets_semgrep_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import _sandbox_capabilities as sandbox

    env: dict[str, str] = {}
    probed: list[Path] = []

    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: sys.executable if name == "semgrep" else None,
    )

    def probe(
        *,
        binary: str,
        settings_file: Path,
        runner: Any = None,
        base_env: Any = None,
    ) -> sandbox.CapabilityProbe:
        probed.append(settings_file)
        assert binary == sys.executable
        assert base_env is env
        return sandbox.CapabilityProbe(True)

    monkeypatch.setattr(sandbox, "semgrep_settings_file_probe", probe)

    result = sandbox.configure_semgrep_settings_file(tmp_path, env=env)

    assert result is not None
    assert result.available
    assert probed == [
        tmp_path / ".rebar" / "scratch" / f"pytest-sandbox-{os.getpid()}" / "semgrep.yml"
    ]
    assert env["SEMGREP_SETTINGS_FILE"] == str(probed[0])
    assert env["SEMGREP_LOG_FILE"] == str(probed[0].with_name("semgrep.log"))


def test_process_table_probe_reports_unavailable_against_denial_stub() -> None:
    from _sandbox_capabilities import process_table_probe

    def denied(_cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise PermissionError("Operation not permitted")

    probe = process_table_probe(runner=denied)

    assert not probe.available
    assert "Operation not permitted" in probe.reason


def test_process_table_probe_reports_unavailable_when_ps_returns_no_rows() -> None:
    from _sandbox_capabilities import process_table_probe

    def empty(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, " \n", "")

    probe = process_table_probe(runner=empty)

    assert not probe.available
    assert probe.reason == "process table probe returned no rows"


def test_process_table_probe_reports_available_when_ps_works() -> None:
    from _sandbox_capabilities import process_table_probe

    def succeeds(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "  1\n", "")

    assert process_table_probe(runner=succeeds).available


def test_root_conftest_does_not_define_xdist_hook_unconditionally() -> None:
    import conftest

    assert not hasattr(conftest, "pytest_testnodedown")


def test_sandbox_skip_summary_aggregates_across_xdist_workers(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from _sandbox_capabilities import (
            publish_counted_skips,
            record_counted_skip,
            report_counted_skips,
        )

        def pytest_configure(config):
            config.addinivalue_line('markers', 'worker_a')
            config.addinivalue_line('markers', 'worker_b')

        def pytest_runtest_setup(item):
            workerid = getattr(item.config, 'workerinput', {{}}).get('workerid')
            if item.get_closest_marker('worker_a') and workerid == 'gw0':
                record_counted_skip(item.config, 'sandbox ps unavailable')
                import pytest
                pytest.skip('sandbox ps unavailable')
            if item.get_closest_marker('worker_b') and workerid == 'gw1':
                record_counted_skip(item.config, 'sandbox ps unavailable')
                import pytest
                pytest.skip('sandbox ps unavailable')

        def pytest_sessionfinish(session, exitstatus):
            publish_counted_skips(session.config)

        def pytest_terminal_summary(terminalreporter, exitstatus, config):
            report_counted_skips(terminalreporter, config)

        def pytest_testnodedown(node, error):
            from _sandbox_capabilities import collect_counted_skips_from_worker
            collect_counted_skips_from_worker(node.config, getattr(node, 'workeroutput', {{}}))
        """
    )
    pytester.makepyfile(
        test_one="""
        import pytest
        @pytest.mark.worker_a
        def test_a(): pass
        @pytest.mark.worker_b
        def test_b(): pass
        """
    )

    result = pytester.runpytest("-n", "2", "-q")

    result.assert_outcomes(skipped=2)
    result.stdout.fnmatch_lines(["*sandbox compatibility skips: 2*", "*sandbox ps unavailable: 2*"])
