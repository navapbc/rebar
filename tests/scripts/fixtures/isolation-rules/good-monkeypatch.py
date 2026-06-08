# Fixture: Python test file that uses monkeypatch.setenv instead of os.environ
# Expected: passes no-direct-os-environ rule

import os


def test_with_monkeypatch(monkeypatch):
    monkeypatch.setenv("MY_KEY", "value")
    assert os.environ["MY_KEY"] == "value"


def test_with_monkeypatch_delenv(monkeypatch):
    monkeypatch.delenv("NONEXISTENT", raising=False)


def test_reading_environ_is_fine():
    """Reading os.environ is fine — only assignment/mutation is flagged."""
    val = os.environ.get("PATH")
    assert val is not None
