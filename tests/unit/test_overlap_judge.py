"""Unit tests for the Stage-2 pairwise overlap judge (epic only-crave-art, story 9022)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.overlap.judge import aggregate, judge
from rebar.llm.runner import Runner, RunRequest

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "overlap_pairs"


def _digest(area="auth", kw=None, ent=None, props=None) -> dict:
    return {
        "problem_keywords": kw or ["login"],
        "component_or_area": area,
        "key_entities": ent or ["SessionToken"],
        "propositions": props or ["login is broken"],
    }


def _v(rel, art="config-key REBAR_X", conf=0.9, abstain=False) -> dict:
    return {"relation": rel, "shared_artifact": art, "confidence": conf, "abstain": abstain}


class _SeqRunner(Runner):
    """A fake runner that returns canned structured verdicts in order (one per judge_one call).
    Lets a test drive the two orderings of a pair with DIFFERENT verdicts."""

    name = "seq"

    def __init__(self, verdicts: list[dict]):
        self._verdicts = list(verdicts)
        self._i = 0

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        v = self._verdicts[self._i]
        self._i += 1
        return dict(v)


def _cfg(**kw) -> LLMConfig:
    return LLMConfig(**kw)


def test_agree_surfaced() -> None:
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates"), _v("duplicates")]),
    )
    assert len(out) == 1
    assert out[0]["relation"] == "duplicates"
    assert out[0]["shared_artifact"]
    assert out[0]["link_command"].startswith("rebar link ")


def test_requires_artifact() -> None:
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates", None), _v("duplicates", None)]),
    )
    assert out == []


def test_disagree_downgrade() -> None:
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates"), _v("related_distinct")]),
    )
    assert out == []


def test_abstain_dropped() -> None:
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates"), _v("duplicates", abstain=True)]),
    )
    assert out == []


def test_low_confidence_downgrade() -> None:
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(overlap_conf_threshold=0.8),
        runner=_SeqRunner([_v("duplicates", conf=0.6), _v("duplicates", conf=0.6)]),
    )
    assert out == []


def test_directional_link_command() -> None:
    # "Q supersedes C" (ordering 1) with a consistent reverse ordering → surface Q→C.
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("supersedes"), _v("depends_on")]),
    )
    assert len(out) == 1
    assert out[0]["relation"] == "supersedes"
    assert out[0]["source_id"] == "Q" and out[0]["target_id"] == "C"
    assert out[0]["link_command"] == "rebar link Q C supersedes"

    # Contradiction: both orderings assert the SAME directional label → downgrade.
    out2 = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("supersedes"), _v("supersedes")]),
    )
    assert out2 == []


def test_directional_reverse_ordering() -> None:
    # Ordering-1 says related_distinct, ordering-2 says "C supersedes Q" → surface C→Q.
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("related_distinct"), _v("supersedes")]),
    )
    assert len(out) == 1
    assert out[0]["source_id"] == "C" and out[0]["target_id"] == "Q"
    assert out[0]["link_command"] == "rebar link C Q supersedes"


def test_duplicates_canonical() -> None:
    # duplicates uses a sorted-id canonical pair, deterministic regardless of query/cand order.
    out = judge(
        "zzz",
        _digest(),
        ["aaa"],
        {"aaa": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates"), _v("duplicates")]),
    )
    assert out[0]["source_id"] == "aaa" and out[0]["target_id"] == "zzz"
    assert out[0]["link_command"] == "rebar link aaa zzz duplicates"


def test_surface_cap() -> None:
    cands = ["c1", "c2", "c3", "c4", "c5"]
    corpus = {c: _digest() for c in cands}
    # Every pair agrees duplicates → 10 judge calls; cap the surfaced findings at 3.
    out = judge(
        "Q",
        _digest(),
        cands,
        corpus,
        config=_cfg(overlap_surface_cap=3),
        runner=_SeqRunner([_v("duplicates")] * 20),
    )
    assert len(out) == 3


def test_llm_error_abstain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A judge_one that raises internally is treated as abstain (no raise, no surface).
    def _boom(req):
        raise RuntimeError("llm down")

    class _BoomRunner(Runner):
        name = "boom"

        def preflight(self) -> None:
            pass

        def run(self, req: RunRequest) -> dict:
            raise RuntimeError("llm down")

    out = judge("Q", _digest(), ["C"], {"C": _digest()}, config=_cfg(), runner=_BoomRunner())
    assert out == []


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_LLM_OVERLAP_CONF_THRESHOLD", "0.55")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_SURFACE_CAP", "9")
    cfg = LLMConfig.from_env()
    assert cfg.overlap_conf_threshold == 0.55
    assert cfg.overlap_surface_cap == 9


def test_judge_precision_recall() -> None:
    fixtures = [json.loads(p.read_text()) for p in sorted(_FIXTURE_DIR.glob("*.json"))]
    assert len(fixtures) >= 12, f"expected >=12 labeled overlap pairs, got {len(fixtures)}"
    cfg = _cfg()
    tp = surfaced = gold_pos = 0
    for fx in fixtures:
        gold = fx["gold"]
        if gold is not None:
            gold_pos += 1
        finding = aggregate("Q", "C", fx["r1"], fx["r2"], cfg)
        predicted = finding["relation"] if finding else None
        if predicted is not None:
            surfaced += 1
            if predicted == gold:
                tp += 1
    precision = tp / surfaced if surfaced else 1.0
    recall = tp / gold_pos if gold_pos else 1.0
    assert precision >= 0.8, f"precision {precision:.2f} < 0.8"
    assert recall >= 0.6, f"recall {recall:.2f} < 0.6"


# ── AC-named proving tests (epic only-crave-art / 9022 acceptance criteria) ──────
def test_agree() -> None:
    """Judges both orderings and aggregates by the stated rule (both agree → surfaced)."""
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("duplicates"), _v("duplicates")]),
    )
    assert len(out) == 1 and out[0]["relation"] == "duplicates"


def test_cap() -> None:
    """At most overlap_surface_cap findings are surfaced."""
    cands = ["c1", "c2", "c3", "c4", "c5"]
    out = judge(
        "Q",
        _digest(),
        cands,
        {c: _digest() for c in cands},
        config=_cfg(overlap_surface_cap=3),
        runner=_SeqRunner([_v("duplicates")] * 20),
    )
    assert len(out) == 3


def test_link_command() -> None:
    """Each surfaced finding carries a valid, DIRECTED `rebar link` command."""
    out = judge(
        "Q",
        _digest(),
        ["C"],
        {"C": _digest()},
        config=_cfg(),
        runner=_SeqRunner([_v("supersedes"), _v("depends_on")]),
    )
    assert out[0]["link_command"] == "rebar link Q C supersedes"
    assert out[0]["relation"] in {"duplicates", "supersedes", "depends_on"}


def test_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """overlap_conf_threshold and overlap_surface_cap are read from the env by from_env."""
    monkeypatch.setenv("REBAR_LLM_OVERLAP_CONF_THRESHOLD", "0.55")
    monkeypatch.setenv("REBAR_LLM_OVERLAP_SURFACE_CAP", "9")
    cfg = LLMConfig.from_env()
    assert cfg.overlap_conf_threshold == 0.55 and cfg.overlap_surface_cap == 9
