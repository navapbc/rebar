"""The make-verify host preflight (fervent-cultish-sanderling).

Every probe is driven through its injected seam, so these tests touch no network and do
not depend on file-permission semantics (which root ignores).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_verify_host", _ROOT / "scripts" / "check_verify_host.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def _done(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class _FakeSocket:
    """Records the AF_UNIX calls without touching the host (the sandbox denies the real ones)."""

    calls: ClassVar[list[str]] = []

    def __init__(self, family, kind) -> None:
        # Read at call time, so it is the AF_UNIX value each test patched in.
        assert family == mod.socket.AF_UNIX

    def bind(self, path) -> None:
        self.calls.append("bind")

    def listen(self, backlog) -> None:
        self.calls.append("listen")

    def connect(self, path) -> None:
        self.calls.append("connect")

    def close(self) -> None:
        self.calls.append("close")


def test_unix_socket_probe_passes_when_bind_listen_and_connect_succeed(monkeypatch) -> None:
    # Windows CPython has no AF_UNIX; the probe would pass without using the factory.
    monkeypatch.setattr(mod.socket, "AF_UNIX", 1, raising=False)
    _FakeSocket.calls = []

    assert mod.unix_socket_probe(socket_factory=_FakeSocket) == (True, "")
    assert _FakeSocket.calls == ["bind", "listen", "connect", "close", "close"]


def test_unix_socket_probe_reports_a_denied_bind(monkeypatch) -> None:
    monkeypatch.setattr(mod.socket, "AF_UNIX", 1, raising=False)

    class _Denied(_FakeSocket):
        def bind(self, path) -> None:
            raise PermissionError(1, "Operation not permitted")

    ok, detail = mod.unix_socket_probe(socket_factory=_Denied)

    assert not ok
    assert "PermissionError" in detail and "Operation not permitted" in detail


def test_unix_socket_probe_passes_where_the_platform_has_no_af_unix(monkeypatch) -> None:
    monkeypatch.delattr(mod.socket, "AF_UNIX", raising=False)

    assert mod.unix_socket_probe() == (True, "")


def test_package_index_probe_runs_the_build_provenance_command() -> None:
    seen: dict[str, object] = {}

    def runner(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        assert "hatchling.build" in (Path(cmd[-1]) / "pyproject.toml").read_text(encoding="utf-8")
        return _done(0)

    assert mod.package_index_probe(runner=runner) == (True, "")
    assert seen["cmd"][:4] == [sys.executable, "-m", "build", "--outdir"]
    assert "env" not in seen["kwargs"]
    assert seen["kwargs"]["timeout"] == mod.PROBE_TIMEOUT_SECONDS


def test_package_index_probe_reports_a_failed_build_with_its_last_line() -> None:
    ok, detail = mod.package_index_probe(
        runner=lambda *a, **k: _done(1, "Retrying...\nERROR: No matching distribution found\n")
    )

    assert not ok
    assert detail.endswith("ERROR: No matching distribution found")


def test_package_index_probe_reports_a_timeout() -> None:
    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    ok, detail = mod.package_index_probe(runner=runner)

    assert not ok
    assert "did not finish" in detail


def test_semgrep_validate_probe_passes_without_an_engine() -> None:
    def runner(*a, **k):
        raise AssertionError("no engine, so nothing may run")

    assert mod.semgrep_validate_probe(runner=runner, which=lambda _name: None) == (True, "")


def test_semgrep_validate_probe_validates_a_rule_with_redirected_state() -> None:
    seen: dict[str, object] = {}

    def runner(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        return _done(0)

    which = {"semgrep": "/bin/semgrep"}.get
    assert mod.semgrep_validate_probe(runner=runner, which=which) == (True, "")
    assert seen["cmd"][:3] == ["/bin/semgrep", "--validate", "--config"]
    assert "--metrics=off" in seen["cmd"]
    assert Path(seen["env"]["SEMGREP_LOG_FILE"]).name == "semgrep.log"
    assert Path(seen["env"]["SEMGREP_SETTINGS_FILE"]).name == "settings.yml"


def test_semgrep_validate_probe_prefers_opengrep_like_the_backend() -> None:
    seen: list[str] = []
    which = {"opengrep": "/bin/opengrep", "semgrep": "/bin/semgrep"}.get

    mod.semgrep_validate_probe(runner=lambda cmd, **k: seen.append(cmd[0]) or _done(0), which=which)

    assert seen == ["/bin/opengrep"]


def test_semgrep_validate_probe_reports_a_failed_validate() -> None:
    ok, detail = mod.semgrep_validate_probe(
        runner=lambda *a, **k: _done(2, "requests.exceptions.ProxyError: Unable to connect\n"),
        which={"opengrep": "/bin/opengrep"}.get,
    )

    assert not ok
    assert "/bin/opengrep --validate failed" in detail and "ProxyError" in detail


def test_main_is_silent_and_zero_when_every_capability_is_present(capsys) -> None:
    assert mod.main((("a", lambda: (True, "")), ("b", lambda: (True, "")))) == 0
    assert capsys.readouterr() == ("", "")


def test_main_names_every_missing_capability_and_exits_1(capsys) -> None:
    def boom() -> tuple[bool, str]:
        raise RuntimeError("unexpected")

    probes = (
        ("cap-one", lambda: (False, "denied")),
        ("cap-two", boom),
        ("cap-ok", lambda: (True, "")),
    )

    assert mod.main(probes) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "verify-host: missing cap-one: denied"
    assert err[1] == "verify-host: missing cap-two: probe raised RuntimeError: unexpected"
    assert "docs/local-dev-env.md" in err[2]
    assert len(err) == 3


def test_verify_runs_the_preflight_strictly_before_its_gates() -> None:
    """Prerequisite order alone does not hold under make -j or -k, so the gates run in the
    recipe of a target whose only prerequisite is the preflight."""
    makefile = (_ROOT / "Makefile").read_text(encoding="utf-8")
    rule = re.search(r"^verify:([^\n#]*)(?:##[^\n]*)?\n((?:\t[^\n]*\n)+)", makefile, re.M)

    assert rule is not None
    assert rule.group(1).split() == ["verify-host"]
    recipe = [
        line.strip() for line in rule.group(2).splitlines() if not line.strip().startswith("@#")
    ]
    assert recipe == ["@$(MAKE) --no-print-directory lint typecheck import-convention test"]
    assert re.search(r"^verify-host:[^\n]*\n\t[^\n]*scripts/check_verify_host\.py", makefile, re.M)
