"""Unit tests for the optional-dependency guard (WS-J1, rebar._optional)."""

from __future__ import annotations

import pytest

from rebar import _optional


def test_guard_import_success_returns_module() -> None:
    mod = _optional.guard_import("json", extra="agents")
    assert mod.dumps([1]) == "[1]"


def test_guard_import_missing_names_the_extra_and_pip_install() -> None:
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.guard_import("a_module_that_does_not_exist_xyz", extra="eval")
    msg = str(ei.value)
    assert "eval" in msg
    assert "pip install 'nava-rebar[eval]'" in msg


def test_require_extra_unknown_is_value_error() -> None:
    with pytest.raises(ValueError, match="unknown extra"):
        _optional.require_extra("bananas")


def test_require_extra_missing_raises_with_install_hint() -> None:
    # eval/tracing are net-new extras, not installed in the dev venv.
    if _optional.extra_installed("eval"):
        pytest.skip("eval extra installed in this env")
    with pytest.raises(_optional.OptionalDependencyError) as ei:
        _optional.require_extra("eval")
    assert "nava-rebar[eval]" in str(ei.value)


def test_extra_installed_returns_bool_and_false_for_unknown() -> None:
    assert isinstance(_optional.extra_installed("agents"), bool)
    assert _optional.extra_installed("nope") is False


def test_all_extras_have_a_probe_and_blurb() -> None:
    for extra, (probe, blurb) in _optional.EXTRAS.items():
        assert probe and blurb, f"extra {extra} missing probe/blurb"
