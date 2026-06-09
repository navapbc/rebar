"""WS4: the engine must resolve to a REAL on-disk directory (no zipimport).

rebar's engine is bash + python helpers exec'd as real files. ``engine_dir()``
asserts the resolved path is a real directory and raises a clear RuntimeError
otherwise, so a zip-imported / mispackaged install fails loudly instead of with
an opaque bash error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import _engine


def test_engine_dir_is_real_on_disk_directory():
    p = _engine.engine_dir()
    assert p.is_dir(), f"engine_dir() must be a real directory, got {p!s}"
    # The dispatcher and the alias wordlist must be present as real files.
    assert _engine.dispatcher().is_file()
    assert _engine.wordlist_path().is_file()


def test_engine_dir_rejects_non_directory(monkeypatch):
    """If importlib.resources resolves the engine to a non-directory (e.g. a
    zipimport-backed path), engine_dir() raises a clear RuntimeError."""
    import importlib.resources

    _engine.engine_dir.cache_clear()
    monkeypatch.setattr(
        importlib.resources, "files", lambda _pkg: Path("/nonexistent/zipimported/rebar")
    )
    with pytest.raises(RuntimeError, match="real on-disk directory"):
        _engine.engine_dir()
    _engine.engine_dir.cache_clear()  # restore real resolution for other tests
