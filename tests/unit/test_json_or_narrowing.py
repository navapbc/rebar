"""_json_or defaults only on JSONDecodeError from malformed subprocess
output. A programming error such as a non-string input must propagate instead
of being hidden by a broad exception handler.
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
