# ADR 0004: Reconciler producer↔consumer snapshot contract (anti-change-detector guardrail)

- **Status:** Accepted
- **Context:** Epic *Close the Jira-sync producer↔consumer & tested-vs-shipped
  test-coverage gap class* (`f89d-e54d-203d-4ee0`), story B (`8937-eed3-c881-4ea1`);
  builds on the hermetic fixtures of story A (`fe57-7712-1e3f-45f4`,
  [docs/jira-fixtures.md](../jira-fixtures.md)).

## Context

Three production Jira-sync bugs escaped this cycle, all the same CLASS — a gap at a
*seam* no test spanned. The fetcher (PRODUCER) writes a per-issue snapshot-entry
dict; the differs (CONSUMERS) read it. Each side was unit-tested in isolation
against **hand-built snapshot dicts in shapes the real fetcher never emits**:

- **0ee6** — the inbound differ read flat `comments`; the fetcher writes nested
  `comment`. Both halves were "correct" against their own fixtures; jointly they
  never matched.
- **3f04** — the snapshot never carried `issuelinks`; inbound link sync was
  structurally dead and outbound re-emitted every link each pass.

Deep research (Pact consumer-driven contracts; vcrpy; Schemathesis/Dredd/Gavel
structure-not-value validation; the Google SWE-book "verified fake" + its
change-detector warning; and how real Jira clients test — `jira-python`'s live
Dockerized sandbox vs `jira-cli`'s on-disk fixtures captured from real Jira served
through production serialization) converges on a layered strategy right-sized for a
single team. A web survey of `ankitpokhrel/jira-cli`, `atlassian-api/atlassian-python-api`,
`pycontribs/jira`, and `MrRefactoring/jira.js` confirmed the mainstream pattern is
**static captured JSON replayed in-process through the production deserialization
path** — exactly what story A built.

## Decision

1. **One producer↔consumer contract test, through the PRODUCTION path.** A single
   schema + round-trip test (`tests/integration/rebar_reconciler/test_snapshot_contract.py`)
   drives the story-A `FakeAcliClient` fixtures → `fetcher.fetch_snapshot` → the real
   differs. No parallel serialization, no hand-built snapshot dicts. This is the
   *keystone*: a key/shape divergence between the fetcher and a differ fails it.

   We explicitly **reject** the heavier options as overkill for a single team: full
   live-sandbox e2e (`jira-python`'s model) and cross-team Pact broker contracts.

2. **The snapshot-entry shape has a single source of truth.** It lives in
   `src/rebar/_engine/rebar_reconciler/_snapshot_schema.py` — a `TypedDict` (for
   readers) + a JSON Schema (`SNAPSHOT_ENTRY_SCHEMA`, validated with `jsonschema`).
   The module docstring tabulates which consumer reads each key. Both the fetcher's
   output and the differs' expectations target it. When a new field is added to the
   snapshot, update the schema first.

3. **Assert structure/type and semantic round-trip — never values or interactions.**
   This is the anti-change-detector rule (Google SWE-book): a test that pins exact
   payloads or mock-call-counts breaks on every benign edit and trains engineers to
   "update the golden" without thinking, so it detects *change* rather than *defects*.

## Rules every NEW reconciler test must follow

- **DO** assert a **semantic round-trip** (the consumer's behaviour changes with the
  producer's content — e.g. "inbound emits a link add for the producer's
  `issuelinks`", "the emitted comment-add count flips when the echo marker is
  stripped") or **structure/type** conformance against `_snapshot_schema`.
- **DO** drive fixtures through the production path (`fetch_snapshot` / the real
  differs), via the story-A `FakeAcliClient`. Don't hand-build snapshot dicts that
  the real fetcher would never emit — that is the exact gap 0ee6/3f04 exploited.
- **DON'T** assert **whole-blob golden equality** of a snapshot/payload, or pin
  **mock-call-counts / interaction order**. Those are change-detectors.
- **DON'T** assert specific field **values** as the contract; assert the **shape**
  and the **round-trip outcome**.
- A test that must temporarily encode a current gap uses `xfail(strict=True)` so it
  flips to a failure the moment the gap closes — never a green assertion that locks
  the bug in (story F de-encodes the historical probe guards).

New reconciler tests should reference this ADR. The fixtures' provenance and
re-capture cadence are in [docs/jira-fixtures.md](../jira-fixtures.md).

## Consequences

- The fetcher and the differs cannot drift on snapshot shape without a red test.
- Outbound/inbound are exercised through the code path production actually runs.
- The suite stays lean: one contract test + schema, zero change-detector goldens.
