"""``rebar.config.repo_root`` / ``reconciler_repo_root`` resolution (bug c0b9).

Moved from the retired ``tests/scripts/test_reconciler_core_seam_clean_heldout.py`` guard
(story skaldic-paradoxal-huemul); the behaviour tests are unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _noneditable_package_config_file(tmp_path: Path) -> Path:
    """A site-packages-shaped ``config.py`` whose ``parents[2]`` is NOT a checkout.

    Mirrors a wheel/non-editable install: ``<venv>/lib/pythonX/site-packages/rebar/
    config.py`` -> ``parents[2]`` == ``<venv>/lib/pythonX`` (no ``.git``, no
    ``src/rebar``). Directories are materialized so ``.resolve()`` behaves like a real
    install path.
    """
    cfg = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "rebar" / "config.py"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# stand-in installed module\n", encoding="utf-8")
    return cfg


def test_repo_root_precedence_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The owned root resolver keeps its precedence after the cut: an explicit argument
    wins over the ``REBAR_ROOT`` env var. A cut that hard-wires one source (or drops the
    explicit-argument path when threading through ``_PassContext``) breaks this."""
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
    """The owned ``reconciler_event_identity`` resolver reads the env OVERRIDE layer after
    the cut: a non-default ``REBAR_ENV_ID`` / ``REBAR_AUTHOR`` must propagate to the
    ``(env_id, author)`` pair (not be frozen to the legacy-Jira default). The unset-default
    branch is covered elsewhere; this pins the set branch so the cut cannot silently drop
    the env read."""
    from rebar.config import reconciler_event_identity

    monkeypatch.setenv("REBAR_ENV_ID", "prod:main")
    monkeypatch.setenv("REBAR_AUTHOR", "reconciler:svc")
    assert reconciler_event_identity() == ("prod:main", "reconciler:svc"), (
        "a set REBAR_ENV_ID/REBAR_AUTHOR must propagate through the owned identity resolver"
    )


def test_reconciler_repo_root_errors_when_neither_package_nor_cwd_is_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-editable install, REBAR_ROOT unset, and a CWD that is not a checkout: the
    resolver must RAISE a clear, actionable error naming both ``REBAR_ROOT`` and
    ``--repo-root`` — never silently return the venv/site-packages tree.

    This is the bug's discriminating leg: before the fix, ``reconciler_repo_root()``
    returns ``Path(__file__).resolve().parents[2]`` unconditionally, so under a
    site-packages ``__file__`` it hands back ``<venv>/lib/pythonX`` (a non-checkout) with
    no error — exactly the CI failure ``Resolved repo_root .../.venv/lib/python3.13``.
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
    """Non-editable install, REBAR_ROOT unset, and the step-3 ``git`` probe FAILS (git
    absent / hung): the resolver must still degrade to the clear step-4 error, never a raw
    subprocess traceback (``FileNotFoundError``/``TimeoutExpired``).

    Guards the ``_git_toplevel_of_cwd`` robustness the plan-review advisory flagged: a
    missing ``git`` binary or a probe timeout on a slow filesystem must be swallowed to
    ``None`` so step 4 (the actionable REBAR_ROOT/--repo-root message) is what surfaces.
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
    """Non-editable install (package root not a checkout), REBAR_ROOT unset, but the CWD
    IS inside a real rebar checkout: the resolver falls back to the git toplevel of the
    CWD and returns THAT checkout.

    Before the fix this returns the site-packages ``parents[2]`` tree instead, so the
    resolved root neither equals the checkout nor points at a real ``src/rebar`` tree.
    """
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
    """Behavior-preserving controls (must be green before AND after the fix):

    * ``REBAR_ROOT`` set -> returned verbatim (first-priority, unchanged), even when it
      points at a non-checkout — the operator's explicit choice is honored as-is.
    * Editable install (the real in-tree ``config.__file__``) with REBAR_ROOT unset ->
      the package root, which under the source checkout IS a real checkout.
    """
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
