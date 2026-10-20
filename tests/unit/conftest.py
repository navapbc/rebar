"""Configure unit tests to import bundled engine helpers.

The engine path and reconciler compatibility shims expose the ``rebar.*``
packages used by the library without per-test ``sys.path`` changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_session_log_writes(monkeypatch):
    """Prevent unit tests from committing session logs or writing the current-log pointer.

    Degraded-gate telemetry reaches ``rebar.append_session_log`` indirectly. Replacing
    that seam isolates the unit tier from the tickets branch and
    ``.rebar/current_session_log``. A function-scoped monkeypatch can restore the
    helper for a test that exercises it.
    """
    import rebar

    def _noop_append_session_log(*_args, **_kwargs):
        return {"id": None, "alias": None, "created": False}

    monkeypatch.setattr(rebar, "append_session_log", _noop_append_session_log)


@pytest.fixture(scope="session")
def _empty_mcp_client_home(tmp_path_factory):
    """One empty directory standing in for an unconfigured operator home."""
    return tmp_path_factory.mktemp("mcp-client-home")


@pytest.fixture(autouse=True)
def _isolated_mcp_client_home(_empty_mcp_client_home, monkeypatch):
    """Redirect implicit MCP client scans to an empty test directory.

    ``doctor_cli`` calls ``scan_mcp_clients`` without ``home``, which would read
    operator configs through ``Path.home()``. This fixture changes only that default.
    Explicit ``home`` arguments, ``$HOME``, and other home-derived resources remain
    unchanged.
    """
    from rebar._commands import doctor_mcp_client

    original = doctor_mcp_client.scan_mcp_clients

    def _scan_isolated(*, home=None, env=None, cwd=None):
        isolated = _empty_mcp_client_home if home is None else home
        isolated_cwd = _empty_mcp_client_home if cwd is None else cwd
        return original(home=isolated, env=env, cwd=isolated_cwd)

    monkeypatch.setattr(doctor_mcp_client, "scan_mcp_clients", _scan_isolated)


@pytest.fixture(scope="session")
def _isolated_aws_home(tmp_path_factory):
    """One injected directory standing in for an unconfigured operator ``~/.aws``.

    Holds an (empty) ``config`` and an (empty) ``credentials`` file so a boto3
    Session pointed at them resolves NO named profiles — the shape of a machine
    that has never configured AWS credentials.
    """
    aws_home = tmp_path_factory.mktemp("aws-home")
    (aws_home / "config").write_text("")
    (aws_home / "credentials").write_text("")
    return aws_home


@pytest.fixture(autouse=True)
def _isolated_aws_provider_credentials(_isolated_aws_home, monkeypatch):
    """Isolate Bedrock tests from operator AWS profiles and credential files.

    Boto3 resolves ambient profile selectors while constructing a session. Pointing
    its config and credential paths at empty fixture files and removing those
    selectors prevents host state from causing ``ProfileNotFound``. Region variables
    and ``$HOME`` remain unchanged. Function-scoped monkeypatches can supply alternate
    AWS settings.
    """
    monkeypatch.setenv("AWS_CONFIG_FILE", str(_isolated_aws_home / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(_isolated_aws_home / "credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
