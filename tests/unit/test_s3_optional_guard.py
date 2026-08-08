"""Unit tests for the git-remote-s3 availability/version guard (story 2b05).

``require_s3_helper()`` is a pure PATH+version check: it succeeds only when the
``git-remote-s3`` console script is on ``PATH`` and the installed distribution is
``>=0.3.2``. It has no git or network dependency, so every oracle here monkeypatches
``shutil.which`` and ``importlib.metadata.version`` — the two seams the guard reads.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from rebar import _optional

pytestmark = pytest.mark.unit


def _install(monkeypatch, *, on_path: bool, version: str | None) -> None:
    """Arrange the guard's two probes: PATH presence and distribution version.

    ``version=None`` makes the metadata lookup raise ``PackageNotFoundError`` (the
    "distribution not installed" case).
    """
    monkeypatch.setattr(
        _optional.shutil,
        "which",
        lambda name: "/usr/bin/git-remote-s3" if on_path else None,
    )

    def _version(dist: str) -> str:
        if version is None:
            raise importlib.metadata.PackageNotFoundError(dist)
        return version

    monkeypatch.setattr(_optional.importlib.metadata, "version", _version)


def test_happy_present_and_new_enough(monkeypatch) -> None:
    """Script on PATH and version at the exact minimum -> success (no raise)."""
    _install(monkeypatch, on_path=True, version="0.3.2")
    # Success is defined as "does not raise".
    _optional.require_s3_helper()


def test_failure_script_missing_names_install(monkeypatch) -> None:
    """No console script on PATH -> OptionalDependencyError naming the pip install."""
    _install(monkeypatch, on_path=False, version="0.3.2")
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.require_s3_helper()
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)


def test_failure_dist_metadata_missing_names_install(monkeypatch) -> None:
    """PackageNotFoundError from version() -> same actionable install error."""
    _install(monkeypatch, on_path=True, version=None)
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.require_s3_helper()
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)


def test_edge_too_old_names_minimum(monkeypatch) -> None:
    """A version below 0.3.2 fails closed and names the >=0.3.2 minimum."""
    _install(monkeypatch, on_path=True, version="0.2.9")
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.require_s3_helper()
    assert "0.3.2" in str(ei.value)


def test_edge_newer_version_succeeds(monkeypatch) -> None:
    """A version above the minimum succeeds."""
    _install(monkeypatch, on_path=True, version="0.4.0")
    _optional.require_s3_helper()


def test_edge_suffixed_version_succeeds(monkeypatch) -> None:
    """A PEP 440 post/dev suffix still parses to (0,3,2) and succeeds."""
    _install(monkeypatch, on_path=True, version="0.3.2.post1")
    _optional.require_s3_helper()


def test_edge_unparseable_version_fails_closed(monkeypatch) -> None:
    """A non-numeric leading component is treated as NOT meeting the minimum."""
    _install(monkeypatch, on_path=True, version="unknown")
    with pytest.raises(_optional.OptionalDependencyError):
        _optional.require_s3_helper()


def test_guard_import_missing_names_the_extra() -> None:
    """The s3 extra routes a missing probe through the standard one-line message."""
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.guard_import("git_remote_s3_missing_xyz", extra="s3")
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)


def test_s3_extra_registered_with_probe_and_blurb() -> None:
    """The s3 extra is declared in EXTRAS with a probe module and a blurb."""
    probe, blurb = _optional.EXTRAS["s3"]
    assert probe == "git_remote_s3"
    assert blurb
