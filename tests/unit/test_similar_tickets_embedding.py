"""The opt-in embedding tier: dispatch, invalidation, degrade, and the explicit build.

Ticket 2041-75d6-2a2f-4be9, implementing ADR 0122's adopted ``vector`` candidate. The
properties under test are the ones the design rests on rather than the mechanics of
cosine arithmetic:

* **Vendor neutrality.** Both model choices are provider-qualified strings resolved by
  ``pydantic-ai``'s own inference, and no vendor name is branched on anywhere in the
  retrieval or digest path. An absent optional backend is a degrade naming the missing
  group, not a traceback.
* **Degrade, never fail.** Unconfigured, backend absent, provider denying, corpus not
  fully covered -- every one of them serves the lexical tier's results. The caller
  cannot tell them apart and does not need to; the distinction is in the diagnostic.
* **Full current-key coverage or nothing.** A vector is only comparable under the key
  it was produced with, so a partial corpus and an interrupted re-embed both degrade
  rather than rank across two embedding spaces.
* **The create path pays nothing.** ``catch_up`` runs inside a user's create, so it
  never digests and never embeds, whatever share of the corpus lacks vectors.

The embedding provider is injected through the ONE seam
``rebar.similar.embedding._embedding_model``, which is also what pydantic-ai's own
``infer_embedding_model`` offers: it returns an ``EmbeddingModel`` instance unchanged.
:class:`TestEmbeddingModel` below is a deterministic bag-of-terms model, so a ranking
assertion is a statement about the ordering rather than about a provider's weights.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.embeddings.base import EmbeddingModel
from pydantic_ai.embeddings.result import EmbeddingResult

from rebar.similar import embedding as rebar_embedding
from rebar.similar import find_similar
from rebar.similar.catchup import catch_up
from rebar.similar.embedding import (
    DIGEST_FIELDS,
    DIGEST_PROMPT_COMMIT,
    build_vectors,
    digest_model_name,
    embedding_model_name,
    recover_digest,
)
from rebar.similar.index import open_index, store_vector, validity_key

pytestmark = pytest.mark.unit

_TS = 1793585542519320001

#: The provider-qualified strings the dispatch tests exercise. Two DISTINCT vendors is
#: the point: one would prove only that a string was accepted.
_DISPATCH_CASES = (
    ("bedrock:amazon.titan-embed-text-v2:0", "BedrockEmbeddingModel"),
    ("openai:text-embedding-3-small", "OpenAIEmbeddingModel"),
)

#: Vendor names that must not appear in any conditional in the retrieval or digest
#: path. Declared in the test rather than in the module so the module cannot satisfy
#: the rule by narrowing its own list.
_VENDOR_NAMES = (
    "anthropic",
    "bedrock",
    "cohere",
    "google",
    "mistral",
    "openai",
    "titan",
    "voyage",
)


class TestEmbeddingModel(EmbeddingModel):
    """A deterministic bag-of-terms embedding model.

    Each text becomes its term counts over a fixed vocabulary, so cosine similarity is
    ordinary term overlap and an ordering assertion means what it says. Counts calls so
    a test can assert that NO provider call was made.
    """

    # Keeps pytest from collecting this as a test class, the same guard pydantic-ai's
    # own TestEmbeddingModel uses.
    __test__ = False

    def __init__(self, vocabulary: tuple[str, ...]) -> None:
        self._vocabulary = vocabulary
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def model_name(self) -> str:
        return "deterministic"

    @property
    def system(self) -> str:
        return "stub"

    async def embed(self, inputs, *, input_type, settings=None) -> EmbeddingResult:
        texts = [inputs] if isinstance(inputs, str) else list(inputs)
        self.calls.append((input_type, tuple(texts)))
        vectors = [[float(text.lower().count(term)) for term in self._vocabulary] for text in texts]
        return EmbeddingResult(
            embeddings=vectors,
            inputs=texts,
            input_type=input_type,
            model_name=self.model_name,
            provider_name=self.system,
        )


# --- store fixtures ------------------------------------------------------------------


def _event(ticket_dir: Path, event_type: str, data: dict, *, ts: int) -> None:
    event_uuid = str(uuid.uuid4())
    payload = {
        "uuid": event_uuid,
        "env_id": "test-env",
        "timestamp": ts,
        "event_type": event_type,
        "author": "t",
        "author_email": "t@e.com",
        "data": data,
    }
    (ticket_dir / f"{ts}-{event_uuid}-{event_type}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_ticket(tracker: Path, tid: str, title: str, description: str = "") -> Path:
    ticket_dir = tracker / tid
    ticket_dir.mkdir(parents=True)
    _event(
        ticket_dir,
        "CREATE",
        {
            "id": tid,
            "alias": f"alias-for-{tid}",
            "title": title,
            "description": description,
            "ticket_type": "task",
            "priority": 2,
            "tags": [],
            "parent_id": "",
            "creation_channel": "python",
        },
        ts=_TS,
    )
    return ticket_dir


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """An empty store directory. The index lands in its sibling ``.rebar``."""
    path = tmp_path / "store" / ".tickets-tracker"
    path.mkdir(parents=True)
    return path


def _unconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default install: neither key set, from whatever the host's shell exports."""
    monkeypatch.delenv("REBAR_LLM_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("REBAR_LLM_SIMILAR_DIGEST_MODEL", raising=False)


def _digest_json(*, propositions: list[str], area: str = "retrieval") -> str:
    return json.dumps(
        {
            "propositions": propositions,
            "key_entities": [],
            "problem_keywords": propositions,
            "component_or_area": area,
        }
    )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: str = "stub:deterministic",
    digest: str = "stub:digest",
) -> None:
    monkeypatch.setenv("REBAR_LLM_EMBEDDING_MODEL", embedding)
    monkeypatch.setenv("REBAR_LLM_SIMILAR_DIGEST_MODEL", digest)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    vocabulary: tuple[str, ...],
    digest_for,
) -> TestEmbeddingModel:
    """Wire the deterministic embedding model and a digest callable through the seams."""
    model = TestEmbeddingModel(vocabulary)
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", lambda _name: model)
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply", lambda _name, text: digest_for(text)
    )
    return model


def _ids(results: list[dict]) -> list[str]:
    return [row["id"] for row in results]


# --- dispatch ------------------------------------------------------------------------


def test_a_provider_qualified_string_cosine_ranks_the_vectors(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1. The configured string drives retrieval, and the ORDERING is the cosine one.

    The two candidates carry the same title, so lexical ranking cannot separate them.
    Their digests do separate them, which is the whole claim the tier makes.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "1111-1111-1111-1111 detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "quota handling", "2222-2222-2222-2222 detail")
    propositions = {
        "1111-1111-1111-1111": ["throttle", "retry"],
        "2222-2222-2222-2222": ["checkout", "invoice"],
    }
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch,
        ("throttle", "retry", "checkout", "invoice"),
        lambda text: _digest_json(
            propositions=propositions.get(_tid_for(text, propositions)) or text.split()
        ),
    )
    assert build_vectors(str(tracker))["status"] == "complete"

    results = find_similar(str(tracker), text="throttle retry")
    assert _ids(results)[0] == "1111-1111-1111-1111"
    assert results[0]["score"] > results[1]["score"]


def test_the_query_is_digested_before_it_is_embedded(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0122's adopted ``vector`` candidate is symmetric: both sides are digests.

    Embedding raw query text against a digest-embedded corpus is an unmeasured hybrid
    of ``vector`` and the rejected ``vector-raw``, so the digest running on the query
    is a correctness property, not an optimization. The stub records what reached the
    digest and what reached the embedder, which is the only way to tell the symmetric
    implementation from the hybrid that also returns plausible-looking results.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    digested: list[str] = []
    embedded: list[str] = []
    _configure(monkeypatch)
    model = TestEmbeddingModel(("throttle",))
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", lambda _name: model)

    def _digest(_name: str, text: str) -> str:
        digested.append(text)
        return _digest_json(propositions=["throttle"])

    monkeypatch.setattr("rebar.similar.embedding._digest_reply", _digest)
    real_embed = rebar_embedding._embed

    def _spy(name: str, texts: list[str], *, as_query: bool) -> list[list[float]]:
        embedded.extend(texts)
        return real_embed(name, texts, as_query=as_query)

    monkeypatch.setattr("rebar.similar.embedding._embed", _spy)
    assert build_vectors(str(tracker))["status"] == "complete"
    digested.clear()
    embedded.clear()

    assert _ids(find_similar(str(tracker), text="quota throttle")) == ["1111-1111-1111-1111"]
    assert digested == ["quota throttle"], "the raw query text must reach the digest"
    expected = rebar_embedding.digest_paragraph(
        rebar_embedding.recover_digest(_digest_json(propositions=["throttle"]))
    )
    assert embedded == [expected], "the DIGEST paragraph, not the raw query, is embedded"
    assert "quota" not in expected, "the discriminator: a raw-query word absent from the digest"


def test_a_query_digest_that_fails_degrades_rather_than_ranking_on_raw_text(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query the digest cannot reduce must NOT fall back to embedding the raw text.

    Ranking a digest-embedded corpus against a raw-text query vector is the hybrid
    this tier does not implement, so the only safe handling is the ordinary degrade.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "unrelated", "checkout detail")
    _configure(monkeypatch)
    replies = iter([_digest_json(propositions=["throttle"]), _digest_json(propositions=["x"])])
    _install_stubs(monkeypatch, ("throttle", "x"), lambda _text: next(replies, "still prose"))
    assert build_vectors(str(tracker))["status"] == "complete"

    results = find_similar(str(tracker), text="quota throttle")
    assert results, "the lexical tier is the floor; it must still serve"
    assert all("score" in row for row in results)


def test_the_query_resolves_the_digest_model_exactly_as_the_build_does(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``similar_digest_model`` unset, BOTH sides fall back to ``embedding_model``.

    The two resolutions are separate call sites, so they can drift. If the query
    required the key the build treats as optional, an install relying on the fallback
    would build a complete corpus and then degrade on every single query -- a failure
    that looks like a provider problem and is invisible to the build's own status.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    monkeypatch.setenv("REBAR_LLM_EMBEDDING_MODEL", "stub:deterministic")
    monkeypatch.delenv("REBAR_LLM_SIMILAR_DIGEST_MODEL", raising=False)
    assert digest_model_name() is None
    seen: list[str] = []

    model = TestEmbeddingModel(("throttle",))
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", lambda _name: model)
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply",
        lambda name, _text: seen.append(name) or _digest_json(propositions=["throttle"]),
    )
    assert build_vectors(str(tracker))["status"] == "complete"
    seen.clear()
    assert _ids(find_similar(str(tracker), text="quota throttle")) == ["1111-1111-1111-1111"]
    assert seen == ["stub:deterministic"], (
        "the QUERY must digest, under the same fallback the build used; an empty list "
        "means it degraded to lexical, which returns this same single result"
    )


def test_a_ranked_query_excludes_the_requested_id(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``exclude_id`` keeps a ticket out of its OWN results on the embedding tier.

    The create path passes the new ticket's id, and catch-up has already indexed it,
    so without this it ranks as its own top match. The lexical tier has its own
    exclusion; this asserts the embedding tier's, which is a separate code path.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "quota handling", "throttle detail too")
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch, ("throttle",), lambda _text: _digest_json(propositions=["throttle"])
    )
    assert build_vectors(str(tracker))["status"] == "complete"

    unfiltered = _ids(find_similar(str(tracker), text="quota throttle"))
    assert set(unfiltered) == {"1111-1111-1111-1111", "2222-2222-2222-2222"}
    filtered = _ids(
        find_similar(str(tracker), text="quota throttle", exclude_id="1111-1111-1111-1111")
    )
    assert filtered == ["2222-2222-2222-2222"]


def _tid_for(text: str, by_ticket: dict[str, list[str]]) -> str | None:
    """The ticket id written into the description, so a stub digest can vary per ticket.

    ``None`` for text carrying no known id -- which is what a QUERY looks like, since
    the query is digested through this same stub.
    """
    for ticket_id in by_ticket:
        if ticket_id in text:
            return ticket_id
    return None


@pytest.mark.parametrize(("model_string", "expected_class"), _DISPATCH_CASES)
def test_a_string_for_two_distinct_providers_resolves_to_each_providers_model(
    model_string: str, expected_class: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2. Two vendors, one code path, resolved by pydantic-ai's own inference."""
    from rebar.similar.embedding import _embedding_model

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    assert type(_embedding_model(model_string)).__name__ == expected_class


def test_no_vendor_name_appears_in_a_conditional_in_the_retrieval_or_digest_path() -> None:
    """AC3. A vendor branch is how a 'provider-agnostic' seam quietly stops being one."""
    import rebar.similar.embedding as embedding_module
    import rebar.similar.index as index_module
    import rebar.similar.lexical as lexical_module

    offenders: list[str] = []
    for module in (embedding_module, lexical_module, index_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            tests = []
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                tests.append(node.test)
            elif isinstance(node, ast.Compare):
                tests.append(node)
            for test in tests:
                for inner in ast.walk(test):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        lowered = inner.value.lower()
                        offenders += [
                            f"{module.__name__}:{inner.lineno} {vendor}"
                            for vendor in _VENDOR_NAMES
                            if vendor in lowered
                        ]
    assert offenders == []


def test_the_digest_model_resolves_from_a_provider_qualified_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4. The digest model is chosen independently, and not from a Bedrock string."""
    from rebar.similar.embedding import _digest_model

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    # The explicit `-chat` form, so this test does not depend on what a bare `openai:`
    # prefix will mean after pydantic-ai v2 changes its default.
    _configure(monkeypatch, digest="openai-chat:gpt-4o-mini")
    assert digest_model_name() == "openai-chat:gpt-4o-mini"
    resolved = _digest_model("openai-chat:gpt-4o-mini")
    assert resolved.model_name == "gpt-4o-mini"
    assert resolved.system == "openai"


# --- invalidation --------------------------------------------------------------------


def _build_one(tracker: Path, monkeypatch: pytest.MonkeyPatch) -> TestEmbeddingModel:
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    model = _install_stubs(
        monkeypatch,
        ("throttle", "retry"),
        lambda _text: _digest_json(propositions=["throttle"]),
    )
    assert build_vectors(str(tracker))["status"] == "complete"
    return model


def test_changing_the_embedding_model_invalidates_the_affected_vectors(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5. A vector from another model's space is not a stale vector; it is a wrong one."""
    model = _build_one(tracker, monkeypatch)
    embedded_before = len(model.calls)

    _configure(monkeypatch, embedding="stub:other-model")
    assert build_vectors(str(tracker))["vectored"] == 1
    assert len(model.calls) > embedded_before


def test_changing_the_pinned_digest_prompt_commit_invalidates_them_likewise(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6. The prompt decides the text that was embedded, so it belongs in the key."""
    model = _build_one(tracker, monkeypatch)
    embedded_before = len(model.calls)

    monkeypatch.setattr("rebar.similar.embedding.DIGEST_PROMPT_COMMIT", "0000000000a")
    assert build_vectors(str(tracker))["vectored"] == 1
    assert len(model.calls) > embedded_before


# --- degrade -------------------------------------------------------------------------


def test_a_query_without_full_current_key_coverage_degrades(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7. Ranking a partial corpus would drop the unreached tickets invisibly."""
    for index in range(3):
        _write_ticket(tracker, f"{index}{index}{index}{index}-1111-1111-1111", "quota", "throttle")
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch, ("throttle",), lambda _text: _digest_json(propositions=["throttle"])
    )
    partial = build_vectors(str(tracker), limit=1)
    assert partial["status"] == "partial"
    assert partial["remaining"] == 2

    results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))


def _lexical_results(tracker: Path, text: str) -> list[dict]:
    """The lexical tier's own answer, as the comparison every degrade is measured against."""
    from rebar.similar.lexical import search

    conn = open_index(tracker)
    assert conn is not None
    try:
        catch_up(tracker, conn)
        return search(conn, text)
    finally:
        conn.close()


def test_an_interrupted_reembed_never_ranks_across_two_embedding_spaces(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8. Mixed spaces would rank, and rank wrongly, with no symptom at all."""
    for index in range(3):
        _write_ticket(tracker, f"{index}{index}{index}{index}-1111-1111-1111", "quota", "throttle")
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch, ("throttle",), lambda _text: _digest_json(propositions=["throttle"])
    )
    assert build_vectors(str(tracker))["status"] == "complete"

    _configure(monkeypatch, embedding="stub:second-space")
    assert build_vectors(str(tracker), limit=1)["status"] == "partial"

    results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))


def test_with_embedding_model_unset_retrieval_degrades(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC9. The default install has no provider, and must still get the advisory."""
    _unconfigure(monkeypatch)
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))
    assert results


def test_both_keys_are_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10. ADR 0122's measured models are a recommendation, never an applied default."""
    _unconfigure(monkeypatch)
    assert embedding_model_name() is None
    assert digest_model_name() is None


def test_an_absent_optional_backend_degrades_naming_the_missing_group(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC11. A backend behind an optional group is an expected configuration, not a bug."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch, embedding="cohere:embed-v4.0", digest="cohere:embed-v4.0")
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply",
        lambda _name, _text: _digest_json(propositions=["throttle"]),
    )

    outcome = build_vectors(str(tracker))
    assert outcome["status"] == "unavailable"
    assert "cohere" in outcome["reason"]
    assert "optional group" in outcome["reason"]

    with caplog.at_level("WARNING"):
        results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))


@pytest.mark.parametrize("failure", [PermissionError("denied"), TimeoutError("timed out")])
def test_a_provider_denial_or_timeout_degrades(
    tracker: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    """AC12. The two failures an operator actually hits, and neither reaches the caller."""
    model = _build_one(tracker, monkeypatch)
    assert model.calls

    def _refuse(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        raise failure

    monkeypatch.setattr("rebar.similar.embedding._embed", _refuse)
    with caplog.at_level("WARNING"):
        results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))
    assert "degraded to lexical" in caplog.text


# --- digest failure accounting -------------------------------------------------------

#: ADR 0122's three observed reply shapes, each carrying a digest whose fields are all
#: empty. Recovery succeeds for all three -- which is the point: without the counter
#: they are indistinguishable from a successful digest.
_EMPTY_SHAPES = (
    '{"ticket_digest": {"propositions": [], "key_entities": [], '
    '"problem_keywords": [], "component_or_area": ""}}',
    "propositions: []\nkey_entities: []\nproblem_keywords: []\ncomponent_or_area: ''\n",
    'Here is the digest you asked for:\n\n{"propositions": [], "key_entities": [], '
    '"problem_keywords": [], "component_or_area": ""}\n\nLet me know if you need more.',
)


def test_an_all_empty_digest_mapping_degrades(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC13. A digest with nothing in it cannot be embedded into a meaningful vector."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    _install_stubs(monkeypatch, ("throttle",), lambda _text: _EMPTY_SHAPES[0])

    outcome = build_vectors(str(tracker))
    assert outcome["empties"] == 1
    assert outcome["status"] == "partial"
    results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))


def test_empties_counts_every_all_empty_recovery(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC14. All three of ADR 0122's shapes recover, and all three must be counted."""
    for index, _shape in enumerate(_EMPTY_SHAPES):
        _write_ticket(tracker, f"{index}{index}{index}{index}-1111-1111-1111", "quota", "throttle")
    _configure(monkeypatch)
    replies = iter(_EMPTY_SHAPES)
    _install_stubs(monkeypatch, ("throttle",), lambda _text: next(replies))

    assert build_vectors(str(tracker))["empties"] == 3


def test_a_build_completes_despite_all_empty_digests(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC15. One ticket's failed digest must not abandon the rest of the corpus."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "1111-1111-1111-1111 detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "retry policy", "2222-2222-2222-2222 detail")
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch,
        ("throttle", "retry"),
        lambda text: (
            _EMPTY_SHAPES[0]
            if "1111-1111-1111-1111" in text
            else _digest_json(propositions=["retry"])
        ),
    )

    outcome = build_vectors(str(tracker))
    assert outcome["empties"] == 1
    assert outcome["vectored"] == 1


def test_an_unrecoverable_reply_is_retried_once(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC16. ADR 0122 saw this failure, found it nondeterministic, and retried."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    replies = iter(["I will start by refactoring the module.", _digest_json(propositions=["x"])])
    _install_stubs(monkeypatch, ("throttle",), lambda _text: next(replies))

    assert build_vectors(str(tracker))["digest_unparsed"] == 1


def test_a_retry_that_succeeds_counts_as_a_success(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17. The retry is the handling, so its success is a success."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    replies = iter(["prose, not a digest", _digest_json(propositions=["throttle"])])
    _install_stubs(monkeypatch, ("throttle",), lambda _text: next(replies))

    outcome = build_vectors(str(tracker))
    assert outcome["digest_failed"] == 0
    assert outcome["vectored"] == 1
    assert outcome["status"] == "complete"


def test_unrecoverable_on_both_attempts_increments_digest_failed(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC18. Two counters, because a retried failure and a final one are not the same."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    _install_stubs(monkeypatch, ("throttle",), lambda _text: "still prose")

    outcome = build_vectors(str(tracker))
    assert outcome["digest_failed"] == 1
    assert outcome["digest_unparsed"] == 2


def test_that_build_still_finishes_and_later_queries_degrade(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC19. One unvectorable ticket costs ranking, not the build and not the advisory."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "1111-1111-1111-1111 detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "retry policy", "2222-2222-2222-2222 detail")
    _configure(monkeypatch)
    _install_stubs(
        monkeypatch,
        ("throttle", "retry"),
        lambda text: (
            "prose" if "1111-1111-1111-1111" in text else _digest_json(propositions=["retry"])
        ),
    )

    outcome = build_vectors(str(tracker))
    assert outcome["vectored"] == 1
    assert outcome["status"] == "partial"
    results = find_similar(str(tracker), text="quota throttle")
    assert _ids(results) == _ids(_lexical_results(tracker, "quota throttle"))


# --- the create path -----------------------------------------------------------------


def test_catch_up_does_no_digest_or_embedding_work(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC20. Catch-up runs inside a user's create; a network call there is the hazard."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle")
    _configure(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply",
        lambda *_a, **_k: calls.append("digest") or "",
    )
    monkeypatch.setattr(
        "rebar.similar.embedding._embed", lambda *_a, **_k: calls.append("embed") or [[0.0]]
    )

    conn = open_index(tracker)
    assert conn is not None
    try:
        catch_up(tracker, conn)
    finally:
        conn.close()
    assert calls == []


@pytest.mark.parametrize("vectored_share", [0, 1, 3])
def test_create_path_cost_is_flat_in_the_unvectored_share(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, vectored_share: int
) -> None:
    """AC21. None, some and all unvectored must cost catch-up the same: zero calls."""
    ticket_ids = [f"{index}{index}{index}{index}-1111-1111-1111" for index in range(3)]
    for ticket_id in ticket_ids:
        _write_ticket(tracker, ticket_id, "quota handling", "throttle")
    _configure(monkeypatch)

    conn = open_index(tracker)
    assert conn is not None
    try:
        catch_up(tracker, conn)
        key = validity_key("stub:deterministic", DIGEST_PROMPT_COMMIT)
        for ticket_id in ticket_ids[:vectored_share]:
            store_vector(conn, ticket_id, key, [1.0, 0.0])

        calls: list[str] = []
        monkeypatch.setattr(
            "rebar.similar.embedding._digest_reply",
            lambda *_a, **_k: calls.append("digest") or "",
        )
        monkeypatch.setattr(
            "rebar.similar.embedding._embed", lambda *_a, **_k: calls.append("embed") or [[0.0]]
        )
        _touch(tracker, ticket_ids[0])
        catch_up(tracker, conn)
        assert calls == []
    finally:
        conn.close()


def _touch(tracker: Path, ticket_id: str) -> None:
    """Give one ticket a new event, so catch-up has real work to do."""
    _event(tracker / ticket_id, "EDIT", {"fields": {"title": "quota handling v2"}}, ts=_TS + 1000)


def test_a_rewritten_document_drops_its_vector(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored vector describes the text it was built from, so re-indexing invalidates it.

    Not an acceptance criterion of its own, but the mechanism AC21 depends on: catch-up
    stays flat BECAUSE it drops rather than re-embeds.
    """
    _build_one(tracker, monkeypatch)
    _touch(tracker, "1111-1111-1111-1111")

    conn = open_index(tracker)
    assert conn is not None
    try:
        catch_up(tracker, conn)
        assert conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    finally:
        conn.close()


# --- recovery chain ------------------------------------------------------------------


@pytest.mark.parametrize("reply", _EMPTY_SHAPES)
def test_every_observed_reply_shape_recovers_to_a_mapping(reply: str) -> None:
    """The chain exists because each of these was silently read as an empty digest."""
    recovered = recover_digest(reply)
    assert recovered is not None
    assert set(DIGEST_FIELDS) <= set(recovered)


def test_a_reply_carrying_no_digest_fields_is_unrecovered_not_empty() -> None:
    """Valid JSON answering a different question is a retry case, not an empties case."""
    assert recover_digest('{"summary": "I refactored the module instead."}') is None


# --- declared surface ----------------------------------------------------------------


def test_rebars_declared_dependencies_are_unchanged() -> None:
    """AC22. This tier reuses a runtime rebar already ships behind an optional extra.

    A core dependency on a model runtime would make the portable lexical tier require
    the very stack it exists to do without.
    """
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    core = " ".join(data["project"]["dependencies"]).lower()
    assert "pydantic-ai" not in core
    assert "pydantic_ai" not in core


def test_both_environment_variables_are_registered_in_the_generated_doc() -> None:
    """AC23. An undocumented env read is one an operator cannot discover."""
    registry = (Path(__file__).resolve().parents[2] / "docs" / "env-vars.md").read_text(
        encoding="utf-8"
    )
    assert "REBAR_LLM_EMBEDDING_MODEL" in registry
    assert "REBAR_LLM_SIMILAR_DIGEST_MODEL" in registry


def test_the_vectors_table_survives_an_index_reopen(tracker: Path) -> None:
    """Storage is part of the index schema, so the version bump must carry it."""
    conn = open_index(tracker)
    assert conn is not None
    try:
        store_vector(conn, "1111-1111-1111-1111", "key", [1.0, 2.0, 3.0])
    finally:
        conn.close()
    reopened = open_index(tracker)
    assert reopened is not None
    try:
        rows = reopened.execute("SELECT validity_key FROM vectors").fetchall()
        assert rows == [("key",)]
    finally:
        reopened.close()


def test_an_index_from_the_previous_version_is_discarded(tracker: Path) -> None:
    """The vectors table arrived with a schema bump; a v2 file must rebuild, not error."""
    conn = open_index(tracker)
    assert conn is not None
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'version'")
    conn.close()

    reopened = open_index(tracker)
    assert reopened is not None
    try:
        assert isinstance(reopened, sqlite3.Connection)
        assert reopened.execute("SELECT value FROM meta WHERE key='version'").fetchone()[0] == "3"
    finally:
        reopened.close()
