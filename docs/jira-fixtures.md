# Hermetic-but-honest Jira fixtures

Epic f89d, story A (`fe57-7712-1e3f-45f4`). The Jira-sync bug class
(0ee6 nested-`comment`, 3f04 absent-`issuelinks`) all began the same way: unit
tests fed the differs **hand-built `jira_snapshot` dicts in shapes the real
fetcher never produces**. Producer and consumer were each unit-tested in
isolation against fictional shapes, so their shared contract was never jointly
exercised.

The fix (modelled on `jira-cli`'s pattern) is **fixtures captured from real Jira
payloads, served through the production serialization path** — never
hand-massaged. This page documents what the fixtures are, how the fake replays
them, and the re-capture cadence that keeps them honest.

## What's committed

`tests/fixtures/jira/` holds **scrubbed real Jira REST payloads** for the exact
four responses `fetcher._build_snapshot` consumes:

| File | Produced by (`AcliClient` method) | Shape |
|------|-----------------------------------|-------|
| `search.json` | `search_issues` | list of issue dicts (`{"key", "fields": {...}}`) |
| `comment_map.json` | `get_comment_map` | `{key: {"comments": [...], "total": N, ...}}` (nested `comment` field) |
| `issuelinks_map.json` | `get_issuelinks_map` | `{key: [issuelink, ...]}` (REST-nested `type`/`inwardIssue`/`outwardIssue`) |
| `parent_map.json` | `get_parent_map` | `{key: parent_key \| None}` |
| `_meta.json` | — | capture provenance (project, JQL, key set) |

**Why per-endpoint maps (not one nested `/issue` blob).** The split into
`search.json` + `comment_map` + `issuelinks_map` + `parent_map` is not arbitrary —
it **mirrors the production fetch decomposition**. `_build_snapshot`'s base search
(`search_issues`) deliberately omits `comment`/`issuelinks`/`parent` (ACLI's field
selector rejects them / they bloat the page), so the fetcher issues *separate* REST
searches (`get_comment_map` / `get_issuelinks_map` / `get_parent_map`) and merges
each field in. The fixtures are keyed the same way so the fake replays exactly what
each production call returns — there is no fully-nested `fields.comment` payload to
mirror because production never fetches one. (Peers do the same where they fetch
separately: `jira-cli` keeps `issue-link-types.json` as its own fixture file.) Each
map still preserves the **raw** Jira field shape, so per-field fidelity is intact.

The curated key set (see `DEFAULT_KEYS` in the capture script) is a small, stable
neighbourhood of REB issues that **jointly exercises every enrichment**: nested
comments, issue-links (including REB-430's authoritative **empty** list), parent
links (including REB-430's **top-level `None`**), and a non-null assignee
(REB-407). Kept small so the committed fixtures stay reviewable.

> The fixtures are an **honest mirror of real Jira shapes**. Do **not** hand-edit
> them — that reintroduces exactly the fiction-shape bug class this story closes.
> Change them only by re-capturing (below). Comment lists are capped to the most
> recent `_MAX_COMMENTS_PER_ISSUE` per issue to stay lean; every retained comment
> keeps its full real shape (ADF body, author dict, ids).

## How the fixtures reach production code

`tests/integration/rebar_reconciler/_fakes.py` provides `FakeAcliClient`, which
returns the fixtures **verbatim** from the production method signatures. It is
installed via the established seam — patching `fetcher._load_acli` to return a
stub module whose `AcliClient(**kwargs)` factory yields the fake:

```python
from _fakes import install
install(monkeypatch, fetcher)          # route _load_acli -> FakeAcliClient
snapshot = fetcher.compute_snapshot("pass-id")   # real enrichment merge runs
```

Because the fetcher builds its own client internally (it accepts no injected
client), this `_load_acli` patch is the **only** seam that drives the fixtures
through `_build_snapshot`'s parent/comment/issuelinks merge — the production code
where the shape bugs lived. Nothing reads the fixtures and hand-reshapes them.

## Secret-scrubbing

Capture runs a **layered redaction** (vcrpy-style) over every payload:

1. **key-based** value replacement for structured secrets/PII — `emailAddress`,
   `accountId`, `displayName`;
2. a **regex sweep** over every remaining string for the concrete leakage vectors
   that hide in free-form URL/`self`/avatar values: emails, raw + `%3A`-encoded
   Jira accountIds, and the **org-identifying Jira tenant host**
   (`<tenant>.atlassian.net` → `example.atlassian.net`). The generic internal
   cluster hosts (`*.prod.atl-paas.net`) are left intact — they are not
   org-identifying and keep the fixtures shape-honest.

Both layers **preserve the JSON shape** (keys + value types) — only secret
*values* change. `tests/integration/rebar_reconciler/test_jira_fixtures.py`
re-asserts the scrub held with **complementary, broader** detectors (not copies of
the scrubber's regexes), so a narrowed scrubber regression surfaces in the test
instead of sharing the scrubber's blind spot.

> **Re-capture PII caveat.** The current capture set is rebar's own dogfood
> tickets with agent-generated comment bodies, so no human PII appears in
> free-form text. If you re-capture issues with **human-authored comments**,
> review the bodies: the regex sweep catches emails but not bare display names in
> prose. Likewise, Atlassian accountIds also appear as bare 24-hex `objectId`s on
> some account vintages; the scrubber targets the `<digits>:<uuid>` form the REB
> tenant uses. Broaden the scrubber (and re-run the scan test) if a re-capture
> introduces either vector.

## Re-capture procedure & cadence

Capture is **live-gated and read-only** (it never writes to Jira):

```sh
REBAR_CAPTURE_JIRA_FIXTURES=1 JIRA_PROJECT=REB \
    python scripts/capture_jira_fixtures.py            # default key set
# or pass explicit keys:
REBAR_CAPTURE_JIRA_FIXTURES=1 JIRA_PROJECT=REB \
    python scripts/capture_jira_fixtures.py REB-431 REB-430
```

Requires the standard Jira credentials (`JIRA_URL` / `JIRA_USER` /
`JIRA_API_TOKEN`) the reconciler already uses, plus the `acli` CLI for
`search_issues`.

**Re-capture when** the Jira REST shape the fetcher consumes may have changed:

- a Jira Cloud REST API change to comments / issuelinks / parent / search;
- a new enrichment field added to `_build_snapshot` (extend `DEFAULT_KEYS` /
  the capture to cover it);
- the snapshot-entry schema (`_snapshot_schema.py`, story B) gains a field.

After re-capturing, commit the regenerated fixtures and let
`test_jira_fixtures.py` (scrub + production-path) and `test_snapshot_contract.py`
(story B, producer↔consumer round-trip) gate the refresh. If a re-capture makes
the contract test fail, that is the **signal working as designed**: the real Jira
shape and a consumer's expectation diverged — fix the consumer (or the schema),
don't paper over the fixture.
