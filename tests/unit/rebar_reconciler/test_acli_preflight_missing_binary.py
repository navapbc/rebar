"""Ticket 3581-9cf1 — `acli` is an undeclared runtime prerequisite of the Jira Cloud path.

A non-container `pip install` succeeds without the `acli` (Atlassian CLI) binary, and then
every Jira **Cloud** mutation used to die with a bare
``FileNotFoundError([Errno 2] No such file or directory: 'acli')`` — an errno that names a
file, not a missing dependency, with no install pointer.

These held-out oracles pin the DECLARATION/preflight contract this ticket owns (distinct from
the transport-internals verdict mapping in ``access_check.py``, ticket ac93):

- A missing `acli` produces a **typed, actionable** ``AcliNotInstalledError`` naming the binary,
  the Jira Cloud requirement, and the install-doc remedy — not a bare ``[Errno 2]``.
- The typed error subclasses ``FileNotFoundError`` so existing/future handlers keep catching it.
- Three states are DISTINGUISHED, not merely three failures: missing-binary (typed error raised)
  vs missing-credentials (empty settings, no error) vs present (available + populated).
- A positive control with a stub `acli` on PATH proves the preflight is not unconditionally
  failing.

The absence is driven by REAL ``PATH`` manipulation (a temp dir with no `acli`), never by
monkeypatching the check away.
"""

from __future__ import annotations

import stat

import pytest

from rebar_reconciler.adapters.jira import acli_subprocess


def _path_without_acli(tmp_path, monkeypatch) -> None:
    """Point PATH at an empty dir so `acli` is genuinely unresolvable."""
    empty = tmp_path / "empty_bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _stub_acli_on_path(tmp_path, monkeypatch) -> None:
    """Put an executable stub named `acli` on PATH (positive control)."""
    bindir = tmp_path / "stub_bin"
    bindir.mkdir()
    stub = bindir / "acli"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir))


# --------------------------------------------------------------------------------------
# AC #2 — a missing acli produces a TYPED, actionable failure, not a bare [Errno 2].
# --------------------------------------------------------------------------------------


def test_require_acli_raises_typed_actionable_error_when_absent(tmp_path, monkeypatch):
    _path_without_acli(tmp_path, monkeypatch)

    with pytest.raises(acli_subprocess.AcliNotInstalledError) as ei:
        acli_subprocess.require_acli()

    msg = str(ei.value)
    assert "acli" in msg
    # names the Jira Cloud requirement (so the reader knows WHICH path needs it)...
    assert "Cloud" in msg
    # ...and points at the install remedy doc (a path from failure to fix).
    assert "docs/jira-sync-setup.md" in msg


def test_typed_error_is_a_filenotfounderror_subclass():
    # Backward-compatible with existing/future FileNotFoundError handlers (e.g. ac93's
    # transport-internals mapping) — so typing the error does not break them.
    assert issubclass(acli_subprocess.AcliNotInstalledError, FileNotFoundError)


def test_run_acli_wraps_bare_errno_into_typed_error(tmp_path, monkeypatch):
    _path_without_acli(tmp_path, monkeypatch)

    with pytest.raises(acli_subprocess.AcliNotInstalledError) as ei:
        acli_subprocess._run_acli(["jira", "workitem", "view", "REB-1"])

    # The defect being fixed is precisely a BARE FileNotFoundError whose str is the raw errno.
    assert type(ei.value) is acli_subprocess.AcliNotInstalledError
    assert "docs/jira-sync-setup.md" in str(ei.value)
    assert str(ei.value) != "[Errno 2] No such file or directory: 'acli'"


# --------------------------------------------------------------------------------------
# Positive control — with a stub acli on PATH the preflight PASSES (not unconditional fail).
# --------------------------------------------------------------------------------------


def test_preflight_passes_with_stub_acli_on_path(tmp_path, monkeypatch):
    _stub_acli_on_path(tmp_path, monkeypatch)

    assert acli_subprocess.acli_binary_available() is True
    # Must not raise.
    acli_subprocess.require_acli()


def test_acli_binary_available_false_when_absent(tmp_path, monkeypatch):
    _path_without_acli(tmp_path, monkeypatch)
    assert acli_subprocess.acli_binary_available() is False


# --------------------------------------------------------------------------------------
# AC #3 — three states are DISTINGUISHED (not merely three failures).
#   missing-binary   -> AcliNotInstalledError raised
#   missing-creds    -> empty JiraSettings returned (NO error)
#   present + creds  -> available and populated
# --------------------------------------------------------------------------------------


def test_missing_binary_vs_missing_credentials_are_distinct(tmp_path, monkeypatch):
    # 1) missing binary -> typed error.
    _path_without_acli(tmp_path, monkeypatch)
    with pytest.raises(acli_subprocess.AcliNotInstalledError):
        acli_subprocess.require_acli()

    # 2) binary present (stub) but NO credentials -> resolve_jira_settings returns an
    #    empty api_token and does NOT raise AcliNotInstalledError. A different outcome.
    _stub_acli_on_path(tmp_path, monkeypatch)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    settings = acli_subprocess.resolve_jira_settings()
    assert settings.api_token == ""
    assert acli_subprocess.acli_binary_available() is True

    # 3) present + credentials -> populated secret, still available.
    monkeypatch.setenv("JIRA_API_TOKEN", "tok-xyz")
    settings2 = acli_subprocess.resolve_jira_settings()
    assert settings2.api_token == "tok-xyz"
    assert acli_subprocess.acli_binary_available() is True


def test_data_center_path_does_not_import_acli_transport():
    """AC #4 — the DC adapter must not acquire a dependency on the acli subprocess transport.

    Structural guard: the DC transport module resolves without importing the Cloud acli
    subprocess transport (it uses the `jira` Python library / REST).
    """
    import importlib

    dc_transport = importlib.import_module("rebar_reconciler.adapters.jira_datacenter.transport")
    src = dc_transport.__file__ or ""
    assert src.endswith("transport.py")
    # The DC transport does not shell out to acli.
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert 'Popen(["acli"' not in text
    assert "_DEFAULT_ACLI_CMD" not in text
