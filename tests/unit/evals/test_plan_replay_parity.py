"""Tests for the eval model-parity guard (``rebar.llm.evals.plan_replay.parity``).

Every eval pass must run on the SAME model class production uses (Pass 1 = finders =
frontier/Opus, Pass 2 = verifier = standard/Sonnet), on a Bedrock-only, fallback-free
chain — a caller cannot suppress the runner's fallback chain merely by not building one
itself: ``PydanticAIRunner.run`` re-derives it from the resolved model STRING against
whatever config root is active (``runner.py:457-459``, ``fallback_targets_for``). The
guard therefore hands eval callers an EPHEMERAL, fallback-free config root to point their
``LLMConfig`` at, rather than just returning a bare model id.

No live/billable call: everything here is pure config/string logic — no LLM, no network.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.evals.plan_replay import parity
from rebar.llm.model_classes import load_class_slots

pytestmark = pytest.mark.unit


def _fixture_repo_root(
    tmp_path,
    *,
    frontier_model="bedrock:us.anthropic.claude-opus-4-8",
    standard_model="bedrock:us.anthropic.claude-sonnet-4-6",
    frontier_fallback=True,
):
    """A throwaway repo root with a KNOWN ``[llm.model_classes]`` table — self-contained,
    so these tests never depend on ambient/sandboxed config resolution (the test harness
    globally sandboxes REBAR_ROOT for safety, per tests/conftest.py's
    ``_no_ambient_model_classes``/``_no_live_model_requests``, so reading "the real repo's
    rebar.toml" via bare ``load_class_slots()`` does not see this project's actual
    production config under pytest)."""
    root = tmp_path / "fixture_repo"
    root.mkdir()
    fallback_line = (
        'fallback = [{ model = "anthropic:claude-opus-4-8" }]' if frontier_fallback else ""
    )
    (root / "rebar.toml").write_text(
        "[llm.model_classes]\n"
        f'frontier = {{ model = "{frontier_model}", {fallback_line} }}\n'
        f'standard = {{ model = "{standard_model}" }}\n'
    )
    return str(root)


# ── happy path ──────────────────────────────────────────────────────────────────
def test_resolve_pinned_model_pass1_is_bedrock_frontier_fallback_free(tmp_path):
    """Pass 1 resolves to the fixture's frontier (Opus) class, on Bedrock, and the
    returned config_root's own model-class table has an EMPTY fallback for that class
    EVEN THOUGH the source config had one — proving the actual homogeneous-chain
    mechanism (fallback_targets_for reads cfg.repo_path), not merely asserting the
    return value's shape."""
    fixture_root = _fixture_repo_root(tmp_path)
    pinned = parity.resolve_pinned_model("pass1", repo_root=fixture_root)

    assert pinned.model_id == "bedrock:us.anthropic.claude-opus-4-8"

    source_slots = load_class_slots(repo_root=fixture_root)
    assert source_slots["frontier"].fallback, "fixture setup: source config must HAVE a fallback"

    ephemeral_slots = load_class_slots(repo_root=pinned.config_root)
    assert ephemeral_slots["frontier"].model == pinned.model_id
    assert ephemeral_slots["frontier"].fallback == ()


# ── edge: pass2 / unmapped pass ──────────────────────────────────────────────────
def test_resolve_pinned_model_pass2_is_bedrock_standard_fallback_free(tmp_path):
    fixture_root = _fixture_repo_root(tmp_path)
    pinned = parity.resolve_pinned_model("pass2", repo_root=fixture_root)

    assert pinned.model_id == "bedrock:us.anthropic.claude-sonnet-4-6"

    ephemeral_slots = load_class_slots(repo_root=pinned.config_root)
    assert ephemeral_slots["standard"].fallback == ()


def test_resolve_pinned_model_rejects_unmapped_pass_name():
    with pytest.raises(ValueError):
        parity.resolve_pinned_model("pass3")


# ── edge: non-Bedrock primary refused ────────────────────────────────────────────
def test_resolve_pinned_model_refuses_non_bedrock_primary(monkeypatch, tmp_path):
    """A frontier class resolving to a non-Bedrock (e.g. direct-Anthropic) primary is
    refused outright — parity is meaningless off Bedrock."""
    fake_toml = tmp_path / "rebar.toml"
    fake_toml.write_text(
        '[llm.model_classes]\nfrontier = { model = "anthropic:claude-opus-4-8" }\n'
    )
    with pytest.raises((ValueError, Exception)):
        parity.resolve_pinned_model("pass1", repo_root=str(tmp_path))


# ── edge: refuse_diff_on_model_mismatch ──────────────────────────────────────────
def test_refuse_diff_on_model_mismatch_raises_on_differing_ids():
    result_a = {"models": {"pass1": "bedrock:us.anthropic.claude-opus-4-8"}}
    result_b = {"models": {"pass1": "bedrock:us.anthropic.claude-sonnet-4-6"}}
    with pytest.raises(ValueError):
        parity.refuse_diff_on_model_mismatch(result_a, result_b)


def test_refuse_diff_on_model_mismatch_accepts_identical_ids():
    result_a = {"models": {"pass1": "bedrock:us.anthropic.claude-opus-4-8"}}
    result_b = {"models": {"pass1": "bedrock:us.anthropic.claude-opus-4-8"}}
    parity.refuse_diff_on_model_mismatch(result_a, result_b)  # must not raise


# ── edge: check_cache_effective ──────────────────────────────────────────────────
def test_check_cache_effective_marks_uncached_when_zero_reads():
    rows = [
        {"cache_read_tokens": 0, "cache_write_tokens": 100},
        {"cache_read_tokens": 0, "cache_write_tokens": 0},
    ]
    verdict = parity.check_cache_effective(rows)
    assert verdict["cached"] is False
    assert verdict["first_cache_hit_row"] is None


def test_check_cache_effective_marks_cached_on_later_hit():
    rows = [
        {"cache_read_tokens": 0, "cache_write_tokens": 100},
        {"cache_read_tokens": 800, "cache_write_tokens": 0},
    ]
    verdict = parity.check_cache_effective(rows)
    assert verdict["cached"] is True
    assert verdict["first_cache_hit_row"] == 1


# ── edge: corpus.py surfaces ran_model ───────────────────────────────────────────
def test_corpus_row_surfaces_ran_model_from_provider_provenance(tmp_path):
    from rebar.llm.evals.plan_replay import corpus
    from rebar.llm.plan_review.pass1 import material_fingerprint
    from tests.unit.evals.test_plan_replay_corpus import TrackerBuilder, _ctx

    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0007"
    tracker.create(ticket_id, description="Parity fixture plan.")
    ctx = _ctx(ticket_id, "Parity fixture plan.")
    fp = material_fingerprint(ctx)
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp,
            "reviewed_related_material": [],
            "provider_provenance": {"ran_model": "bedrock:us.anthropic.claude-opus-4-8"},
        },
    )

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")
    cache_file = tmp_path / "cache" / f"{manifest['content_hash']}.jsonl"
    rows = [json.loads(line) for line in cache_file.read_text().splitlines() if line.strip()]
    (row,) = [r for r in rows if r["ticket_id"] == ticket_id]
    assert row["ran_model"] == "bedrock:us.anthropic.claude-opus-4-8"


def test_corpus_row_ran_model_absent_when_no_provenance(tmp_path):
    from rebar.llm.evals.plan_replay import corpus
    from rebar.llm.plan_review.pass1 import material_fingerprint
    from tests.unit.evals.test_plan_replay_corpus import TrackerBuilder, _ctx

    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0008"
    tracker.create(ticket_id, description="No-provenance fixture plan.")
    ctx = _ctx(ticket_id, "No-provenance fixture plan.")
    fp = material_fingerprint(ctx)
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp,
            "reviewed_related_material": [],
        },
    )

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")
    cache_file = tmp_path / "cache" / f"{manifest['content_hash']}.jsonl"
    rows = [json.loads(line) for line in cache_file.read_text().splitlines() if line.strip()]
    (row,) = [r for r in rows if r["ticket_id"] == ticket_id]
    assert row["ran_model"] is None


def test_corpus_row_ran_model_absent_when_provenance_is_explicit_null(tmp_path):
    """A sidecar carrying ``"provider_provenance": null`` (present key, null value — a
    real shape seen in the live corpus, distinct from the key being absent entirely)
    must not raise: ``dict.get(key, default)`` only applies the default when the key is
    MISSING, not when its value is explicitly None."""
    from rebar.llm.evals.plan_replay import corpus
    from rebar.llm.plan_review.pass1 import material_fingerprint
    from tests.unit.evals.test_plan_replay_corpus import TrackerBuilder, _ctx

    tracker = TrackerBuilder(tmp_path / "store")
    ticket_id = "0000-0000-0000-0010"
    tracker.create(ticket_id, description="Null-provenance fixture plan.")
    ctx = _ctx(ticket_id, "Null-provenance fixture plan.")
    fp = material_fingerprint(ctx)
    tracker.review_result(
        ticket_id,
        data={
            "schema": "plan_review_result_v2",
            "ticket_id": ticket_id,
            "verdict": "PASS",
            "material_fingerprint": fp,
            "reviewed_related_material": [],
            "provider_provenance": None,
        },
    )

    manifest = corpus.build_corpus({"main": str(tracker.path)}, cache_dir=tmp_path / "cache")
    cache_file = tmp_path / "cache" / f"{manifest['content_hash']}.jsonl"
    rows = [json.loads(line) for line in cache_file.read_text().splitlines() if line.strip()]
    (row,) = [r for r in rows if r["ticket_id"] == ticket_id]
    assert row["ran_model"] is None
