"""Prompt front-matter format + canonical writer (workflow authoring v2, d25d).

Golden round-trip + boundary-corruptor coverage for the parse-split-rejoin writer:
idempotency, byte-for-byte body preservation (no-trailing-NL / CRLF / body that
itself starts with ``---``), canonical key order, WARN+PRESERVE of unknown keys,
BOM rejection, and the read-side schema_version coexistence refuse. Pure stdlib +
PyYAML; no store/network."""

from __future__ import annotations

import warnings

import pytest

from rebar.llm.prompts import (
    FRONT_MATTER_KEYS,
    PROMPT_SCHEMA_VERSION,
    PromptError,
    PromptVersionError,
    _split_front_matter_raw,
    parse_front_matter,
    write_front_matter,
)


def _canonicalize(text: str) -> str:
    """String→string canonical pass: split (byte-preserving) then re-write."""
    meta, body = _split_front_matter_raw(text)
    return write_front_matter(meta, body)


# ── idempotency + stamping ──────────────────────────────────────────────────────


def test_writer_stamps_schema_version_and_is_idempotent() -> None:
    out = write_front_matter({"title": "T", "description": "d"}, "Body {{x}}\n")
    assert f"schema_version: {PROMPT_SCHEMA_VERSION}" in out
    # write(*split(write(m,b))) == write(m,b)
    assert _canonicalize(out) == out
    # and a second canonical pass is a no-op (the wb(wb(x)) == wb(x) requirement)
    assert _canonicalize(_canonicalize(out)) == _canonicalize(out)


def test_canonical_key_order() -> None:
    # Keys handed out of order are emitted in FRONT_MATTER_KEYS order.
    out = write_front_matter(
        {"category": "review", "title": "T", "execution_mode": "single_turn"}, "b\n"
    )
    lines = [ln.split(":")[0] for ln in out.splitlines() if ln and not ln.startswith("---")]
    order = [k for k in lines if k in FRONT_MATTER_KEYS]
    assert order == sorted(order, key=FRONT_MATTER_KEYS.index)
    assert order[0] == "schema_version"


# ── body byte-for-byte (the three boundary corruptors, port of spike E1) ─────────


@pytest.mark.parametrize(
    "body",
    [
        "no trailing newline",  # corruptor 1: missing final \n
        "line1\r\nline2\r\n",  # corruptor 2: embedded CRLF in the BODY
        "---\nthis body itself starts with a fence\n",  # corruptor 3
        "",  # empty body
        "normal body\nwith two lines\n",
    ],
)
def test_body_preserved_byte_for_byte(body: str) -> None:
    out = write_front_matter({"title": "T"}, body)
    # the file is exactly: front-matter block + body, verbatim
    assert out.endswith(body)
    fm, recovered = _split_front_matter_raw(out)
    assert recovered == body  # round-trip recovers the body unchanged
    # and re-writing reproduces the same bytes (idempotent over the corruptor)
    assert write_front_matter(fm, recovered) == out


# ── unknown keys: WARN + PRESERVE (appended, sorted) ─────────────────────────────


def test_unknown_keys_warn_and_are_preserved_sorted() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = write_front_matter({"title": "T", "zeta_x": 1, "alpha_x": 2}, "b\n")
    assert any("alpha_x" in str(w.message) for w in caught)
    assert any("zeta_x" in str(w.message) for w in caught)
    # preserved, and appended in sorted order AFTER the known keys
    assert out.index("alpha_x") < out.index("zeta_x")
    assert out.index("title") < out.index("alpha_x")
    meta, _ = _split_front_matter_raw(out)
    assert meta["alpha_x"] == 2 and meta["zeta_x"] == 1


# ── BOM rejection ─────────────────────────────────────────────────────────────


def test_writer_rejects_bom_body() -> None:
    with pytest.raises(PromptError, match="BOM"):
        write_front_matter({"title": "T"}, "\ufeffbody\n")


def test_raw_split_rejects_bom_file() -> None:
    with pytest.raises(PromptError, match="BOM"):
        _split_front_matter_raw("\ufeff---\ntitle: T\n---\nbody\n")


# ── read-side schema_version coexistence ─────────────────────────────────────────


def test_higher_schema_version_is_refused_on_read() -> None:
    newer = f"---\nschema_version: {PROMPT_SCHEMA_VERSION + 1}\ntitle: T\n---\nbody\n"
    with pytest.raises(PromptVersionError, match="newer than this rebar"):
        parse_front_matter(newer)
    with pytest.raises(PromptVersionError):
        _split_front_matter_raw(newer)


def test_current_schema_version_reads_fine() -> None:
    cur = f"---\nschema_version: {PROMPT_SCHEMA_VERSION}\ntitle: T\n---\nbody\n"
    meta, body = parse_front_matter(cur)
    assert meta["title"] == "T" and body == "body\n"


def test_legacy_no_version_still_parses() -> None:
    # Existing front-matter-less prompts and ones without schema_version are unchanged.
    assert parse_front_matter("plain body, no front matter\n") == (
        {},
        "plain body, no front matter\n",
    )
    meta, body = parse_front_matter("---\nvariables: [x]\n---\nHi {{x}}\n")
    assert meta == {"variables": ["x"]} and body == "Hi {{x}}\n"
