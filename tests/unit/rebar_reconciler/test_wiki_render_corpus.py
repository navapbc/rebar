"""Corpus safety tests for the DC wiki renderer (story 271c, epic 708d).

These run the renderer over the vendored ``tests/fixtures/dc_wiki_corpus/`` snapshot
of real rebar prose — the punctuation-dense material pandoc's jira writer mishandles.
The corpus is committed (see ``scripts/build_dc_wiki_corpus.py``) so the counts below
are hermetic and reproducible without a live ticket store.

The claims are SAFETY claims, not fidelity claims: the DC path is one-way, so what
must hold is that nothing is corrupted and that rendering settles.

**Cost discipline.** Rendering the corpus once costs ~884 pandoc subprocess spawns
(~48s). CI runs the suite under ``-n 3 --dist worksteal --timeout=300``, so a test
that rendered the corpus five times exceeded the per-test timeout and crashed its
xdist worker. Three things keep this module cheap without weakening any assertion:

* every test shares ONE pass-1 render of the corpus (the ``corpus_pass1`` fixture),
  and that render is shared ACROSS xdist workers via a session-scoped on-disk
  artifact — a process-local ``functools.lru_cache`` was re-filled once per worker,
  which measured as three ~43s fills under ``-n 3`` versus one ~48s fill serially
  (ticket 20cb-cbae-e9df-45e3);
* the fixed-point tests read pass 1 from that same artifact and only compute the
  passes they actually add; and
* five-pass identity is proven with TWO renders rather than five — see
  :func:`test_dc_corpus_passes_two_to_five_are_byte_identical`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from rebar_reconciler.adapters.jira_family import wiki_render
from rebar_reconciler.adapters.jira_family.wiki_render import (
    code_fragments,
    render_markdown_to_wiki,
)

_CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "dc_wiki_corpus"

_PIPE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_BOX_RULE_RE = re.compile(r"^\s*\+[-+=]{2,}\+\s*$", re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_STRATA = ("code_arrow", "table", "prose")


def _load(name: str) -> list[str]:
    return json.loads((_CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def _all_bodies() -> list[str]:
    return [body for name in _STRATA for body in _load(name)]


# ── Session-scoped, cross-worker pass-1 artifact ──────────────────────────────
# A full corpus render is ~884 pandoc spawns. A process-local cache is re-filled by
# every xdist worker that lands one of the consumers below, so the render is instead
# published to a file shared by all workers of THIS pytest session.

# Deliberately BELOW CI's ``--timeout=300`` per-test guard, with headroom over the
# ~48s winner fill: a worker blocked on a crashed or pathologically slow winner
# renders locally and still finishes inside the timeout instead of being killed by it.
_FILL_FALLBACK_SECONDS = 150.0


def _pandoc_stamp() -> str:
    """Identify the pandoc build in play: its reported version, path and size.

    The VERSION is what actually decides the output — pandoc's jira writer changes
    its escaping between releases, which is why the `wiki` extra pins
    ``pypandoc-binary==1.15`` at all. Reading it costs one ~0.2s subprocess per
    session.
    """
    path = wiki_render._pandoc_path() or ""
    try:
        import pypandoc

        version = str(pypandoc.get_pandoc_version())
    except Exception:  # noqa: BLE001 — absent extra or unreadable binary
        version = "unknown"
    size = str(Path(path).stat().st_size) if path and Path(path).exists() else "0"
    return f"{version}|{path}|{size}"


def _corpus_digest() -> str:
    """Pin the artifact to the inputs the render is a function of.

    Covers the corpus bodies, the renderer source, and the pandoc version/path/size.
    Two distinct pandoc builds reporting the same version at the same path and size
    would collide, so this is a strong practical key rather than a proof.
    """
    digest = hashlib.sha256()
    for name in _STRATA:
        digest.update((_CORPUS / f"{name}.json").read_bytes())
    digest.update(Path(wiki_render.__file__).read_bytes())
    digest.update(_pandoc_stamp().encode("utf-8"))
    return digest.hexdigest()[:16]


def _render(stratum: str) -> list[tuple[str, str]]:
    return [(body, render_markdown_to_wiki(body)) for body in _load(stratum)]


def _probe_order() -> tuple[str, ...]:
    """Rotate which stratum THIS worker fills first, so workers do not collide.

    Only a scheduling hint: correctness comes from the lock either way. Without it,
    concurrent whole-corpus consumers all queue on the same stratum and the fill
    serialises; with it they fill different strata at once, so the wall-clock cost of
    a cold session is one stratum rather than the whole corpus.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    try:
        index = int(worker.removeprefix("gw"))
    except ValueError:
        index = 0
    shift = index % len(_STRATA)
    return _STRATA[shift:] + _STRATA[:shift]


def _shared_stratum(root: Path, key: str, stratum: str) -> list[tuple[str, str]]:
    """One stratum's pass-1 render, computed ONCE per session across all workers.

    Exclusion is a lock DIRECTORY (``os.mkdir`` is atomic on POSIX and Windows, so no
    new dependency is needed). The winner publishes with ``os.replace``, which is
    atomic — a visible artifact is therefore always complete.
    """
    artifact = root / f"dc_wiki_pass1_{key}_{stratum}.json"
    lock = root / f"dc_wiki_pass1_{key}_{stratum}.lock"
    deadline = time.monotonic() + _FILL_FALLBACK_SECONDS

    while True:
        if artifact.exists():
            return [(body, out) for body, out in json.loads(artifact.read_text(encoding="utf-8"))]
        try:
            lock.mkdir()
        except FileExistsError:
            if time.monotonic() > deadline:
                # The holder is gone or pathologically slow. Render locally — i.e.
                # degrade to the old per-worker behaviour — but deliberately do NOT
                # remove the lock: only its creator does that. Breaking it here
                # would let a THIRD worker acquire a lock that a merely-slow holder
                # then deletes in its own `finally`, so two workers could hold what
                # is nominally the same lock.
                return _render(stratum)
            time.sleep(0.25)
            continue
        try:
            payload = [[body, out] for body, out in _render(stratum)]
            staging = root / f"dc_wiki_pass1_{key}_{stratum}.{os.getpid()}.tmp"
            staging.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(staging, artifact)
            return [(body, out) for body, out in payload]
        finally:
            # Retire the lock on the success AND the error path, so a worker that
            # raises mid-render does not strand every other worker on the fallback.
            # Safe to remove by name: nothing else ever deletes this directory, so
            # the lock seen here is always the one acquired above.
            try:
                lock.rmdir()
            except OSError:
                pass


def _shared_pass1(root: Path) -> tuple[tuple[str, str], ...]:
    """Every corpus body paired with its first-pass render, computed ONCE per session.

    ``root`` is ``tmp_path_factory.getbasetemp().parent`` — under xdist a worker's
    basetemp is ``pytest-<N>/popen-gw<K>``, so the parent is the session directory
    every worker shares, and pytest rotates it between sessions.
    """
    key = _corpus_digest()
    by_stratum = {stratum: _shared_stratum(root, key, stratum) for stratum in _probe_order()}
    # Re-emit in canonical `_all_bodies()` order, whatever order this worker filled in.
    return tuple(pair for stratum in _STRATA for pair in by_stratum[stratum])


@pytest.fixture(scope="session")
def corpus_pass1(tmp_path_factory: pytest.TempPathFactory) -> tuple[tuple[str, str], ...]:
    """Every corpus body paired with its first-pass render, shared across workers."""
    return _shared_pass1(tmp_path_factory.getbasetemp().parent)


def test_corpus_cardinality_is_pinned() -> None:
    """The fixture is FROZEN; a silent regeneration must fail here."""
    assert len(_load("code_arrow")) == 29
    assert len(_load("table")) == 29
    assert len(_load("prose")) == 120
    assert len(_all_bodies()) == 178


# NOTE: there is deliberately NO test here asserting the corpus is free of the repo's
# RETIRED vocabulary (the old bridge command spellings, the old force-close flag). The
# generator's scrub maps them, and two repo-wide guards already scan EVERY tracked file
# — including this fixture — for exactly those spellings
# (`test_bridge_vocabulary_stale_heldout` and `test_transition_force_flag_24f7`). A local
# copy would be weaker than those, and it could only be written by spelling the retired
# tokens out, which makes THIS file an offender the guards then flag.
def test_corpus_carries_no_unscrubbed_secrets() -> None:
    """Re-assert the generator's scrub held, per the capture-fixture doctrine."""
    blob = "\n".join(_all_bodies())

    assert not re.search(r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}", blob)
    assert ".atlassian.net" not in blob
    assert not set(re.findall(r"https?://[^\s)>\]]+", blob)) - {"https://example.invalid/redacted"}


def test_dc_corpus_protected_excerpts_are_retained(
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """The headline safety claim: code is content and never moves.

    Covers pandoc's escaping of punctuation inside code spans (``{{\\->}}``), which
    the renderer rejects via its post-conversion preservation check.
    """
    offenders = [
        body[:120]
        for body, out in corpus_pass1
        if any(fragment not in out for fragment in code_fragments(body))
    ]

    assert offenders == []


def test_dc_corpus_tables_survive_verbatim(corpus_pass1: tuple[tuple[str, str], ...]) -> None:
    """Every ASCII table is still a table, un-eroded, after rendering."""
    tables = [
        (body, out)
        for body, out in corpus_pass1
        if _PIPE_DELIM_RE.search(body) or _BOX_RULE_RE.search(body)
    ]

    assert len(tables) >= 29

    for _body, out in tables:
        assert _PIPE_DELIM_RE.search(out) or _BOX_RULE_RE.search(out)
        assert "\\-\\-" not in out


def test_dc_corpus_html_comments_survive_exactly(
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """pandoc DELETES HTML comments; rebar's echo marker is one, so they must survive."""
    for body, out in corpus_pass1:
        for marker in _HTML_COMMENT_RE.findall(body):
            assert marker in out


def test_render_is_deterministic() -> None:
    """The premise the cheap five-pass proof rests on: same input, same output."""
    body = "# T\n\nprose -> arrow with **bold**\n\n- a\n- b\n"

    assert render_markdown_to_wiki(body) == render_markdown_to_wiki(body)


@pytest.mark.parametrize("stratum", _STRATA)
def test_dc_corpus_passes_two_to_five_are_byte_identical(
    stratum: str,
    corpus_pass1: tuple[tuple[str, str], ...],
) -> None:
    """Rendering settles: no ratchet, no drift, across the whole corpus.

    Proven with TWO renders rather than five. The renderer is a pure deterministic
    function R (pinned by ``test_render_is_deterministic``), so once a body reaches a
    fixed point the rest of the sequence is forced: if ``R(p1) == p1`` then
    ``p3 = R(p2) = R(p1) = p2``, and likewise for p4 and p5. Where a body is NOT an
    immediate fixed point, one further render settles it and the same argument
    applies from there — so checking p2, and p3 only when needed, is equivalent to
    checking all five passes, at a fraction of the subprocess cost.

    Split per stratum so no single test carries the whole corpus past CI's per-test
    timeout. Pass 1 comes from the shared ``corpus_pass1`` artifact — it is the same
    ``render_markdown_to_wiki(body)`` value this test used to recompute — so only the
    passes this test actually adds are paid for here.
    """
    pass1 = dict(corpus_pass1)
    for body in _load(stratum):
        first = pass1[body]
        second = render_markdown_to_wiki(first)
        if second == first:
            continue  # fixed point: passes 2-5 are all `first` by determinism
        third = render_markdown_to_wiki(second)
        assert third == second, "rendering did not settle by pass 3"


def test_dc_corpus_coverage_ratios(corpus_pass1: tuple[tuple[str, str], ...]) -> None:
    """Richness floors, measured over the committed fixture.

    Floors, not equalities: the renderer may only get richer. A drop below either bar
    means eligible units silently started falling back.
    """
    pairs = corpus_pass1
    changed = [body for body, out in pairs if out != body]

    body_ratio = len(changed) / len(pairs)
    char_ratio = sum(len(b) for b in changed) / sum(len(b) for b, _ in pairs)

    assert body_ratio >= 0.90  # measured 0.916
    assert char_ratio >= 0.95  # measured 0.969


def test_dc_corpus_has_eligible_units_that_actually_change() -> None:
    """Guard against a vacuous pass: eligible units must really be dispatched.

    Uses one stratum, not the whole corpus — the claim is existential, so paying for
    a second full-corpus render to prove it would be waste.
    """
    pandoc = wiki_render._pandoc_path()
    eligible = 0
    changed = 0
    render_calls = 0
    first_changed_call = None
    for body in _load("prose"):
        for kind, text in wiki_render._lock_and_split(body):
            if kind != wiki_render._RENDER:
                continue
            eligible += 1
            render_calls += 1
            rendered = wiki_render._render_unit(text, pandoc or "")
            if rendered != text:
                changed += 1
                if first_changed_call is None:
                    first_changed_call = render_calls
                break
        if changed:
            break

    assert eligible > 0
    assert changed > 0
    assert render_calls == first_changed_call
