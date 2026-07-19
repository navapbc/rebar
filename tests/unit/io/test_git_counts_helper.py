"""Regression for the shared crash-safe commit-count helper (bug cf3a / efb7-09de).

``git rev-list --count`` can transiently fail under CI load (rc!=0, empty stdout).
The old inline ``int(r.stdout.strip())`` turned that into an opaque
``ValueError: invalid literal for int() with base 10: ''``. The shared
``_git_counts.commit_count`` helper must instead retry the transient and, on a
persistent failure, raise a diagnostic ``RuntimeError`` that surfaces git's stderr
— never a bare ``ValueError``. These tests inject the failure so no real CI-load
race is needed, and they pin BOTH the retry path and the diagnostic surface.
"""

from __future__ import annotations

import subprocess

import _git_counts
import pytest

# A sentinel git dir our mocks intercept; any command NOT targeting it is
# delegated to real git, so the repo-isolation guard's own rev-list is untouched.
_SENTINEL = "/rebar-cf3a-sentinel-dir"


def test_persistent_transient_raises_diagnostic_not_int_valueerror(monkeypatch) -> None:
    """A persistent rc=128 / empty stdout must raise a clear RuntimeError carrying
    git's stderr — NOT the opaque ``int('')`` ValueError the guard replaced."""
    real_run = subprocess.run

    def dead_run(cmd, *a, **kw):
        if isinstance(cmd, list) and _SENTINEL in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: bad revision")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_git_counts.subprocess, "run", dead_run)

    with pytest.raises(RuntimeError) as ei:
        _git_counts.commit_count(_SENTINEL, "HEAD", attempts=3, delay=0)

    msg = str(ei.value)
    assert "invalid literal" not in msg, f"still the opaque int('') crash: {msg!r}"
    assert "bad revision" in msg, f"error must surface git's stderr, got {msg!r}"


def test_retries_past_a_transient_then_succeeds(monkeypatch) -> None:
    """A single transient (rc=128, empty stdout) is retried, and the next clean
    result is returned — proving the guard retries rather than crashing."""
    real_run = subprocess.run
    calls = {"n": 0}

    def flaky_run(cmd, *a, **kw):
        if isinstance(cmd, list) and _SENTINEL in cmd:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: transient")
            return subprocess.CompletedProcess(cmd, 0, stdout="7\n", stderr="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(_git_counts.subprocess, "run", flaky_run)

    assert _git_counts.commit_count(_SENTINEL, "HEAD", attempts=5, delay=0) == 7
    assert calls["n"] == 2, "expected exactly one retry past the injected transient"
