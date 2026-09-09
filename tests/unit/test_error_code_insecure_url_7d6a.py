"""Classify ``InsecureUrlError`` as ``config_insecure_url``.

The distinct code separates a cleartext-URL policy rejection from an unreadable
configuration and must be selected before the general ``ConfigError`` arm.
"""

from __future__ import annotations

import rebar
from rebar.config import ConfigError, InsecureUrlError


def test_insecure_url_error_classifies_as_config_insecure_url() -> None:
    exc = InsecureUrlError("reconciler.base_url uses cleartext http://")
    assert rebar.error_code_for(exc) == "config_insecure_url"


def test_config_insecure_url_is_a_known_error_code() -> None:
    assert "config_insecure_url" in rebar.KNOWN_ERROR_CODES


def test_plain_config_error_still_classifies_as_config_unreadable() -> None:
    assert rebar.error_code_for(ConfigError("boom")) == "config_unreadable"


def test_classification_is_message_independent() -> None:
    assert rebar.error_code_for(InsecureUrlError("wording one")) == rebar.error_code_for(
        InsecureUrlError("totally different wording")
    )
