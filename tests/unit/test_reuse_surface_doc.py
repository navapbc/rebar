"""Anti-drift check for the reusable-machinery reference (docs/reuse-surface.md).

The doc documents exact signatures of the reuse surface (the signing API, the LLM
runner + workflow runtime, the prompt library, the output-schema seam). This test is
the deterministic proving mechanism for its "verified against the current source (no
drift)" acceptance criterion (ticket f5df): it introspects each documented callable and
asserts its real signature matches what the doc states. If someone changes a signature
without updating the doc (or vice-versa), this fails — turning a manual-inspection
claim into a CI-enforced invariant.
"""

from __future__ import annotations

import inspect

import pytest


def _params(func) -> list[str]:
    return list(inspect.signature(func).parameters)


def test_signing_surface_signatures_match_doc() -> None:
    from rebar import signing

    assert _params(signing.sign_manifest) == ["ticket_id", "manifest", "repo_root"]
    assert _params(signing.verify_signature) == ["ticket_id", "repo_root"]
    assert _params(signing.verify_record) == ["record", "ticket_id", "key"]
    assert _params(signing.signing_key) == ["tracker", "create_if_missing"]
    assert _params(signing.key_fingerprint) == ["key"]
    assert _params(signing.compute_signature) == ["ticket_id", "manifest", "key"]
    assert _params(signing.parse_manifest) == ["payload"]
    assert _params(signing.head_sha) == ["repo_root"]


def test_prompt_library_signatures_match_doc() -> None:
    from rebar.llm import prompts

    assert _params(prompts.get_prompt) == ["prompt_id", "repo_root"]
    assert _params(prompts.resolve_prompt) == [
        "reviewer",
        "variables",
        "langfuse_cfg",
        "repo_root",
        "variant",
    ]
    # The closed front-matter contract key set the doc enumerates.
    for key in (
        "schema_version",
        "title",
        "description",
        "inputs",
        "outputs",
        "execution_mode",
        "category",
        "tags",
        "dimension",
        "applies_to",
        "default",
    ):
        assert key in prompts.FRONT_MATTER_KEYS
    assert prompts.EXECUTION_MODES == ("single_turn", "agentic")


def test_runner_and_contract_signatures_match_doc() -> None:
    from rebar.llm import contracts, findings
    from rebar.llm.runner import RunRequest, get_runner

    fields = RunRequest.__dataclass_fields__
    for f in ("system_prompt", "instructions", "config", "output_schema", "mode", "execution_mode"):
        assert f in fields
    assert _params(get_runner) == ["config", "override"]
    assert _params(contracts.register_contract) == ["name", "builder"]
    assert _params(contracts.response_model_for) == ["output_schema"]
    assert _params(findings.validate_structured) == ["data", "output_schema"]


def test_workflow_executor_signature_matches_doc() -> None:
    pytest.importorskip("yaml")
    from rebar.llm.workflow import executor

    params = _params(executor.run_workflow)
    # The doc documents these run_workflow parameters.
    for p in ("doc", "inputs", "run_id", "target_ticket", "repo_root"):
        assert p in params
