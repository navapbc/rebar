"""ADR 0122's embedding tier: digest each ticket, embed the digest, cosine-rank.

This is an OPT-IN RANKING UPGRADE over the lexical tier, not a replacement for it.
The lexical tier is what makes retrieval portable -- no provider, no credentials, no
network -- and it stays the default. This module replaces the scoring function when,
and only when, an operator has configured an embedding model AND every indexed ticket
carries a vector in that model's space. In every other circumstance it DEGRADES:
:func:`rank` returns ``None`` and the caller serves the lexical results.

Three properties are load-bearing, and each is a constraint rather than an
implementation detail.

**Catch-up never digests, and the corpus build is never implicit.** ``find_similar``
calls ``catch_up`` inline, and ticket creation calls ``find_similar``, so anything
catch-up does runs inside a user's create. One ``stat`` per directory belongs there;
digesting and embedding every ticket whose text moved does not. So catch-up drops the
vector of a changed ticket and moves on, and never calls :func:`build_vectors`. The
build is separate and explicit, invoked by a caller that has decided to pay for it.
Until it has covered the corpus, queries degrade rather than silently building it.

**The query is digested exactly as the corpus was.** ADR 0122 adopted the ``vector``
candidate, and that candidate is SYMMETRIC: its query vector and its corpus vectors
are both embeddings of the LLM digest paragraph -- ``evals/similar-tickets/measure.py``
ranks with ``vectors.get(source)`` drawn from the same digest-vector map it scores
against. Embedding raw query text against a digest-embedded corpus is neither
``vector`` nor the rejected ``vector-raw`` diagnostic but an unmeasured hybrid of the
two, so it carries none of the recall ADR 0122 established and would make the tier's
whole justification inapplicable to the thing actually shipped. Ranking therefore
spends a digest call on the query. That cost lands only where an operator configured
an embedding model and paid to build a covering corpus -- which is a digest call per
ticket already -- and a digest that fails degrades to lexical like any other
unavailability, so the create it runs under is never failed by it.

**No vendor name is branched on.** Both model choices are provider-qualified strings
resolved through ``pydantic_ai``'s own ``infer_embedding_model`` / ``infer_model``,
which is the same dispatch shape ``rebar.llm.providers`` already uses. ADR 0122's
measured models are a recommendation recorded in the docs; neither config key ships a
default, so an install that configures nothing gets the lexical tier.

**A missing optional backend is expected, not a defect.** ``pydantic-ai-slim`` reaches
rebar through the ``[agents]`` extra and its per-vendor backends sit behind optional
groups that raise ``ImportError`` naming the group. That is a degrade with a
diagnostic naming the group, never a traceback reaching the caller.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from typing import Any

from .index import current_vectors, store_vector, unvectored_ids, validity_key

logger = logging.getLogger(__name__)

#: The commit the 4-field digest prompt below is pinned at (ADR 0122). It is part of
#: the validity key, so editing the prompt text WITHOUT moving this constant would
#: leave vectors describing text that is no longer produced. Move both together.
DIGEST_PROMPT_COMMIT = "f147e08242f"

#: ADR 0122's measured input cap. Ticket text beyond it is truncated, not rejected.
DIGEST_INPUT_CAP = 40_000

#: The four fields the digest carries, in the order they are concatenated into the
#: paragraph that is embedded.
DIGEST_FIELDS = ("propositions", "key_entities", "problem_keywords", "component_or_area")

#: Wrapper keys a reply may nest the digest under. ADR 0122 observed ``ticket_digest``
#: in the wild; any single-key mapping is unwrapped, so this list is a label for the
#: expected case rather than the mechanism.
_WRAPPER_KEYS = ("ticket_digest", "digest", "result", "output")

#: The 4-field digest prompt of :data:`DIGEST_PROMPT_COMMIT`, vendored here because
#: the file it lived in was deleted with the overlap subsystem. It is that prompt with
#: FOUR deliberate deviations, recorded because an undisclosed edit to a prompt whose
#: output is embedded is indistinguishable from the measured one:
#:
#: 1. The clause "WITHOUT embeddings" is dropped. It described the retired lexical
#:    overlap op and is false here -- this output is embedded.
#: 2. The parenthetical about the op post-validating the proposition count is dropped.
#:    It referred to that same deleted op, so it instructed against nothing.
#: 3. Em-dashes are written ``--``. ASCII, no semantic content.
#: 4. "Return ONLY the structured `ticket_digest` object" gains ", as JSON". This one
#:    CHANGES what the model emits: the retired op had a structured-output schema to
#:    bind the reply, and this tier parses free text instead, so the reply shape has
#:    to be asked for. :func:`recover_digest` tolerates the shapes that still vary.
#:
#: Deviations 1, 2 and 4 mean the stored vectors are not byte-for-byte the eval's.
#: ADR 0122's recall is therefore evidence for the ARCHITECTURE -- digest, embed,
#: cosine-rank -- rather than a measurement of this exact prompt string.
DIGEST_PROMPT = """\
You extract a compact, normalized DEDUP DIGEST from a single work ticket. The digest is used
to find semantically overlapping or duplicate tickets elsewhere in the tracker, so it must
capture what the ticket is *fundamentally about* in a stable, canonical form -- robust to
paraphrase ("login broken" and "users cannot authenticate" should yield overlapping digests).

You are given the ticket's text (title, description, and comments) as the user message.

Produce a `ticket_digest` with exactly these four fields:

- `problem_keywords`: salient problem/domain keywords. Deduplicate; prefer canonical,
  lowercased terms. Omit filler.
- `component_or_area`: a short phrase naming the component / subsystem / area the ticket
  concerns (e.g. "plan-review gate", "event store reducer", "Jira reconciler").
- `key_entities`: NAMED, SPECIFIC entities the ticket turns on -- config keys, schema/table
  names, file or module paths, function names, event types, CLI subcommands. Specificity is
  what makes two tickets provably about the same thing, so prefer concrete named entities over
  vague nouns.
- `propositions`: 2 to 6 ATOMIC statements, each a single problem/behavior/repro claim
  capturing one facet of the ticket. Each proposition should stand alone and describe intent or
  symptom, not implementation minutiae.

STRICT RULES:
- DISCARD logs, stack traces, tracebacks, raw error dumps, and pasted code blocks. They are
  noise for overlap detection -- never copy log lines, frames, or code into any field.
- Do NOT invent facts not supported by the ticket text.
- Emit NO timestamps, uuids, dates, or other nondeterministic values in any field -- the digest
  must be stable modulo wording.
- Return ONLY the structured `ticket_digest` object, as JSON.
"""


class TierUnavailable(Exception):
    """The embedding tier cannot serve this query, with the reason it cannot.

    Raised internally and converted to a degrade by :func:`rank`; it never reaches a
    ``find_similar`` caller. The message is the diagnostic an operator reads to tell
    "not configured" from "configured and failing", which are otherwise identical
    from the outside: both produce lexical results.
    """


# ── Configuration ─────────────────────────────────────────────────────────────


def _llm_config() -> Any | None:
    """The resolved ``[llm]`` config, or ``None`` when the agents stack is absent.

    ``rebar.llm`` lives behind the ``[agents]`` extra, so importing it is itself a
    degradable operation: a core-only install must get the lexical tier rather than
    an ImportError on the create path.
    """
    try:
        from rebar.llm.config import LLMConfig

        return LLMConfig.from_env()
    except Exception:  # pragma: no cover - exercised via the configured paths
        logger.debug("similar-ticket embedding tier: llm config unavailable", exc_info=True)
        return None


def embedding_model_name() -> str | None:
    """The configured ``[llm] embedding_model``, or ``None`` when unset."""
    cfg = _llm_config()
    return getattr(cfg, "embedding_model", None) or None


def digest_model_name() -> str | None:
    """The configured ``[llm] similar_digest_model``, or ``None`` when unset."""
    cfg = _llm_config()
    return getattr(cfg, "similar_digest_model", None) or None


def tier_key() -> str | None:
    """The current validity key, or ``None`` when no embedding model is configured."""
    model = embedding_model_name()
    return validity_key(model, DIGEST_PROMPT_COMMIT) if model else None


# ── Digest recovery ───────────────────────────────────────────────────────────


def _first_balanced_object(text: str) -> str | None:
    """The first ``{...}`` substring whose braces balance, or ``None``.

    ADR 0122 observed valid JSON behind a prose preamble. Scanning for a balanced
    span recovers it without the regex-for-JSON failure mode of stopping at the first
    ``}``; string literals are tracked so a brace inside one is not counted.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _as_yaml(text: str) -> dict | None:
    """*text* decoded as a YAML mapping, or ``None``. A missing PyYAML is not an error."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a rebar dependency
        return None
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _unwrap(mapping: dict) -> dict:
    """Strip a single wrapper key such as ``ticket_digest``.

    The unwrap is conditional on the OUTER mapping not already carrying the digest
    fields, so a digest that legitimately has one field cannot be mistaken for a
    wrapper and descended into.
    """
    if any(field in mapping for field in DIGEST_FIELDS):
        return mapping
    if len(mapping) == 1:
        (only,) = mapping.values()
        if isinstance(only, dict):
            return only
    for key in _WRAPPER_KEYS:
        inner = mapping.get(key)
        if isinstance(inner, dict):
            return inner
    return mapping


def recover_digest(reply: str) -> dict | None:
    """The digest mapping carried by *reply*, or ``None`` if none can be recovered.

    The chain is JSON, then YAML, then the first balanced object embedded in prose;
    whatever yields a top-level mapping is unwrapped. A mapping that carries NONE of
    the four field names is not a digest -- a model that answered a different question
    in valid JSON must count as unrecovered, not as an empty digest, because the two
    have different handling (retry versus accept-and-degrade).
    """
    text = (reply or "").strip()
    if not text:
        return None
    candidates = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        pass
    yaml_loaded = _as_yaml(text)
    if yaml_loaded is not None:
        candidates.append(yaml_loaded)
    embedded = _first_balanced_object(text)
    if embedded is not None:
        try:
            candidates.append(json.loads(embedded))
        except ValueError:
            pass
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        unwrapped = _unwrap(candidate)
        if isinstance(unwrapped, dict) and any(f in unwrapped for f in DIGEST_FIELDS):
            return unwrapped
    return None


def _field_text(value: Any) -> str:
    """One digest field flattened to text. Lists join; everything else stringifies."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_field_text(v) for v in value).strip()
    if isinstance(value, dict):
        return " ".join(_field_text(v) for v in value.values()).strip()
    return str(value).strip()


def digest_paragraph(mapping: dict) -> str:
    """The 4 fields concatenated into the paragraph that is embedded.

    Empty when every field is empty, which is exactly the ``empties`` condition: a
    lenient parser that returned this silently cannot tell mass failure from success,
    and ADR 0122 requires the counter for that reason.
    """
    parts = [_field_text(mapping.get(field)) for field in DIGEST_FIELDS]
    return " ".join(p for p in parts if p).strip()


# ── Provider seams ────────────────────────────────────────────────────────────
#
# Both functions below are the ONLY places this module talks to a provider, and both
# dispatch a provider-qualified string through pydantic-ai's own inference. Tests
# replace these two names; nothing else in the module needs stubbing, and no code
# path below branches on which vendor the string names.


def _digest_model(model: str) -> Any:
    """Resolve a provider-qualified string to a pydantic-ai model.

    This is the whole of rebar's provider selection for the digest: a string goes in,
    pydantic-ai decides what to build. There is no table of vendors here to fall out of
    date, and adding a vendor is a configuration change rather than a code change.
    """
    from pydantic_ai.models import infer_model

    return infer_model(model)


def _embedding_model(model: str) -> Any:
    """Resolve a provider-qualified string to a pydantic-ai embedding model.

    The embedding counterpart of :func:`_digest_model`, and dispatched the same way.
    ``infer_embedding_model`` returns an ``EmbeddingModel`` unchanged, which is what
    lets a test inject a deterministic stand-in through this one seam.
    """
    from pydantic_ai.embeddings import infer_embedding_model

    return infer_embedding_model(model)


def _digest_reply(model: str, text: str) -> str:
    """One digest call. Returns the model's raw reply text."""
    from pydantic_ai import Agent

    agent = Agent(_digest_model(model), instructions=DIGEST_PROMPT, output_type=str)
    return str(agent.run_sync(text[:DIGEST_INPUT_CAP]).output or "")


def _embed(model: str, texts: list[str], *, as_query: bool) -> list[list[float]]:
    """Embed *texts* under *model*. Returns one vector per input, in order."""
    from pydantic_ai.embeddings import Embedder

    embedder = Embedder(_embedding_model(model))
    result = embedder.embed_query_sync(texts) if as_query else embedder.embed_documents_sync(texts)
    return [list(vector) for vector in result.embeddings]


# ── Ranking ───────────────────────────────────────────────────────────────────


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, or ``0.0`` for a zero or mismatched-width vector."""
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _require_coverage(conn: sqlite3.Connection, key: str) -> dict[str, list[float]]:
    """Every current-key vector, or raise when the corpus is not fully covered.

    Ranking a PARTIAL corpus would silently drop every ticket the build has not
    reached, which the caller cannot distinguish from those tickets being irrelevant.
    An interrupted re-embed under a new key is the same condition and gets the same
    answer, which is what keeps two embedding spaces from being compared.
    """
    missing = unvectored_ids(conn, key)
    if missing:
        raise TierUnavailable(
            f"{len(missing)} indexed ticket(s) carry no vector under the current "
            "embedding configuration; run a vector build to converge"
        )
    vectors = current_vectors(conn, key)
    if not vectors:
        raise TierUnavailable("no vectors are stored under the current embedding configuration")
    return vectors


def _titles(conn: sqlite3.Connection, ticket_ids: list[str]) -> dict[str, tuple[str, str]]:
    """``{ticket_id: (alias, title)}`` for the ranked rows, read from the same index."""
    if not ticket_ids:
        return {}
    placeholders = ",".join("?" for _ in ticket_ids)
    rows = conn.execute(
        f"SELECT ticket_id, alias, title FROM docs WHERE ticket_id IN ({placeholders})",
        ticket_ids,
    ).fetchall()
    return {row[0]: (row[1] or "", row[2] or "") for row in rows}


def _embed_query(model: str, digest_model: str, text: str) -> list[float]:
    """The query vector, produced the way the corpus vectors were produced.

    The digest runs on the query because ADR 0122's adopted ``vector`` candidate is
    symmetric -- see this module's docstring. It is the same :func:`_paragraph_for`
    the build calls, including its single retry, so the two sides cannot drift: a
    change to how a ticket is reduced to an embeddable paragraph changes both at once.
    The per-query stats are discarded; the build is where those counters are reported.
    """
    paragraph = _paragraph_for(digest_model, text, _new_stats())
    if paragraph is None:
        raise TierUnavailable("the query digest produced no embeddable paragraph")
    try:
        return _embed(model, [paragraph], as_query=True)[0]
    except ImportError as exc:
        raise TierUnavailable(f"embedding backend unavailable: {exc}") from exc
    except Exception as exc:
        raise TierUnavailable(f"embedding call failed: {exc}") from exc


def rank(
    conn: sqlite3.Connection,
    text: str,
    *,
    exclude_id: str | None = None,
    limit: int = 5,
) -> list[dict] | None:
    """Cosine-ranked similar tickets, or ``None`` meaning "degrade to lexical".

    ``None`` is the single degrade signal, and every unavailability shares it: no
    configured model, an absent optional backend, a provider denial or timeout, and a
    corpus without full current-key coverage. The caller does not need to tell them
    apart -- it serves the lexical results either way -- so the distinction is carried
    in the diagnostic rather than in the return value.
    """
    if limit <= 0:
        return []
    try:
        return _rank_or_raise(conn, text, exclude_id=exclude_id, limit=limit)
    except TierUnavailable as exc:
        logger.warning("similar-ticket embedding tier degraded to lexical: %s", exc)
        return None
    except Exception:
        logger.warning("similar-ticket embedding tier failed; degrading to lexical", exc_info=True)
        return None


def _rank_or_raise(
    conn: sqlite3.Connection,
    text: str,
    *,
    exclude_id: str | None,
    limit: int,
) -> list[dict]:
    """:func:`rank` without the degrade conversion. Raises :class:`TierUnavailable`."""
    # The cheap probe comes FIRST, deliberately. An install that never configured the
    # tier is the common case and it runs on every create, so it must not pay a config
    # discovery pass and an import of the agents stack to be told "not configured". An
    # empty `vectors` table proves the tier cannot serve, whatever the config says.
    if conn.execute("SELECT 1 FROM vectors LIMIT 1").fetchone() is None:
        raise TierUnavailable("no vectors are stored; the embedding tier is not in use")
    model = embedding_model_name()
    if not model:
        raise TierUnavailable("no [llm] embedding_model is configured")
    # The SAME resolution the build uses, deliberately: a query that resolved the
    # digest model differently from the build would embed its digest under a prompt
    # the corpus was not digested with, which is the asymmetry this tier exists to
    # avoid -- and an install relying on the fallback would build a full corpus and
    # then degrade on every query.
    digest_model = digest_model_name() or model
    stored = _require_coverage(conn, validity_key(model, DIGEST_PROMPT_COMMIT))
    query_vector = _embed_query(model, digest_model, text or "")

    scored = [
        (ticket_id, _cosine(query_vector, vector))
        for ticket_id, vector in stored.items()
        if ticket_id != exclude_id
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    top = scored[:limit]
    meta = _titles(conn, [ticket_id for ticket_id, _ in top])
    return [
        {
            "id": ticket_id,
            "alias": meta.get(ticket_id, ("", ""))[0],
            "title": meta.get(ticket_id, ("", ""))[1],
            "score": max(0.0, min(1.0, (score + 1.0) / 2.0)),
        }
        for ticket_id, score in top
    ]


# ── The explicit build ────────────────────────────────────────────────────────


def _new_stats() -> dict[str, int]:
    """The counters every build reports. ADR 0122 requires ``empties`` by name."""
    return {
        "vectored": 0,
        "skipped": 0,
        "empties": 0,
        "digest_unparsed": 0,
        "digest_failed": 0,
    }


def _digest_once(model: str, text: str, stats: dict[str, int]) -> dict | None:
    """One digest attempt. ``None`` means the reply carried no recoverable object."""
    try:
        reply = _digest_reply(model, text)
    except ImportError as exc:
        raise TierUnavailable(f"digest backend unavailable: {exc}") from exc
    except Exception as exc:
        raise TierUnavailable(f"digest call failed: {exc}") from exc
    recovered = recover_digest(reply)
    if recovered is None:
        stats["digest_unparsed"] += 1
    return recovered


def _paragraph_for(model: str, text: str, stats: dict[str, int]) -> str | None:
    """The embeddable paragraph for one ticket, or ``None`` to leave it unvectored.

    An unrecoverable reply is retried ONCE -- ADR 0122 saw two tickets in 630 answer a
    different question entirely, and both succeeded on retry, so the failure is
    nondeterministic and a retry is the correct handling. A retry that succeeds counts
    as a success and leaves ``digest_failed`` at zero. An all-empty digest is NOT
    retried: the model answered, and asking again costs a call to be told the same
    thing.
    """
    recovered = _digest_once(model, text, stats)
    if recovered is None:
        recovered = _digest_once(model, text, stats)
    if recovered is None:
        stats["digest_failed"] += 1
        return None
    paragraph = digest_paragraph(recovered)
    if not paragraph:
        stats["empties"] += 1
        return None
    return paragraph


def _ticket_text(conn: sqlite3.Connection, ticket_id: str) -> str:
    """The indexed title and description of one ticket, as the digest's input."""
    row = conn.execute(
        "SELECT title, description FROM docs WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        return ""
    return f"{row[0] or ''}\n\n{row[1] or ''}".strip()


def build_vectors(tracker: str, *, limit: int | None = None) -> dict[str, Any]:
    """Digest and embed every indexed ticket lacking a current-key vector.

    This is the build, and it is EXPLICIT: nothing in the read path calls it, so a
    create never pays for it. It is incremental and resumable -- each ticket is
    committed as it is embedded, so an interrupted run leaves the tickets it reached
    vectored and the rest for the next run.

    Returns the counters in :func:`_new_stats` plus ``status``: ``"complete"`` when
    the corpus is fully covered on return, ``"partial"`` when some tickets were left
    unvectored, and ``"unavailable"`` when no model is configured or the provider
    could not be reached at all.
    """
    from .index import open_index

    stats: dict[str, Any] = _new_stats()
    model = embedding_model_name()
    if not model:
        reason = "no [llm] embedding_model is configured"
        return {**stats, "status": "unavailable", "reason": reason}
    digest_model = digest_model_name() or model

    conn = open_index(tracker)
    if conn is None:
        return {**stats, "status": "unavailable", "reason": "the index could not be opened"}
    try:
        from .catchup import catch_up

        catch_up(tracker, conn)
        return _run_build(conn, model, digest_model, stats, limit)
    finally:
        conn.close()


def _run_build(
    conn: sqlite3.Connection,
    model: str,
    digest_model: str,
    stats: dict[str, Any],
    limit: int | None,
) -> dict[str, Any]:
    """The build loop over the uncovered tickets. Split out to keep the entry point flat."""
    key = validity_key(model, DIGEST_PROMPT_COMMIT)
    pending = unvectored_ids(conn, key)
    if limit is not None:
        pending = pending[:limit]
    for ticket_id in pending:
        try:
            _vector_one(conn, key, model, digest_model, ticket_id, stats)
        except TierUnavailable as exc:
            logger.warning("similar-ticket vector build stopped: %s", exc)
            return {**stats, "status": "unavailable", "reason": str(exc)}
    remaining = unvectored_ids(conn, key)
    return {
        **stats,
        "status": "complete" if not remaining else "partial",
        "remaining": len(remaining),
    }


def _vector_one(
    conn: sqlite3.Connection,
    key: str,
    model: str,
    digest_model: str,
    ticket_id: str,
    stats: dict[str, Any],
) -> None:
    """Digest, embed and store one ticket. A digest that fails leaves it unvectored.

    A digest failure is per-ticket and the build proceeds; a PROVIDER failure is not,
    and propagates as :class:`TierUnavailable` so the run stops rather than burning a
    call per remaining ticket against an endpoint that is denying all of them.

    The digest and the embed are slow and run OUTSIDE the write lock, so a concurrent
    catch-up can rewrite or remove this ticket's document while they run -- dropping
    the vector precisely because the text moved. The upsert is therefore conditional:
    the document is re-read under ``BEGIN IMMEDIATE`` and the vector is stored only if
    the text it describes is still the indexed text. Storing it unconditionally would
    resurrect a vector of superseded text UNDER THE CURRENT KEY, which is worse than
    leaving the ticket unvectored: :func:`unvectored_ids` would report coverage the
    corpus does not have, and ranking would serve the stale row instead of degrading.
    A ticket lost to that race counts as ``skipped`` and the next build reaches it.
    """
    text = _ticket_text(conn, ticket_id)
    paragraph = _paragraph_for(digest_model, text, stats)
    if paragraph is None:
        stats["skipped"] += 1
        return
    try:
        vector = _embed(model, [paragraph], as_query=False)[0]
    except ImportError as exc:
        raise TierUnavailable(f"embedding backend unavailable: {exc}") from exc
    except Exception as exc:
        raise TierUnavailable(f"embedding call failed: {exc}") from exc
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _ticket_text(conn, ticket_id) != text:
            conn.execute("ROLLBACK")
            stats["skipped"] += 1
            return
        store_vector(conn, ticket_id, key, vector)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    stats["vectored"] += 1
