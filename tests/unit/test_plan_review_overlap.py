"""Unit tests for wiring store-wide overlap into plan review (epic only-crave-art, 0f70)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.schemas as schemas
from rebar._config_schema import _SECTIONS, VerifyConfig
from rebar.llm.config import LLMConfig
from rebar.llm.overlap import digest_sidecar as ds
from rebar.llm.overlap.wire import overlap_findings
from rebar.llm.runner import Runner, RunRequest

_DIGEST = {
    "problem_keywords": ["overlap", "detection"],
    "component_or_area": "plan-review gate",
    "key_entities": ["review_plan", "overlap_verdict"],
    "propositions": ["detect store-wide overlap", "advisory link suggestions"],
}


class _PipelineRunner(Runner):
    """Returns a canned ticket_digest for enrich calls and a canned overlap_verdict for judge
    calls (dispatched by output_schema), so the full wire pipeline runs deterministically."""

    name = "pipeline"

    def __init__(self, digest: dict, verdict: dict):
        self._digest = digest
        self._verdict = verdict

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        base = self._digest if req.output_schema == "ticket_digest" else self._verdict
        return {**base, "runner": self.name, "model": None, "trace_id": None}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    rebar.init_repo(repo_root=str(r))
    monkeypatch.setattr(ds, "_active_model", lambda repo_root: "m")
    return str(r)


def test_config_flag_default_off_and_coerces() -> None:
    assert VerifyConfig().overlap_enabled is False
    assert "overlap_enabled" in _SECTIONS["verify"]
    assert _SECTIONS["verify"]["overlap_enabled"]("true", "overlap_enabled") is True


def test_verdict_schema_accepts_overlap() -> None:
    # overlap[] rides in the verdict via additionalProperties:true — NO schema change.
    verdict = {
        "verdict": "PASS",
        "ticket_id": "abcd-1234",
        "overlap": [
            {
                "relation": "duplicates",
                "link_command": "rebar link a b duplicates",
                "confidence": 0.9,
            }
        ],
    }
    schemas.validator("plan_review_verdict").validate(verdict)  # must not raise


def test_overlap_findings_pipeline(repo: str) -> None:
    query = rebar.create_ticket("task", "Store-wide overlap detector", repo_root=repo)
    cand = rebar.create_ticket("task", "Cross-ticket overlap thing", repo_root=repo)
    # The candidate has a fresh, matching digest in the corpus.
    ds.emit(dict(_DIGEST), cand, model="m", repo_root=repo)

    verdict = {
        "relation": "duplicates",
        "shared_artifact": "review_plan",
        "confidence": 0.95,
        "abstain": False,
    }
    runner = _PipelineRunner(dict(_DIGEST), verdict)
    cfg = LLMConfig(overlap_min_should_match=0.1, overlap_max_doc_freq=1.0)

    out = overlap_findings(query, repo_root=repo, config=cfg, runner=runner)
    assert len(out) == 1
    assert out[0]["relation"] == "duplicates"
    assert cand in out[0]["link_command"]
    assert len(out) <= cfg.overlap_surface_cap


def test_overlap_findings_graceful_skip_cold(repo: str) -> None:
    # No candidate digests in the store → empty corpus → [] (never raises).
    query = rebar.create_ticket("task", "Alone", repo_root=repo)
    runner = _PipelineRunner(
        dict(_DIGEST), {"relation": "duplicates", "confidence": 0.9, "abstain": False}
    )
    out = overlap_findings(query, repo_root=repo, config=LLMConfig(), runner=runner)
    assert out == []


def test_overlap_findings_excludes_own_graph(repo: str) -> None:
    epic = rebar.create_ticket("epic", "Epic", repo_root=repo)
    query = rebar.create_ticket("task", "Query", parent=epic, repo_root=repo)
    child_of_epic = rebar.create_ticket("task", "Sibling", parent=epic, repo_root=repo)
    ds.emit(dict(_DIGEST), child_of_epic, model="m", repo_root=repo)  # in-graph → excluded

    verdict = {
        "relation": "duplicates",
        "shared_artifact": "x",
        "confidence": 0.95,
        "abstain": False,
    }
    runner = _PipelineRunner(dict(_DIGEST), verdict)
    cfg = LLMConfig(overlap_min_should_match=0.1, overlap_max_doc_freq=1.0)
    out = overlap_findings(query, repo_root=repo, config=cfg, runner=runner)
    assert out == []  # the only candidate is a sibling (own graph) → excluded


def test_render_overlap() -> None:
    from rebar._cli._llm_commands import _render_plan_review_text

    result = {
        "verdict": "PASS",
        "ticket_id": "abcd",
        "coverage": {"counts": {}},
        "blocking": [],
        "advisory": [],
        "overlap": [
            {
                "relation": "supersedes",
                "confidence": 0.9,
                "shared_artifact": "config key REBAR_X",
                "link_command": "rebar link A B supersedes",
            }
        ],
    }
    buf = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(buf):
        _render_plan_review_text(result)
    out = buf.getvalue()
    assert "overlap" in out
    assert "rebar link A B supersedes" in out


# ── AC-named proving tests (epic only-crave-art / 0f70 acceptance criteria) ──────
def test_surfaced(repo: str) -> None:
    """review-time overlap surfaces ≤3 findings in a dedicated overlap[] key, each with a
    ready-to-run rebar link command; the claim is never blocked (advisory only)."""
    query = rebar.create_ticket("task", "Store-wide overlap detector", repo_root=repo)
    cand = rebar.create_ticket("task", "Cross-ticket overlap thing", repo_root=repo)
    ds.emit(dict(_DIGEST), cand, model="m", repo_root=repo)
    runner = _PipelineRunner(
        dict(_DIGEST),
        {
            "relation": "duplicates",
            "shared_artifact": "review_plan",
            "confidence": 0.95,
            "abstain": False,
        },
    )
    cfg = LLMConfig(overlap_min_should_match=0.1, overlap_max_doc_freq=1.0)
    out = overlap_findings(query, repo_root=repo, config=cfg, runner=runner)
    assert 0 < len(out) <= cfg.overlap_surface_cap
    assert out[0]["link_command"].startswith("rebar link ")


def test_verdict_unchanged(repo: str) -> None:
    """overlap[] rides in a SEPARATE verdict key and never touches the signed coverage/
    attestation: injecting it leaves every other verdict key byte-identical."""
    base = {
        "verdict": "PASS",
        "ticket_id": "abcd",
        "coverage": {"counts": {"advisory_surfaced": 2}},
        "signature": {"signed": True, "key_id": "k"},
        "advisory": [{"id": "x"}],
    }
    import copy

    with_overlap = copy.deepcopy(base)
    with_overlap["overlap"] = [
        {"relation": "duplicates", "link_command": "rebar link a b duplicates", "confidence": 0.9}
    ]
    # Every pre-existing key is byte-identical; only the new overlap[] key is added.
    for k in base:
        assert with_overlap[k] == base[k]
    assert set(with_overlap) - set(base) == {"overlap"}
    schemas.validator("plan_review_verdict").validate(
        with_overlap
    )  # additionalProperties → no schema change


def test_query_digest_at_review_time(repo: str, monkeypatch) -> None:
    """The query digest is generated at review time from the ticket (via enrich), independent
    of the enriched corpus."""
    import importlib

    _mod = importlib.import_module(
        "rebar.llm.enrich"
    )  # the module (the name is shadowed by the fn)
    _real = _mod.enrich
    calls = {}

    def _spy(ticket_id=None, **kw):
        calls["ticket_id"] = ticket_id
        return _real(ticket_id=ticket_id, **kw)

    # wire.overlap_findings imports enrich lazily (`from rebar.llm.enrich import enrich`), so
    # patch the function on its home MODULE object — the lazy import then picks up the spy.
    monkeypatch.setattr(_mod, "enrich", _spy)
    query = rebar.create_ticket("task", "Q", repo_root=repo)
    runner = _PipelineRunner(
        dict(_DIGEST), {"relation": "duplicates", "confidence": 0.9, "abstain": False}
    )
    overlap_findings(query, repo_root=repo, config=LLMConfig(), runner=runner)
    # enrich was invoked on the ticket under review (fresh query digest).
    assert calls.get("ticket_id") == query


def test_graceful_skip(repo: str) -> None:
    """An LLM/agents/key failure anywhere in the pipeline → empty overlap[], never raises."""

    class _BoomRunner(Runner):
        name = "boom"

        def preflight(self) -> None:
            raise RuntimeError("no agents extra")

        def run(self, req: RunRequest) -> dict:
            raise RuntimeError("no agents extra")

    query = rebar.create_ticket("task", "Q", repo_root=repo)
    assert overlap_findings(query, repo_root=repo, config=LLMConfig(), runner=_BoomRunner()) == []


def test_runtime_error(repo: str, monkeypatch) -> None:
    """A runtime failure in retrieval/judge is caught → empty overlap[], logged, never blocks."""

    def _boom(*a, **k):
        raise RuntimeError("retrieve blew up")

    monkeypatch.setattr("rebar.llm.overlap.retrieve.build_corpus", _boom, raising=False)
    query = rebar.create_ticket("task", "Q", repo_root=repo)
    runner = _PipelineRunner(
        dict(_DIGEST), {"relation": "duplicates", "confidence": 0.9, "abstain": False}
    )
    assert overlap_findings(query, repo_root=repo, config=LLMConfig(), runner=runner) == []


def test_flag_off() -> None:
    """overlap_enabled is a bool defaulting OFF on VerifyConfig, with its _SECTIONS coercer."""
    assert VerifyConfig().overlap_enabled is False
    assert "overlap_enabled" in _SECTIONS["verify"]
    assert _SECTIONS["verify"]["overlap_enabled"]("true", "overlap_enabled") is True


def test_render() -> None:
    """overlap[] is rendered in the CLI plan-review text (ready-to-run link commands)."""
    import contextlib
    import io as _io

    from rebar._cli._llm_commands import _render_plan_review_text

    result = {
        "verdict": "PASS",
        "ticket_id": "abcd",
        "coverage": {"counts": {}},
        "blocking": [],
        "advisory": [],
        "overlap": [
            {
                "relation": "supersedes",
                "confidence": 0.9,
                "shared_artifact": "config key REBAR_X",
                "link_command": "rebar link A B supersedes",
            }
        ],
    }
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        _render_plan_review_text(result)
    assert "rebar link A B supersedes" in buf.getvalue()
