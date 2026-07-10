"""Guard: the external tier must PERMIT live model requests.

The default suite installs a session-scoped safety net
(``tests/conftest.py::_no_live_model_requests``) that flips pydantic-ai's global
``models.ALLOW_MODEL_REQUESTS`` kill-switch to ``False`` so a stray unit test can
never bill a real provider. The external tier's whole purpose is the opposite —
it makes REAL, billable calls — so the tier must re-enable the switch, or every
live test raises ``RuntimeError: model requests are not allowed`` (0.1s, before any
network I/O). This asserts the re-enable is in effect; it makes NO model call and
needs NO credentials.
"""

from __future__ import annotations

import pytest


def test_external_tier_allows_model_requests() -> None:
    pai_models = pytest.importorskip("pydantic_ai.models")
    assert pai_models.ALLOW_MODEL_REQUESTS is True, (
        "the external tier must re-enable pydantic-ai model requests; the default "
        "suite's session guard leaves ALLOW_MODEL_REQUESTS False, which blocks every "
        "live call in tests/external/ with RuntimeError before any network I/O"
    )
