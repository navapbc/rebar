"""Regression test for the `_json_or` opportunistic narrowing (epic ring-gun-jot, 8d01).

`rebar._json_or` previously caught a blind `except Exception` and returned a sentinel
default on ANY failure — masking genuine bugs (e.g. a TypeError from passing a non-str)
in the `import rebar` hot path. It was narrowed to `json.JSONDecodeError`, the only
expected failure (malformed/empty quality-gate subprocess output). This test pins both
the preserved fail-soft behavior AND the now-unmasked programming error.
"""

from __future__ import annotations

import pytest

import rebar


def test_malformed_json_returns_default() -> None:
    """The expected failure — malformed/empty JSON — still falls back to the default."""
    sentinel = {"passed": False}
    assert rebar._json_or("not json at all", sentinel) is sentinel
    assert rebar._json_or("", sentinel) is sentinel
    assert rebar._json_or("{partial", sentinel) is sentinel


def test_valid_json_is_parsed() -> None:
    assert rebar._json_or('{"score": 5, "passed": true}', None) == {"score": 5, "passed": True}


def test_non_json_decode_error_is_no_longer_masked() -> None:
    """The narrowing's point: a genuine bug (a non-str argument → TypeError) now PROPAGATES
    instead of being silently swallowed into the sentinel default."""
    with pytest.raises(TypeError):
        rebar._json_or(123, "default")  # type: ignore[arg-type]  # json.loads(int) raises TypeError
