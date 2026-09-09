"""Cross-surface contract for operation snapshot composition.

CLI, public Python, MCP, and the reconciler entry point must compose equivalent snapshots from
identical inputs. MCP and reconciler tests invoke their public entry points and require the shared
composer to run. Each captured snapshot is checked against absolute values, provenance, root, and
envelope data so common corruption cannot pass by equality alone.

A captured snapshot remains stable after environment, file, or working-directory changes. A later
composition observes those changes. Invalid selected configuration fails before side effects.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _reconciler_main() -> Any:
    """Import the reconciler entry point from its sibling package.

    The engine directory stays on ``sys.path`` so later imports of unloaded reconciler submodules
    continue to work.
    """
    engine_dir = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    return importlib.import_module("rebar_reconciler.__main__")


def _composed_by(run: Callable[[], object]) -> list[OperationSnapshot]:
    """Record snapshots composed while ``run`` executes.

    Proxy surfaces resolve ``rebar._operation_config.compose_operation_snapshot`` at call time, so
    rebinding that attribute captures their calls. Direct imports in this module remain
    uninstrumented. The wrapper returns each original result unchanged. An empty capture means the
    surface bypassed the shared composer.
    """
    import rebar._operation_config as opcfg

    seen: list[OperationSnapshot] = []
    real = opcfg.compose_operation_snapshot

    def _record(**kwargs: Any) -> OperationSnapshot:
        snapshot = real(**kwargs)
        seen.append(snapshot)
        return snapshot

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(opcfg, "compose_operation_snapshot", _record)
        run()
    return seen


# The absolute expectation for the shared five-layer input built by
# ``test_surfaces_compose_equivalent_snapshot``: a project layer, an env layer, and
# the envelope. Asserted against EACH surface's own composed snapshot, not only the
# reference, so a perturbed composer is RED in the surface blocks themselves rather
# than uniformly wrong-but-equal across all of them.
_EXPECTED_LAYERS: tuple[tuple[str, str, object, str], ...] = (
    ("sync", "push", "always", "project"),
    ("verify", "max_ticket_description_chars", 4321, "env"),
)


def _assert_snapshot_content(surface: str, snap: OperationSnapshot, expected_root: Path) -> None:
    """The snapshot carries the expected values, provenance, root, and envelope."""
    assert Path(snap.repo_root) == expected_root, surface
    assert snap.envelope_version == cfg.ENVELOPE_VERSION, surface
    for section, key, value, source in _EXPECTED_LAYERS:
        assert snap.values[section][key] == value, f"{surface}: {section}.{key} value"
        assert snap.sources[section][key] == source, f"{surface}: {section}.{key} source"


def _assert_surface(
    surface: str,
    snapshots: list[OperationSnapshot],
    *,
    expected_root: Path,
    reference: OperationSnapshot,
) -> None:
    """The surface reached the composer, and what it composed is right AND identical."""
    assert snapshots, (
        f"the {surface} surface produced no operation snapshot. Either it no longer "
        "routes through compose_operation_snapshot, or the composer raised and "
        "compose_and_bind_operation_snapshot swallowed it (that fail-open swallow is "
        "deliberate — a malformed/insecure config must never break the legacy op — "
        "which is exactly why the surface cannot be trusted to report its own failure "
        "and this capture has to)"
    )
    for other in snapshots:
        _assert_snapshot_content(surface, other, expected_root)
        assert other.fingerprint() == reference.fingerprint(), surface
        assert other.values == reference.values, surface
        assert other.sources == reference.sources, surface
        assert other.repo_root == reference.repo_root, surface
        assert other.envelope_version == reference.envelope_version, surface


# ── AC1: every surface composes an equivalent snapshot from identical inputs ──
def test_surfaces_compose_equivalent_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    p = _proj(tmp_path, push="always")
    # Use project and environment inputs that every surface accepts with distinct provenance.
    monkeypatch.setenv("REBAR_ROOT", str(p))
    monkeypatch.setenv("REBAR_VERIFY_MAX_TICKET_DESCRIPTION_CHARS", "4321")
    cfg.reset_config_cache()

    # Python surface: compose directly (once per operation).
    py_snap = compose_operation_snapshot(repo_root=str(p))

    # Check absolute content before comparing surfaces so shared corruption cannot pass.
    expected_root = p.resolve()
    _assert_snapshot_content("public Python", py_snap, expected_root)

    # Invoke a FastMCP registry read to isolate the MCP composition path.
    from rebar.mcp_server import build_server

    server = build_server()
    mcp_composed = _composed_by(
        lambda: asyncio.run(server.call_tool("explain_criterion", {"criterion_id": "plan"}))
    )
    _assert_surface("MCP", mcp_composed, expected_root=expected_root, reference=py_snap)

    # Dry-run enumeration reaches reconciler composition before locks, passes, or network effects.
    reconciler_main = _reconciler_main()
    rcs: list[int] = []
    reconc_composed = _composed_by(
        lambda: rcs.append(reconciler_main.main(["--dry-run-enumerate", "--repo-root", str(p)]))
    )
    assert rcs == [0]
    _assert_surface("reconciler", reconc_composed, expected_root=expected_root, reference=py_snap)

    # CLI surface: the real dispatcher maps ``-c sync.push=off`` into the same
    # ``cli`` provenance and effective value the composer records.
    from rebar._cli import main

    cli_snap = compose_operation_snapshot(repo_root=str(p), cli_overrides={"sync": {"push": "off"}})
    rc = main(["-c", "sync.push=off", "config", "--root", str(p), "--output", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["config"]["sync"]["push"] == cli_snap.values["sync"]["push"] == "off"
    assert payload["sources"]["sync"]["push"] == cli_snap.sources["sync"]["push"] == "cli"


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
