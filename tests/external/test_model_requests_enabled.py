"""Check that the external tier re-enables pydantic-ai model requests.

The default suite disables ``models.ALLOW_MODEL_REQUESTS`` to prevent provider calls. The
external fixture must restore it before service tests run. This check makes no model call and
requires no credential.
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
