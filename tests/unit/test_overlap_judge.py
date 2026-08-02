"""Unit tests for the Stage-2 pairwise overlap judge (epic only-crave-art, story 9022)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.overlap.judge import (
    _CANDIDATES_PER_CALL,
    _SHARED_DIGEST_REF,
    _digest_block,
    aggregate,
    judge,
    judge_batch,
    judge_one,
)
from rebar.llm.runner import Runner, RunRequest

_LABEL_RE = re.compile(r"\[candidate_id: ([^\]]+)\]")


def _labels(req: RunRequest) -> list[str]:
    """The candidate labels the judge put in this request, in the order it listed them."""
    return _LABEL_RE.findall(req.instructions)


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
    """A fake runner that hands out canned verdicts in order, ONE PER CANDIDATE, wrapped in the
    batched wire shape and labelled with the candidate ids the request actually asked about.

    Consuming per candidate rather than per call keeps a canned sequence meaning the same thing
    however the judge chunks its calls, so a test can drive the two orderings of a pair with
    DIFFERENT verdicts without encoding the batch size."""

    name = "seq"

    def __init__(self, verdicts: list[dict]):
        self._verdicts = list(verdicts)
        self._i = 0

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        entries = []
        for label in _labels(req):
            v = dict(self._verdicts[self._i])
            self._i += 1
            v["candidate_id"] = label
            entries.append(v)
        return {"verdicts": entries}


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


class _RecordingRunner(Runner):
    """A fake runner that keeps the ACTUAL ``RunRequest`` payload of every call.

    Lets a test assert on the request the judge really builds — the system/user split and the
    bytes in each — rather than on a stand-in that would read the same either way."""

    name = "recording"

    def __init__(self, verdicts: list[dict]):
        self._verdicts = list(verdicts)
        self._i = 0
        self.requests: list[RunRequest] = []

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        self.requests.append(req)
        labels = _labels(req)
        if not labels:  # a single-pair judge_one call — no labelled list to answer
            v = self._verdicts[self._i % len(self._verdicts)]
            self._i += 1
            return dict(v)
        entries = []
        for label in labels:
            v = dict(self._verdicts[self._i % len(self._verdicts)])
            self._i += 1
            v["candidate_id"] = label
            entries.append(v)
        return {"verdicts": entries}


def _judge_recording(query: dict, corpus: dict[str, dict]) -> _RecordingRunner:
    runner = _RecordingRunner([_v("related_distinct")])
    judge("Q", query, sorted(corpus), corpus, config=_cfg(), runner=runner)
    return runner


def test_shared_query_digest_lives_only_in_the_system_prompt() -> None:
    # The query digest is IDENTICAL on every call of a batch, so it belongs in the stable system
    # block (which carries the cache breakpoint), not re-sent in each volatile user message.
    query = _digest(area="overlap", ent=["QueryTicket"], props=["the query side"])
    corpus = {"C1": _digest(area="alpha", ent=["Alpha"]), "C2": _digest(area="beta", ent=["Beta"])}
    runner = _judge_recording(query, corpus)

    assert len(runner.requests) == 2  # one batch x two orderings
    systems = {r.system_prompt for r in runner.requests}
    assert len(systems) == 1, "the hoisted prefix must be byte-identical across the batch"
    query_block = _digest_block(query)
    assert query_block in systems.pop()
    assert [r.instructions for r in runner.requests if query_block in r.instructions] == []
    for req in runner.requests:
        for cand_id in ("C1", "C2"):
            assert _digest_block(corpus[cand_id]) in req.instructions


def test_hoisted_reference_keeps_the_ordered_pair_slots() -> None:
    # The prompt reads "FIRST <relation> SECOND" directionally, so the hoisted digest must still
    # be identified as the FIRST side in ordering 1 and the SECOND side in ordering 2 — batching
    # replaces the single digest on the OTHER side with a labelled list, never the slots.
    query = _digest(area="overlap", ent=["QueryTicket"])
    corpus = {"C1": _digest(area="alpha", ent=["Alpha"])}
    runner = _judge_recording(query, corpus)

    first_ordering, second_ordering = (r.instructions for r in runner.requests)
    listing = f"[candidate_id: C1]\n{_digest_block(corpus['C1'])}"
    assert first_ordering == f"FIRST:\n{_SHARED_DIGEST_REF}\n\nSECOND CANDIDATES:\n{listing}"
    assert second_ordering == f"FIRST CANDIDATES:\n{listing}\n\nSECOND:\n{_SHARED_DIGEST_REF}"


# ── batching: fewer calls, label-keyed demultiplexing, and the fail-safe (c403) ──


def _six() -> tuple[dict, dict[str, dict]]:
    """A query digest and a six-candidate corpus — the size the acceptance criteria name."""
    corpus = {f"C{i}": _digest(area=f"area{i}", ent=[f"Ent{i}"]) for i in range(1, 7)}
    return _digest(area="overlap", ent=["QueryTicket"]), corpus


class _CannedBatchRunner(Runner):
    """Returns a caller-supplied batched response verbatim, so a test can hand the judge a
    shuffled, incomplete, or malformed batch and see how it demultiplexes it."""

    name = "canned-batch"

    def __init__(self, response):
        self._response = response
        self.requests: list[RunRequest] = []

    def preflight(self) -> None:
        pass

    def run(self, req: RunRequest) -> dict:
        self.requests.append(req)
        return self._response


def test_batching_issues_fewer_calls_than_one_per_ordered_pair() -> None:
    # The whole point of the change: six candidates must cost fewer than 6 x 2 calls, and every
    # candidate's digest must still reach the model, each under its own label.
    query, corpus = _six()
    runner = _judge_recording(query, corpus)

    assert len(runner.requests) < 2 * len(corpus)
    assert len(runner.requests) == 2  # six fits in one batch, judged in both orderings
    for req in runner.requests:
        assert _labels(req) == sorted(corpus)
        for cand_id, digest in corpus.items():
            assert f"[candidate_id: {cand_id}]\n{_digest_block(digest)}" in req.instructions


def test_a_corpus_larger_than_the_bound_is_split_across_calls() -> None:
    # The batch is bounded, so a full Stage-1 candidate set becomes several calls per ordering —
    # and every candidate is still judged exactly once in each.
    n = _CANDIDATES_PER_CALL + 2
    corpus = {f"C{i:02d}": _digest(area=f"area{i}") for i in range(n)}
    runner = _judge_recording(_digest(area="overlap"), corpus)

    assert len(runner.requests) == 4  # two batches x two orderings
    judged = [label for req in runner.requests for label in _labels(req)]
    assert sorted(judged) == sorted(list(corpus) * 2)
    assert all(len(_labels(req)) <= _CANDIDATES_PER_CALL for req in runner.requests)


def test_batch_verdicts_are_matched_by_label_not_by_position() -> None:
    # A batched response may come back in any order; keying by position would silently attribute
    # one candidate's verdict to another, which is the precision failure batching risks.
    query = _digest(area="overlap")
    corpus = {"C1": _digest(area="alpha"), "C2": _digest(area="beta")}
    reversed_entries = {
        "verdicts": [
            dict(_v("related_distinct"), candidate_id="C2"),
            dict(_v("duplicates"), candidate_id="C1"),
        ]
    }
    runner = _CannedBatchRunner(reversed_entries)
    out = judge("Q", query, sorted(corpus), corpus, config=_cfg(), runner=runner)
    assert [f["counterpart_id"] for f in out] == ["C1"]
    assert out[0]["relation"] == "duplicates"


def test_a_missing_or_unknown_batch_entry_abstains_only_that_candidate() -> None:
    # A candidate the batch failed to answer for, and an entry naming an id we never sent, must
    # both degrade to an abstain for the affected candidate ALONE — the rest keep their verdicts.
    query = _digest(area="overlap")
    corpus = {"C1": _digest(area="alpha"), "C2": _digest(area="beta")}
    partial = {
        "verdicts": [
            dict(_v("duplicates"), candidate_id="C1"),
            dict(_v("duplicates"), candidate_id="GHOST"),
        ]
    }
    pairs = [(cid, corpus[cid]) for cid in sorted(corpus)]
    verdicts = judge_batch(query, pairs, _cfg(), _CannedBatchRunner(partial), shared_side="first")
    # Exactly the candidates asked about, no more: GHOST is dropped rather than carried.
    assert set(verdicts) == {"C1", "C2"}
    assert verdicts["C1"]["relation"] == "duplicates"
    assert verdicts["C2"]["abstain"] is True

    out = judge(
        "Q", query, sorted(corpus), corpus, config=_cfg(), runner=_CannedBatchRunner(partial)
    )
    assert [f["counterpart_id"] for f in out] == ["C1"]  # C2 (unanswered) abstained away


def test_a_duplicate_batch_entry_does_not_overwrite_the_first() -> None:
    # One question, one answer: a second entry for an id already read is dropped rather than
    # allowed to revise a verdict the aggregator has already been promised.
    query = _digest(area="overlap")
    corpus = {"C1": _digest(area="alpha")}
    doubled = {
        "verdicts": [
            dict(_v("duplicates"), candidate_id="C1"),
            dict(_v("unrelated", art=None), candidate_id="C1"),
        ]
    }
    out = judge(
        "Q", query, sorted(corpus), corpus, config=_cfg(), runner=_CannedBatchRunner(doubled)
    )
    assert len(out) == 1 and out[0]["relation"] == "duplicates"


@pytest.mark.parametrize(
    "response",
    [
        {},  # no verdict list at all
        {"verdicts": None},  # present but not a list
        {"verdicts": "duplicates"},  # a string, not a list of entries
        # A fully surfaceable verdict that names no candidate: it must not be spread over the
        # batch, because there is no way to know which candidate it was about.
        {"verdicts": [_v("duplicates")]},
        {"verdicts": ["duplicates"]},  # entries that are not objects
    ],
)
def test_a_malformed_batch_response_surfaces_nothing(response) -> None:
    # The step is ADVISORY: a batch the judge cannot read is an abstain for every candidate in
    # it, never an exception and never a guessed relation.
    query, corpus = _six()
    out = judge(
        "Q", query, sorted(corpus), corpus, config=_cfg(), runner=_CannedBatchRunner(response)
    )
    assert out == []


def test_a_raising_runner_surfaces_nothing_for_a_whole_batch() -> None:
    # One failed batch call must not fail the review, and must not partially surface.
    class _BoomRunner(Runner):
        name = "boom"

        def preflight(self) -> None:
            pass

        def run(self, req: RunRequest) -> dict:
            raise RuntimeError("llm down")

    query, corpus = _six()
    assert judge("Q", query, sorted(corpus), corpus, config=_cfg(), runner=_BoomRunner()) == []


def test_judge_one_default_shape_is_unchanged() -> None:
    # Omitting the hoist reproduces the pre-change request bytes exactly, so no other caller of
    # judge_one changes behaviour.
    first, second = _digest(area="alpha"), _digest(area="beta")
    runner = _RecordingRunner([_v("related_distinct")])
    judge_one(first, second, _cfg(), runner)

    req = runner.requests[0]
    assert req.instructions == f"FIRST:\n{_digest_block(first)}\n\nSECOND:\n{_digest_block(second)}"
    assert _digest_block(first) not in req.system_prompt
    assert _SHARED_DIGEST_REF not in req.system_prompt


def test_missing_corpus_digest_makes_no_call() -> None:
    # A candidate absent from the corpus is skipped outright — the advisory step never pays for a
    # call it cannot ground, and nothing propagates to the caller.
    runner = _RecordingRunner([_v("duplicates")])
    out = judge("Q", _digest(), ["missing"], {}, config=_cfg(), runner=runner)
    assert out == []
    assert runner.requests == []
