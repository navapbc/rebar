# ADR 0032 — Adjective-adjective-animal aliases for new tickets

**Status:** Accepted (ticket 9408-861a-405d-4131 — dendrological-superhistoric-grizzlybear)
**Date:** 2026-07-06

## Context

Ticket aliases are the human-friendly handles for tickets (e.g. the `dark-acme-lumen`
form shown throughout `show`/`list`). They are computed deterministically from a
ticket's 16-hex id by `rebar._alias.compute_alias`, which draws from the bundled
`ticket-wordlist.txt` (an adjectives section + a `# NOUNS` section) and produces an
**adjective-noun-noun** string. The alias is persisted onto the CREATE event at
create time, and recomputed at read time (`_ids.py`, `reducer/_processors.py`) as a
backfill for any ticket that has no stored `data.alias` (tickets predating the alias
feature).

We want new tickets to use a friendlier, more memorable **adjective-adjective-animal**
form, sourced from an established OSS word list we don't have to curate ourselves,
while leaving every existing ticket's alias exactly as it is today.

## Decision

Add a second, independent generator rather than change the existing one:

- **New generator** `compute_genesis_alias(ticket_id)` in `src/rebar/_alias.py`
  produces `adjective-adjective-animal`, backed by a new bundled wordlist
  `src/rebar/_engine/resources/ticket-wordlist-v2.txt` (adjectives, a `# ANIMALS`
  marker, then animals). It has its **own** cache/warn globals (`_WORDS_V2_CACHE`,
  `_WARNED_MISSING_V2`) so it never touches the legacy loader's state. The two
  adjectives are guaranteed distinct (on a modulo collision the second index is
  bumped by one). It is deterministic off the hex id, returns `None` for a
  too-short (<12 hex) id, and falls back to a hex alias with a one-shot WARN when
  the wordlist is unavailable — matching the legacy loader's failure behaviour.

- **Wired into the create path only.** `_commands/composer.py`'s `_compute_alias`
  (called by `create_core`) now calls `compute_genesis_alias`. Because `create_core`
  persists the result onto the CREATE event, new tickets lock in the new format for
  life.

- **Legacy path is byte-identical.** `compute_alias` and `ticket-wordlist.txt` are
  unchanged, so the read-time backfill for pre-existing tickets (which have no stored
  alias) produces exactly the same adjective-noun-noun alias as before. No existing
  ticket's alias changes.

- **Word list provenance.** The v2 wordlist is vendored from the gfycat-style lists in
  [`a-type/adjective-adjective-animal`](https://github.com/a-type/adjective-adjective-animal)
  (MIT, © 2021 Grant Forrest). Words are lowercased and non-alphabetic tokens dropped;
  otherwise the lists are used verbatim (no hand curation). The upstream license is
  vendored to `docs/licenses/adjective-adjective-animal-LICENSE.txt`.

### Why a new function, not a version flag on `compute_alias`

The legacy function is on the hot read-time backfill path for every alias-less ticket.
A version flag would either need call sites to know which era a ticket is from (they
don't, without a reduce) or risk the old and new wordlists sharing one cache and
clobbering each other. A separate function with separate cache state keeps the legacy
path provably unchanged and the two formats fully isolated.

## Consequences

- New tickets get aliases like `dendrological-superhistoric-grizzlybear`. Some gfycat
  animal names are long compound words, so a minority of aliases are longer than the
  legacy form; this is inherent to the chosen list and was accepted.
- Alias uniqueness is still not guaranteed (≈1.4×10¹¹ combinations); aliases remain
  mnemonic helpers, and resolution is by exact-string match, so the format change does
  not affect the id resolver, the Jira-key regex, or the canonical-id contract.
- `delete.py`'s reference-scan fast-path now searches for **both** alias forms (legacy
  and v2), since it cannot tell a deleted ticket's era without a reduce.
- Both wordlists ship in the package. If the v2 list is ever refreshed from upstream,
  do it by re-vendoring — do not hand-edit — to keep the "not curated by us" property.
