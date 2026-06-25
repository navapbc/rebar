"""Characterization tests for the canonical-JSON/hash consolidation (epic civil-marlin-flare).

After consolidating six inline "sorted-key compact JSON (+ sha256)" reimplementations onto the
single seam ``rebar._store.canonical`` (``canonical_str`` / ``canonical_bytes`` / ``content_hash``),
this module is the byte-for-byte safety net proving the migration was **behavior-preserving**.

The expected values below are **golden constants captured from the PRE-refactor code
paths** (literal strings/digests, NOT re-derived from the new seam) — so a regression that
changes any migrated caller's output is caught rather than masked. Each migrated caller is
exercised by name: ``signing._canonical_payload``, ``schema.canonical_json`` /
``schema.content_hash``, ``mutation.serialize_manifest``, ``conflict_resolver._hash_value``.

Two encoding axes are pinned because they were the actual drift between the inline copies:
``ascii_only`` (``ensure_ascii``) and ``default`` (the ``json.dumps`` fallback). The
reconciler manifest + provenance ledger relied on the stdlib ``ensure_ascii=True`` default,
so they migrate with ``ascii_only=True``; the workflow + signing forms used
``ensure_ascii=False`` (the seam default).
"""

from __future__ import annotations

import sys
from pathlib import Path

from rebar import signing
from rebar._store.canonical import canonical_bytes, canonical_str, content_hash
from rebar.llm.workflow.schema import canonical_json
from rebar.llm.workflow.schema import content_hash as schema_content_hash

# A single golden document exercising every drift axis: nested, null, non-ASCII values,
# non-ASCII / Greek unicode KEYS (so sort order over codepoints is also pinned).
_DOC = {"z": {"deep": [1, {"k": "café"}]}, "a": None, "名前": "世界", "α": "🚀"}

# Golden bytes/digests captured from the pre-refactor implementations (see the epic).
_GOLDEN_SCHEMA_CJSON = '{"a":null,"z":{"deep":[1,{"k":"café"}]},"α":"🚀","名前":"世界"}'
_GOLDEN_SCHEMA_CHASH = "a46728b5fd442e6d1417ac83743622f9112a50266f32f52a975d8c6d6b0097c2"
_GOLDEN_CR_HASH_DOC = "4ad792a92fc077fbaa1f348a2c13a58b2a36514b07f1c62ed630c1901f25b855"
_GOLDEN_MUT_JSON = (
    '[{"action":"update","direction":"inbound","payload":{"summary":"caf\\u00e9 \\u2615"},'
    '"provenance":{"src":"jira"},"target":"TICKET-\\u4e16\\u754c"}]'
)
_GOLDEN_MUT_SHA = "95e67c62c5fd9df30df6661be4851aace11db9b2385738ac30b1776610f91ad0"
_GOLDEN_SIGN_PAYLOAD = (
    b'{"algorithm":"HMAC-SHA256","manifest":["dep sha123 src/caf\xc3\xa9/'
    b'\xe4\xb8\x96\xe7\x95\x8c.py","step build ok"],"ticket_id":"1234-abcd","v":1}'
)

_SIGN_TICKET = "1234-abcd"
_SIGN_MANIFEST = ["dep sha123 src/café/世界.py", "step build ok"]


def _reconciler_modules():
    """Import the engine-rooted ``rebar_reconciler`` modules (own sys.path root).

    Under pytest the ``tests/unit/rebar_reconciler`` package shadows the engine package
    of the same name, so — like ``tests/unit/rebar_reconciler/conftest.py`` — we put the
    engine on ``sys.path`` and extend the shadowing package's ``__path__`` to the engine
    dir so engine submodules (``mutation``, ``conflict_resolver``, ``timeutil``) resolve.
    """
    engine = Path(__file__).resolve().parents[2] / "src" / "rebar" / "_engine"
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    import rebar_reconciler

    engine_pkg = str(engine / "rebar_reconciler")
    if engine_pkg not in rebar_reconciler.__path__:
        rebar_reconciler.__path__.append(engine_pkg)
    from rebar_reconciler import conflict_resolver, mutation

    return mutation, conflict_resolver


# ── the seam's own contract: the two additive, keyword-only axes ───────────────────────────────
def test_seam_default_is_utf8_literal():
    # ascii_only defaults False -> literal UTF-8 (the canonical event form).
    assert canonical_str({"k": "世界"}) == '{"k":"世界"}'
    assert b"\\u" not in canonical_bytes({"k": "世界"})


def test_seam_ascii_only_escapes_non_ascii():
    assert canonical_str({"k": "世界"}, ascii_only=True) == '{"k":"\\u4e16\\u754c"}'


def test_seam_default_callback_handles_non_json_native():
    # default=str lets arbitrary objects serialize (used by the provenance ledger).
    class Obj:
        def __str__(self) -> str:
            return "obj!"

    assert canonical_str({"x": Obj()}, default=str) == '{"x":"obj!"}'


def test_seam_content_hash_is_sha256_of_canonical_bytes():
    import hashlib

    assert content_hash(_DOC) == hashlib.sha256(canonical_bytes(_DOC)).hexdigest()


def test_seam_keyword_params_are_additive_positional_unchanged():
    # The pre-refactor positional signature canonical_str(doc) is preserved byte-identically,
    # so the existing event-serialization importers are untouched.
    assert canonical_str(_DOC) == _GOLDEN_SCHEMA_CJSON


# ── per-caller byte/digest equality vs the pre-refactor goldens ────────────────────────────────
def test_schema_canonical_json_unchanged():
    assert canonical_json(_DOC) == _GOLDEN_SCHEMA_CJSON


def test_schema_content_hash_unchanged():
    assert schema_content_hash(_DOC) == _GOLDEN_SCHEMA_CHASH


def test_signing_canonical_payload_unchanged():
    assert signing._canonical_payload(_SIGN_TICKET, _SIGN_MANIFEST) == _GOLDEN_SIGN_PAYLOAD


def test_conflict_resolver_hash_value_non_ascii_unchanged():
    # Routes a non-ASCII value through _hash_value: locks the ascii_only=True choice
    # (its pre-refactor json.dumps relied on the stdlib ensure_ascii=True default).
    _, conflict_resolver = _reconciler_modules()
    assert conflict_resolver._hash_value(_DOC) == _GOLDEN_CR_HASH_DOC


def test_mutation_serialize_manifest_unchanged():
    mutation, _ = _reconciler_modules()
    m = mutation.Mutation(
        direction=mutation.MutationDirection.inbound,
        action=mutation.MutationAction.update,
        target="TICKET-世界",
        payload={"summary": "café ☕"},
        provenance={"src": "jira"},
    )
    json_text, sha = mutation.serialize_manifest([m])
    assert json_text == _GOLDEN_MUT_JSON
    assert sha == _GOLDEN_MUT_SHA


# ── signing sign→verify round-trip still certifies (the persisted-and-recompared path) ─────────
def test_signing_round_trip_certifies():
    key = b"deterministic-test-signing-key"
    sig = signing.compute_signature(_SIGN_TICKET, _SIGN_MANIFEST, key)
    # deterministic recompute
    assert signing.compute_signature(_SIGN_TICKET, _SIGN_MANIFEST, key) == sig
    record = {
        "manifest": _SIGN_MANIFEST,
        "signature": sig,
        "key_id": signing.key_fingerprint(key),
        "algorithm": signing.ALGORITHM,
        "v": signing.PAYLOAD_VERSION,
    }
    verdict = signing.verify_record(record, _SIGN_TICKET, key)
    assert verdict["verdict"] == "certified", verdict
