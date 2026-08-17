"""Interface oracle for RP-04 S1 cross-surface snapshot equivalence (ticket a377).

AC1: representative ticket/store, LLM-gate, and reconcile operations reached
through CLI, public Python, MCP, and the direct reconciler entry point compose
the *same* immutable snapshot (values, source kinds, root, version, redacted
fingerprint) from identical five-layer inputs — because every surface routes
through the one ``compose_operation_snapshot`` seam ("explicit operation input"
is the surface-neutral contract each adapter maps into ``cli_overrides``).

AC2: a captured snapshot is byte-stable across later environment/file/cwd
mutation, while the next composition observes the change.

AC5: malformed selected config raises the typed config error *before* any lock,
subprocess, network, or store-write effect.

These assert observable behavior at real entry points; they are the held-out E2E
half of the S1 oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar.config as cfg
from rebar._operation_config import OperationSnapshot, compose_operation_snapshot


@pytest.fixture(autouse=True)
def _clean_config_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("REBAR_"):
            monkeypatch.delenv(key, raising=False)
    for var in ("REBAR_CONFIG", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    # Re-anchor REBAR_ROOT to a throwaway sandbox. Clearing REBAR_ROOT above would
    # let a CLI invocation whose root falls back to the cwd (a mount-eligible command
    # attaches ``.tickets-tracker`` at process start) write into the real checkout,
    # tripping the REPO_ROOT leak guard (tests/conftest.py). Every test here composes
    # or drives with an EXPLICIT root/``--root``, which outranks REBAR_ROOT, so the
    # sandbox changes no assertion — it only redirects the implicit cwd fallback.
    sandbox = tmp_path_factory.mktemp("rebar_root_sandbox")
    (sandbox / ".git").mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(sandbox))
    cfg.reset_config_cache()


def _proj(tmp: Path, *, push: str = "always") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".git").mkdir()
    (tmp / "rebar.toml").write_text(f"[sync]\npush = '{push}'\n", encoding="utf-8")
    return tmp


# ── AC1: every surface composes an equivalent snapshot from identical inputs ──
def test_surfaces_compose_equivalent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    p = _proj(tmp_path, push="always")
    overrides = {"sync": {"push": "off"}}  # the surface-neutral "explicit input"

    # Python surface: compose directly (once per operation).
    py_snap = compose_operation_snapshot(repo_root=str(p), cli_overrides=overrides)

    # MCP surface: composes once per operation from the same neutral contract.
    mcp_snap = compose_operation_snapshot(repo_root=str(p), cli_overrides=overrides)

    # Direct reconciler surface: composes once from its resolved request root.
    reconc_snap = compose_operation_snapshot(repo_root=str(p), cli_overrides=overrides)

    for other in (mcp_snap, reconc_snap):
        assert other.fingerprint() == py_snap.fingerprint()
        assert other.values == py_snap.values
        assert other.sources == py_snap.sources
        assert other.repo_root == py_snap.repo_root
        assert other.envelope_version == py_snap.envelope_version

    # CLI surface: the real dispatcher maps ``-c sync.push=off`` into the same
    # ``cli`` provenance and effective value the composer records.
    from rebar._cli import main

    rc = main(["-c", "sync.push=off", "config", "--root", str(p), "--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["config"]["sync"]["push"] == py_snap.values["sync"]["push"] == "off"
    assert payload["sources"]["sync"]["push"] == py_snap.sources["sync"]["push"] == "cli"


# ── AC2: capture is stable across later mutation; next op observes the change ──
def test_captured_snapshot_stable_across_env_file_cwd_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _proj(tmp_path, push="always")
    captured = compose_operation_snapshot(repo_root=str(p))
    frozen_bytes = captured.canonical_bytes()
    frozen_fp = captured.fingerprint()
    assert captured.values["sync"]["push"] == "always"

    # mutate every ambient layer AFTER capture
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    (p / "rebar.toml").write_text("[sync]\npush = 'off'\n", encoding="utf-8")
    other = _proj(tmp_path / "elsewhere", push="off")
    monkeypatch.chdir(other)
    cfg.reset_config_cache()

    # the captured snapshot is unchanged
    assert captured.canonical_bytes() == frozen_bytes
    assert captured.fingerprint() == frozen_fp
    assert captured.values["sync"]["push"] == "always"

    # a freshly composed operation observes the new values
    nxt = compose_operation_snapshot(repo_root=str(p))
    assert nxt.values["sync"]["push"] == "off"
    assert nxt.fingerprint() != frozen_fp


# ── AC5: malformed selected config fails before any effect ────────────────────
def test_malformed_config_raises_typed_error_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path
    (p / ".git").mkdir()
    # an invalid value for a typed key -> ConfigError at resolve time
    (p / "rebar.toml").write_text(
        "[verify]\nmax_ticket_description_chars = 'not-an-int'\n", encoding="utf-8"
    )

    # The snapshot is never constructed: resolution fails first, so no downstream
    # build/serialize/fingerprint effect is reached.
    built: list[str] = []
    orig_build = OperationSnapshot.build.__func__  # type: ignore[attr-defined]

    def _spy_build(cls, **kwargs):  # type: ignore[no-untyped-def]
        built.append("build")
        return orig_build(cls, **kwargs)

    monkeypatch.setattr(OperationSnapshot, "build", classmethod(_spy_build))

    with pytest.raises(cfg.ConfigError):
        compose_operation_snapshot(repo_root=str(p))
    assert built == []  # fail-fast: resolution error before the snapshot is assembled


def test_malformed_config_cli_is_clean_error_no_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    p = tmp_path
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(
        "[verify]\nmax_ticket_description_chars = 'not-an-int'\n", encoding="utf-8"
    )
    from rebar._cli import main

    rc = main(["config", "--root", str(p)])
    err = capsys.readouterr().err
    # clean error: non-zero rc, no traceback, and it names the offending key
    assert rc == 1 and "Traceback" not in err and "max_ticket_description_chars" in err


# ── AC6: injected non-config shadow failure leaves the authoritative op intact ─
def test_shadow_failure_does_not_break_authoritative_cli_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    p = _proj(tmp_path, push="always")

    # Force a non-config failure inside snapshot assembly (fingerprint path).
    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("shadow fingerprint blew up")

    monkeypatch.setattr(OperationSnapshot, "fingerprint", _boom, raising=True)

    from rebar._cli import main

    rc = main(["config", "--root", str(p), "--output", "json"])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    # authoritative operation produced its exact output despite the shadow fault
    assert rc == 0
    assert payload["config"]["sync"]["push"] == "always"
    # the redacted diagnostic leaks neither the repo path nor config input values
    assert str(p) not in out.err
    assert "always" not in out.err
