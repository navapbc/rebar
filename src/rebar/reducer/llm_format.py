"""ticket_reducer/llm_format.py

LLM formatting logic for ticket state dicts.

Provides to_llm(state) which converts a full ticket state dict to a
minified dict with shortened keys, stripped nulls/empty lists, and no
verbose timestamps.

Key mapping:
  ticket_id   → id
  ticket_type → t
  title       → ttl
  status      → st
  author      → au
  parent_id   → pid
  priority    → pr
  assignee    → asn
  description → desc
  tags        → tg
  comments    → cm  (sub-keys: body→b, author→au; timestamp omitted)
  deps        → dp  (sub-keys: target_id→tid, relation→r; link_uuid omitted)
  conflicts   → cf
  inbound_links → ibl (sub-keys: from_id→f, relation→r)
  children    → ch
"""

from __future__ import annotations

KEY_MAP = {
    "ticket_id": "id",
    "ticket_type": "t",
    "title": "ttl",
    "status": "st",
    "author": "au",
    "parent_id": "pid",
    "priority": "pr",
    "assignee": "asn",
    "alias": "a",
    "description": "desc",
    "tags": "tg",
    "comments": "cm",
    "deps": "dp",
    "conflicts": "cf",
    "inbound_links": "ibl",
    "children": "ch",
    "signature": "sig",
}

# Fields omitted from LLM format (verbose timestamps / system metadata)
OMIT_KEYS = {"created_at", "env_id"}

# Fields always emitted even when value is None
ALWAYS_EMIT: set[str] = set()

# Comment: keep only body and author (omit timestamp — not useful for LLM)
COMMENT_KEY_MAP = {
    "body": "b",
    "author": "au",
}
COMMENT_OMIT = {"timestamp"}

DEP_KEY_MAP = {
    "target_id": "tid",
    "relation": "r",
}
DEP_OMIT = {"link_uuid"}

INBOUND_KEY_MAP = {
    "from_id": "f",
    "relation": "r",
}


def shorten_comment(c: object) -> object:
    """Shorten comment dict to abbreviated keys, omitting timestamp and None values."""
    if not isinstance(c, dict):
        return c
    out = {}
    for k, v in c.items():
        if k in COMMENT_OMIT or v is None:
            continue
        out[COMMENT_KEY_MAP.get(k, k)] = v
    return out


def shorten_dep(d: object) -> object:
    """Shorten dep dict to abbreviated keys, omitting link_uuid and None values."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if k in DEP_OMIT or v is None:
            continue
        out[DEP_KEY_MAP.get(k, k)] = v
    return out


def shorten_signature(s: object) -> object:
    """Compact a signature record for the LLM view: the FACT of a signature, not
    the signature itself. Keeps presence + verified-step count + the env key
    fingerprint; drops the (already public_state-stripped) HMAC hex, algorithm,
    and verbose timestamps. Live validity is a `verify-signature` concern."""
    if not isinstance(s, dict):
        return s
    manifest = s.get("manifest")
    out: dict = {"present": True}
    if isinstance(manifest, list):
        out["steps"] = len(manifest)
    if s.get("key_id"):
        out["key"] = s["key_id"]
    return out


def shorten_inbound(d: object) -> object:
    """Shorten an inbound-link dict (from_id/relation) to abbreviated keys."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        out[INBOUND_KEY_MAP.get(k, k)] = v
    return out


def to_llm(state: dict) -> dict:
    """Convert a full ticket state dict to LLM-optimised format."""
    out = {}
    for k, v in state.items():
        if k in OMIT_KEYS:
            continue
        if v is None and k not in ALWAYS_EMIT:
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        short_k = KEY_MAP.get(k, k)
        if k == "comments":
            v = [shorten_comment(c) for c in v]
        elif k == "deps":
            v = [shorten_dep(d) for d in v]
        elif k == "inbound_links":
            v = [shorten_inbound(e) for e in v]
        elif k == "signature":
            v = shorten_signature(v)
        out[short_k] = v
    return out
