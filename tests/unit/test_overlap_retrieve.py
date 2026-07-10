"""Unit tests for Stage-1 BM25F candidate generation + graph exclusion
(epic only-crave-art, story 5a8f).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.overlap import retrieve as R
from rebar.llm.overlap.graph import related_ticket_ids
from rebar.llm.overlap.retrieve import OverlapCandidate, retrieve


def _digest(keywords, area, entities, props) -> dict:
    return {
        "problem_keywords": keywords,
        "component_or_area": area,
        "key_entities": entities,
        "propositions": props,
    }


def test_topk_order() -> None:
    query = _digest(["login", "authentication"], "auth", ["SessionToken"], ["users cannot log in"])
    corpus = {
        "near": _digest(["login", "authentication"], "auth", ["SessionToken"], ["login is broken"]),
        "far": _digest(["billing", "invoice"], "payments", ["Invoice"], ["invoice math wrong"]),
        "mid": _digest(["login", "logout"], "auth", ["Session"], ["logout does not clear"]),
    }
    out = retrieve(query, corpus, exclude=set(), config=LLMConfig(overlap_k=20))
    ids = [c.ticket_id for c in out]
    assert ids[0] == "near"
    assert "far" not in ids  # no shared salient terms
    assert all(isinstance(c, OverlapCandidate) for c in out)


def test_topk_cap() -> None:
    query = _digest(["alpha"], "x", ["E"], ["p alpha"])
    corpus = {f"t{i}": _digest(["alpha"], "x", ["E"], ["p alpha"]) for i in range(30)}
    out = retrieve(
        query,
        corpus,
        exclude=set(),
        config=LLMConfig(overlap_k=5, overlap_min_should_match=0.1, overlap_max_doc_freq=1.0),
    )
    assert len(out) == 5


def test_perf() -> None:
    query = _digest(["overlap", "detection"], "gate", ["review_plan"], ["store-wide overlap"])
    corpus = {
        f"t{i}": _digest(
            ["overlap", "detection", f"kw{i % 7}"], "gate", [f"E{i % 5}"], [f"proposition {i}"]
        )
        for i in range(300)
    }
    start = time.perf_counter()
    retrieve(query, corpus, exclude=set(), config=LLMConfig())
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"retrieve took {elapsed_ms:.1f} ms (budget < 50 ms for 300 digests)"


def test_boilerplate() -> None:
    # A term present in nearly every doc (boilerplate) must not drive a match.
    query = _digest(["boiler"], "x", ["E"], ["boiler"])
    corpus = {f"t{i}": _digest(["boiler"], "x", ["E"], ["boiler"]) for i in range(10)}
    out = retrieve(query, corpus, exclude=set(), config=LLMConfig(overlap_max_doc_freq=0.5))
    assert out == []  # "boiler" is in 100% of docs → pruned → nothing to match


def test_floor_empty() -> None:
    query = _digest(["unique", "query", "terms"], "x", ["Q"], ["nothing shares this"])
    corpus = {"t": _digest(["completely", "different"], "y", ["Z"], ["unrelated entirely"])}
    out = retrieve(query, corpus, exclude=set(), config=LLMConfig(overlap_min_should_match=0.5))
    assert out == []


def test_exclude_set() -> None:
    query = _digest(["login"], "auth", ["S"], ["login broken"])
    corpus = {
        "keep": _digest(["login"], "auth", ["S"], ["login broken"]),
        "drop": _digest(["login"], "auth", ["S"], ["login broken"]),
    }
    out = retrieve(
        query,
        corpus,
        exclude={"drop"},
        config=LLMConfig(overlap_min_should_match=0.1, overlap_max_doc_freq=1.0),
    )
    ids = {c.ticket_id for c in out}
    assert "keep" in ids and "drop" not in ids


def test_error_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scoring error returns [] (never raises).
    monkeypatch.setattr(R, "_weighted_tf", lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
    assert (
        retrieve(_digest(["a"], "x", ["E"], ["p"]), {"t": _digest(["a"], "x", ["E"], ["p"])}, set())
        == []
    )


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_LLM_OVERLAP_K", "7")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_MAX_DOC_FREQ", "0.9")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_MIN_SHOULD_MATCH", "0.42")
    cfg = LLMConfig.from_env()
    assert cfg.overlap_k == 7
    assert cfg.overlap_max_doc_freq == 0.9
    assert cfg.overlap_min_should_match == 0.42


def test_field_weights_constant() -> None:
    # Field weights are a code constant, not a config knob; problem/area weighted high.
    assert R._FIELD_WEIGHTS["problem_keywords"] == 3.0
    assert R._FIELD_WEIGHTS["component_or_area"] == 3.0
    assert R._FIELD_WEIGHTS["key_entities"] == 1.0
    assert not hasattr(LLMConfig(), "overlap_field_weights")


# ── graph exclusion ───────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    rebar.init_repo(repo_root=str(r))
    return str(r)


def _tracker(repo: str) -> str:
    from rebar._commands._seam import tracker_dir

    return str(tracker_dir(repo))


def test_graph_exclusion(repo: str) -> None:
    epic = rebar.create_ticket("epic", "Epic", repo_root=repo)
    story = rebar.create_ticket("story", "Story", parent=epic, repo_root=repo)
    sib = rebar.create_ticket("story", "Sibling", parent=epic, repo_root=repo)
    task = rebar.create_ticket("task", "Task", parent=story, repo_root=repo)
    linked = rebar.create_ticket("task", "Linked", repo_root=repo)
    unrelated = rebar.create_ticket("task", "Unrelated", repo_root=repo)
    rebar.link(story, linked, "relates_to", repo_root=repo)

    related = related_ticket_ids(story, _tracker(repo))
    assert epic in related  # ancestor
    assert task in related  # descendant
    assert sib in related  # sibling
    assert linked in related  # linked (outgoing)
    assert unrelated not in related
    assert story not in related  # never itself


def test_graph_exclusion_incoming_link(repo: str) -> None:
    a = rebar.create_ticket("task", "A", repo_root=repo)
    b = rebar.create_ticket("task", "B", repo_root=repo)
    # b links to a (incoming from a's perspective) — a's own deps do not contain it.
    rebar.link(b, a, "relates_to", repo_root=repo)
    related = related_ticket_ids(a, _tracker(repo))
    assert b in related


def test_graph_exclusion_root_no_siblings(repo: str) -> None:
    root = rebar.create_ticket("epic", "Root", repo_root=repo)
    other = rebar.create_ticket("epic", "Other root", repo_root=repo)
    related = related_ticket_ids(root, _tracker(repo))
    assert other not in related  # two parent-less roots are NOT siblings


def test_cold_skip(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # build_corpus includes only present+fresh digests; absent/stale are skipped.
    from rebar.llm.overlap import digest_sidecar as ds

    monkeypatch.setattr(ds, "_active_model", lambda repo_root: "m")
    with_digest = rebar.create_ticket("task", "Has digest", repo_root=repo)
    _no_digest = rebar.create_ticket("task", "No digest", repo_root=repo)
    ds.emit(_digest(["k"], "a", ["E"], ["p one", "p two"]), with_digest, model="m", repo_root=repo)

    corpus = R.build_corpus(_tracker(repo), repo_root=repo)
    assert with_digest in corpus
    assert _no_digest not in corpus  # absent digest skipped
