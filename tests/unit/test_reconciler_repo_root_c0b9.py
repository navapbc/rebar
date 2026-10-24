"""Test repo-root precedence and noneditable-install fallback."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _noneditable_package_config_file(tmp_path: Path) -> Path:
    """Model an installed ``config.py`` whose package ancestors are not a checkout."""
    cfg = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "rebar" / "config.py"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# stand-in installed module\n", encoding="utf-8")
    return cfg


def test_repo_root_precedence_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit root outranks ``REBAR_ROOT``. Otherwise, ``REBAR_ROOT`` wins."""
    from rebar.config import repo_root

    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    env_root = tmp_path / "env_root"
    env_root.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(env_root))

    assert repo_root(explicit) == explicit.resolve(), (
        "an explicit repo_root argument must win over the REBAR_ROOT env var"
    )
    assert repo_root() == env_root.resolve(), (
        "with no explicit argument, REBAR_ROOT must win over the discovered fallback"
    )


def test_reconciler_event_identity_env_override_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set ``REBAR_ENV_ID`` and ``REBAR_AUTHOR`` overrides determine the identity pair."""
    from rebar.config import reconciler_event_identity

    monkeypatch.setenv("REBAR_ENV_ID", "prod:main")
    monkeypatch.setenv("REBAR_AUTHOR", "reconciler:svc")
    assert reconciler_event_identity() == ("prod:main", "reconciler:svc"), (
        "a set REBAR_ENV_ID/REBAR_AUTHOR must propagate through the owned identity resolver"
    )


def test_reconciler_repo_root_errors_when_neither_package_nor_cwd_is_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a checkout or ``REBAR_ROOT``, name both root options in the error.

    A noneditable install must never return its site-packages tree as the repo root.
    """
    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    scratch = tmp_path / "scratch_not_a_repo"
    scratch.mkdir()
    monkeypatch.chdir(scratch)

    with pytest.raises(RuntimeError) as excinfo:
        cfg_mod.reconciler_repo_root()

    message = str(excinfo.value)
    assert "REBAR_ROOT" in message, (
        "the resolution error must name REBAR_ROOT as a remedy; got: " + message
    )
    assert "--repo-root" in message, (
        "the resolution error must name --repo-root as a remedy; got: " + message
    )


def test_reconciler_repo_root_git_probe_failure_degrades_to_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing or timed-out Git discovery falls through to the root-options error.

    Neither failure may expose a subprocess traceback instead of ``REBAR_ROOT`` and
    ``--repo-root`` remedies.
    """
    import subprocess as _subprocess

    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    def _git_unavailable(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(cfg_mod.subprocess, "run", _git_unavailable)

    with pytest.raises(RuntimeError) as excinfo:
        cfg_mod.reconciler_repo_root()

    message = str(excinfo.value)
    assert "REBAR_ROOT" in message and "--repo-root" in message, (
        "a failed git probe must fall through to the clear step-4 error; got: " + message
    )

    def _git_timeout(*_args: object, **_kwargs: object) -> object:
        raise _subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(cfg_mod.subprocess, "run", _git_timeout)
    with pytest.raises(RuntimeError):
        cfg_mod.reconciler_repo_root()


def test_reconciler_repo_root_falls_back_to_cwd_checkout_under_noneditable_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no ``REBAR_ROOT``, use the CWD checkout when the package is noneditable."""
    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    checkout = tmp_path / "real_checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "src" / "rebar").mkdir(parents=True)
    monkeypatch.chdir(checkout)

    resolved = cfg_mod.reconciler_repo_root()
    assert resolved.resolve() == checkout.resolve(), (
        "with a non-checkout package root and REBAR_ROOT unset, the resolver must fall "
        f"back to the git toplevel of the CWD; got {resolved!r}, expected {checkout!r}"
    )
    # Contract: the resolved root is a real checkout (the discriminator the venv tree fails).
    assert (resolved / ".git").exists() and (resolved / "src" / "rebar").is_dir(), (
        f"resolved root {resolved!r} is not a real checkout"
    )


def test_reconciler_repo_root_preserves_env_and_editable_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Honor explicit ``REBAR_ROOT`` verbatim. Editable installs use their checkout."""
    import rebar.config as cfg_mod

    # Control 1: explicit REBAR_ROOT wins verbatim.
    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(explicit))
    assert cfg_mod.reconciler_repo_root() == Path(str(explicit)), (
        "a set REBAR_ROOT must be honored verbatim as the first-priority root"
    )

    # Control 2: editable/source install resolves to the real checkout (package root valid).
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    resolved = cfg_mod.reconciler_repo_root()
    assert (resolved / ".git").exists() and (resolved / "src" / "rebar").is_dir(), (
        f"editable-install package root must be a real checkout; got {resolved!r}"
    )
