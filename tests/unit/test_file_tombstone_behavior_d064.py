"""File-kind tombstones honour the registry's ``behavior`` field (bug d064).

``_deprecations`` is a typed tombstone registry: each retired input records
whether encountering it should ``warn`` or hard-``error``. The env and cfg kinds
route through ``raise_or_warn_env`` / ``raise_or_warn_cfg_key``. The file kind did
not -- ``raise_or_warn_file`` had zero callers and ``config.py`` hand-rolled an
unconditional raise for the single registered file tombstone, ignoring the row's
``behavior`` entirely.

That bypass was invisible only by coincidence: the one registered file entry
(``.rebar/config.conf``) carries behavior ``error`` and the hand-rolled code
raised, so registry and code agreed by accident. These tests remove the
coincidence -- they drive a ``warn``-class file tombstone (which has no coverage
today) and a downgraded ``.rebar/config.conf``, and assert the *registry row*
decides the outcome.

Assertions are on observable behavior: whether resolving config raises, and what
is logged. Nothing pins how ``config.py`` enumerates the rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import rebar._deprecations as dep
from rebar._deprecations import RemovedInput, RemovedInputError


def _registry_with(monkeypatch: pytest.MonkeyPatch, *rows: RemovedInput) -> None:
    """Replace the tombstone registry with exactly ``rows``.

    Patched by string name (``_TOMBSTONE_REGISTRY``), which is the reason this is
    a monkeypatch rather than an injected seam -- the module global is read at
    call time by every ``raise_or_warn_*`` helper.
    """
    monkeypatch.setattr(dep, "_TOMBSTONE_REGISTRY", tuple(rows))


def _file_row(name: str, behavior: str) -> RemovedInput:
    return RemovedInput(
        kind="file",
        name=name,
        replacement="rebar.toml [tool.rebar]",
        removed_in="0.1.0",
        behavior=behavior,
    )


def _resolve(repo: Path):
    from rebar.config import load_config, reset_config_cache

    reset_config_cache()
    return load_config(str(repo))


# ── the behavior field decides ───────────────────────────────────────────────


def test_error_class_file_tombstone_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the existing error-class behaviour is unchanged."""
    _registry_with(monkeypatch, _file_row(".rebar/config.conf", "error"))
    (tmp_path / ".rebar").mkdir(exist_ok=True)
    (tmp_path / ".rebar" / "config.conf").write_text("[core]\n")

    with pytest.raises(RemovedInputError):
        _resolve(tmp_path)


def test_warn_class_file_tombstone_warns_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``warn``-class file tombstone must NOT abort config resolution -- today
    the hand-rolled raise fires regardless of the row's behavior."""
    _registry_with(monkeypatch, _file_row(".rebar/config.conf", "warn"))
    (tmp_path / ".rebar").mkdir(exist_ok=True)
    (tmp_path / ".rebar" / "config.conf").write_text("[core]\n")

    with caplog.at_level(logging.WARNING):
        _resolve(tmp_path)  # must not raise

    assert any(".rebar/config.conf" in r.getMessage() for r in caplog.records), (
        "a warn-class file tombstone must emit a warning naming the retired file"
    )


def test_a_newly_registered_warn_file_tombstone_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The registry must drive detection, not a hardcoded filename. A file entry
    other than ``.rebar/config.conf`` is ignored entirely by the hand-rolled path."""
    _registry_with(monkeypatch, _file_row(".rebar/legacy-settings.conf", "warn"))
    (tmp_path / ".rebar").mkdir(exist_ok=True)
    (tmp_path / ".rebar" / "legacy-settings.conf").write_text("x = 1\n")

    with caplog.at_level(logging.WARNING):
        _resolve(tmp_path)

    assert any("legacy-settings.conf" in r.getMessage() for r in caplog.records), (
        "a registered file tombstone must be detected by its registry row"
    )


def test_a_newly_registered_error_file_tombstone_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _registry_with(monkeypatch, _file_row(".rebar/legacy-settings.conf", "error"))
    (tmp_path / ".rebar").mkdir(exist_ok=True)
    (tmp_path / ".rebar" / "legacy-settings.conf").write_text("x = 1\n")

    with pytest.raises(RemovedInputError):
        _resolve(tmp_path)


def test_absent_tombstoned_file_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The negative control: no retired file on disk means no warning and no raise,
    so the detection cannot be trivially always-on."""
    _registry_with(monkeypatch, _file_row(".rebar/legacy-settings.conf", "warn"))

    with caplog.at_level(logging.WARNING):
        _resolve(tmp_path)

    assert not any("legacy-settings.conf" in r.getMessage() for r in caplog.records)
