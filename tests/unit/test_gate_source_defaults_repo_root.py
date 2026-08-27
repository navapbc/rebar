"""`resolve_gate_handle` must default the repo root from config, as it does ref and source.

Ticket: fatherly-incoherent-mare (1eb6-65f2-5a4e-4c7b).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import gate_source


def _capture_acquire(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record the repo_root that reaches the snapshot layer, and stop there."""
    seen: dict = {}

    def fake_acquire(ref, *, source_mode, repo_root, fetch):
        seen["repo_root"] = repo_root
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(gate_source, "acquire", fake_acquire)
    monkeypatch.setattr(gate_source, "default_ref", lambda _r=None: "main")
    monkeypatch.setattr(gate_source, "default_source", lambda _r=None: "attested")
    return seen


def test_a_none_repo_root_is_resolved_from_config_before_the_snapshot_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE DEPLOYED FAILURE. Every attested tool — review_plan, scan_spec, verify_completion,
    review_code — calls through WITHOUT a repo_root (`_mcp_llm.py:116, 146, 196`). If this
    function passes that None straight down, the snapshot layer falls back to the bare cwd,
    which on the MCP container is `/app`: a source copy whose `.git` is excluded by
    `.dockerignore`. Every one of them then failed with
    `cannot resolve ref 'origin/main' to a commit in '.'` while REBAR_ROOT named a healthy
    checkout that resolved the very SHA being requested.

    This function already applies the configured defaults for `ref` and `source`; the root is
    the third one it owes.
    """
    real = tmp_path / "checkout"
    real.mkdir()
    not_a_repo = tmp_path / "elsewhere"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    monkeypatch.setenv("REBAR_ROOT", str(real))

    seen = _capture_acquire(monkeypatch)
    with pytest.raises(RuntimeError, match="stop-after-capture"):
        gate_source.resolve_gate_handle(None, None, None)

    assert seen["repo_root"] == str(real), (
        "a None repo_root must be resolved from REBAR_ROOT before reaching the snapshot "
        f"layer; got {seen['repo_root']!r}, which sends it to the bare-cwd fallback"
    )


def test_an_explicit_repo_root_is_never_overridden_by_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control. Reversing this would be WORSE than the bug being fixed: a gate would
    silently verify a different tree than the caller named, and still sign an attestation
    saying it verified the named one.
    """
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path / "other"))

    seen = _capture_acquire(monkeypatch)
    with pytest.raises(RuntimeError, match="stop-after-capture"):
        gate_source.resolve_gate_handle(None, None, str(explicit))

    assert seen["repo_root"] == str(explicit), (
        "an explicitly passed repo_root must never be replaced by REBAR_ROOT"
    )
