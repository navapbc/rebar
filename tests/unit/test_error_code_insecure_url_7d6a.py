"""InsecureUrlError classifies as ``config_insecure_url``, not ``config_unreadable`` (7d6a).

``error_code_for`` step 4 used to classify EVERY :class:`ConfigError` subclass as
``config_unreadable`` by isinstance — including :class:`InsecureUrlError`, which was
explicitly designed (bug bdb8) to let a caller tell a deliberate cleartext-URL
security-policy rejection apart from a malformed-config parse fault. An MCP client
branching on ``config_unreadable`` would mis-prompt the operator to "fix an unreadable
config" when the config parsed fine and was rejected by policy. The fix: a distinct
``config_insecure_url`` code, classified before the ``ConfigError`` arm.
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
