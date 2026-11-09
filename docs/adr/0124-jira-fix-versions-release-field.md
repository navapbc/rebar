# ADR 0124 — Jira fix versions sync into rebar as a `releases` list

**Status:** Accepted
**Date:** 2026-11-09
**Ticket:** `polonium-autophobic-anchovy` / `0f4c-4586-8f3e-432d`
**Epic:** `inferior-seaborgium-aruanas` / `21ec-3e9b-4a27-4775`

## Context

Teams plan and ship work by release in Jira, through the issue field `fixVersions`. rebar
has no release concept, so the bridge drops that field: the bridge's field contract
(`src/rebar/reconciler/_field_contract.py`) registers no `fixVersions` entry, compiled ticket
state (`src/rebar/reducer/_state.py`, `make_initial_state`) has no release field, and no CLI,
MCP or library surface can show, filter or set one. rebar therefore cannot answer "what is
in release X?" without Jira.

The epic's operator settled the product decisions on 2026-11-05: sync in both directions; a
list of release names, not a single value; names only, with no release records; a missing
Jira version raises a `BRIDGE_ALERT` and is retried each pass, and the bridge never creates
versions; Jira's "Affects versions" field is out of scope; and no inheritance through the
parent hierarchy. The operator also directed that the list sync the way tags sync with Jira
labels. This ADR records those decisions and settles the points the epic delegated to it:
the replayed event that carries the field, the field's symmetry class, how name identity and
renames behave, and what happens to archived and deleted versions.

The tags↔labels pattern this follows, as it stands on `main`:

- **Storage and replay.** Tags are a list in compiled state, changed only by per-event
  deltas: `TAG_DELTA {added, removed}`, folded by `process_tag_delta`
  (`src/rebar/reducer/_processors.py`), removals first and then additions, so an add beats a
  remove in one event and replay is deterministic.
- **Inbound.** `_diff_labels_inbound` (`src/rebar/reconciler/inbound_collection_diffs.py`)
  set-diffs Jira labels against local tags. `_inbound_update_apply_labels`
  (`src/rebar/reconciler/apply_inbound_events.py`) writes a `TAG_DELTA` marked
  `source="inbound"`, reading current state only to suppress no-ops, and skips the write
  rather than assume an empty set when the reducer fails.
- **Outbound.** `_diff_labels` (`src/rebar/reconciler/outbound_labels.py`) emits a REMOVE
  only for a value in the local ever-seen intent set from `local_label_intent`, which skips
  inbound-origin events (bug a06c). A value Jira added and rebar never had is never removed
  from Jira.
- **Jira writes.** Per-item update verbs, never a whole-list set, so a concurrent Jira-side
  edit is not overwritten.
- **Same pass.** When both directions touch one value in a pass, the outbound change wins
  and the inbound one is suppressed (`compute_inbound_mutations`,
  `src/rebar/reconciler/inbound_differ.py`).

## Decision

1. **Field shape.** Compiled ticket state gains `releases`: a list of Jira version names
   (strings), default `[]`, kept in insertion order with no duplicates. A release is a name
   only. rebar stores no release record, no released flag and no dates; those stay in Jira.

2. **Event carrier: a new `RELEASE_DELTA` event.** Its body is
   `{"added": [...], "removed": [...]}` plus `"source": "inbound"` when the reconciler writes
   it. A new `process_release_delta` folds it with exactly `process_tag_delta`'s rules:
   non-list bodies act as empty, non-string or empty names are skipped, removals apply
   first and then additions, and a repeated fold changes nothing. No writer replaces the
   whole list, and `EDIT.fields` never carries `releases`, because `EDIT` replays
   last-writer-wins and would drop a concurrent addition.

   *Rejected: generalizing `TAG_DELTA` with a field selector.* A clone running an older
   rebar knows `TAG_DELTA` but not the selector, so it would fold release names into `tags`
   — silent corruption on every older reader. A new event type instead takes the existing
   preserve-and-ignore path for unknown types (`src/rebar/reducer/_version.py`): an older
   clone keeps the file, ignores its effect, and compaction never absorbs it.

   *Schema version 6, readers before writers.* `SCHEMA_VERSION` goes from 5 to 6 because
   a new event body type is added, as v3 did for `TAG_DELTA`. Rollout follows the rule in
   `docs/event-schema.md` ("Schema version & forward compatibility"): upgrade the reconcile
   host before any writer emits `RELEASE_DELTA`, so the bridge never reduces a ticket while
   blind to its releases. `SNAPSHOT` compaction needs no change: it stores the reduced state
   and `process_snapshot` restores every key, so `releases` survives compaction and a
   pre-feature snapshot replays to `[]`.

3. **Symmetry class.** `FIELD_CONTRACT["releases"]` is `ADD_WINS` with conflict class `set`
   and `outbound_name="fixVersions"`, the class `labels` has. `conflict_resolver.FIELD_CLASSES`
   gains `"releases": "set"`. The contract property tests cover it like labels.

4. **Inbound (Jira → rebar).** The reconciler reads `fixVersions[].name` from each bound
   issue, set-diffs the names against local `releases`, and writes one `RELEASE_DELTA` with
   `source="inbound"`, reading current state only to suppress no-ops and skipping the write
   when the reducer fails. When the snapshot does not carry the `fixVersions` key at all, the
   diff is empty: an unobserved field is never read as "all releases removed".

5. **Outbound (rebar → Jira).** The reconciler adds each local name the issue lacks, and
   removes an issue name rebar lacks only when the name is in the local ever-seen release
   intent set — names from non-inbound `RELEASE_DELTA.added` and from `SNAPSHOT` state.
   Jira writes use per-item update verbs only:
   - Cloud: `PUT /rest/api/3/issue/{key}` with
     `{"update": {"fixVersions": [{"add": {"name": "<name>"}}]}}`, or `remove`;
   - Data Center: `issue.update(update={"fixVersions": [{"add": {"name": "<name>"}}]})`, the
     verb the Data Center label remove already uses.
   Neither path ever sends `set` or `fields.fixVersions`.

6. **Same-pass conflicts.** Outbound wins, as for labels: an outbound ADD of a name
   suppresses the same pass's inbound REMOVE of it, and an outbound REMOVE suppresses the
   inbound ADD. For releases, the outbound context records both the ADDs sent to Jira and
   the ADDs withheld under decision 8, so a withheld name is never stripped locally by the
   inbound set-diff.

7. **Identity is the name; renames are remove plus add.** Jira version names are unique
   within a project, so a name identifies a version for this purpose. When a version that
   reached rebar from Jira is renamed, its issues carry the new name and the next pass
   removes the old name and adds the new one on the bound tickets. When a name set from
   rebar is renamed in Jira, the old name no longer exists in the project, so decision 8
   withholds it. Because decision 6 records withheld ADDs in the outbound context, the
   inbound REMOVE of the old name is suppressed: the old name stays on the rebar ticket with
   an alert, and the new name arrives inbound beside it, until someone removes the old name.

8. **Missing version: withhold, alert, retry; never create.** The bridge reads each bound
   project's version list once per pass (Cloud `GET /rest/api/3/project/{key}/versions`;
   Data Center `project_versions`). It does not send an outbound ADD for a name the project
   lacks. The name stays on the rebar ticket, and the bridge writes one `BRIDGE_ALERT` per
   ticket and name with reason `release-version-missing:<name>`. Every pass re-checks; once
   the name is written, removed from the ticket, or otherwise no longer withheld, the
   bridge writes a resolving `BRIDGE_ALERT`. A steady state writes nothing. A withheld name
   is recorded as an outbound ADD in the outbound context (decision 6), so the inbound
   set-diff cannot strip it.
   When the version list cannot be read, availability is unknown for that project and pass,
   and the bridge neither withholds-and-alerts nor resolves anything there, so a Jira outage
   cannot raise alerts. No code path creates a Jira version: versions are created in Jira by
   people.

9. **Archived and deleted versions.** An archived version is withheld from outbound ADD
    like a missing one, with alert reason `release-version-archived:<name>`. What Jira's edit
   API does with an archived version is unverified — no version exists on the site to test,
   and the Atlassian pages checked on 2026-11-09 do not state it — and withholding is the
   outcome that cannot write an unexpected value. An archived name already on a Jira issue
   still syncs inbound, and an outbound REMOVE of it is attempted normally. Released
   versions are ordinary versions. A deleted version is absent from the project list, so
   decision 8 applies; when Jira strips a deleted version from its issues, the inbound diff
   removes the name locally.

10. **Out of scope.**
    - Jira "Affects versions" (`versions`) is not synced, and rebar has no affected-release
      field.
    - No inheritance through the parent hierarchy, mirroring Jira, where Fix versions does
      not propagate from an epic to its children. `list --release X` returns exactly the
      tickets whose own `releases` contain X.
    - No config key: the field syncs whenever the bridge runs, and a store with no Jira
      bridge simply has empty `releases`.

11. **Surfaces.** `rebar edit --add-release/--remove-release/--set-releases` (the set form
    computes a delta against the observed list, add-wins, like `--set-tags`);
    `rebar list --release` (comma-separated, any match); `releases` in `rebar show` and the
    lean list rows; the library `edit_ticket` keywords `add_releases`/`remove_releases`/
    `set_releases` and `list_tickets(release=...)`; and the matching MCP parameters, with
    `releases` inside the read tools' declared `outputSchema`s.

## Evidence

Read-only checks against the production Jira Cloud site, 2026-11-09 (PST):

- `GET /rest/api/3/field`: `fixVersions` has schema
  `{"type": "array", "items": "version", "system": "fixVersions"}`; `labels` is
  `{"type": "array", "items": "string", "system": "labels"}`.
- `GET /rest/api/3/issue/REB-7015/editmeta`: no `fixVersions` entry. `labels` lists the
  operations `add`, `set` and `remove`.
- `GET /rest/api/3/project/{key}/versions` for all 144 projects visible to the bridge
  account: 0 versions in every project. `POST /rest/api/3/search/jql` with
  `fixVersion is not EMPTY`: 0 issues. No project on the site can therefore exercise a
  per-item `fixVersions` add or remove without first creating a version, which this epic
  does not do in production. The production-verification child is the first live write.
- Atlassian's "Updating an Issue via the JIRA REST APIs" page lists Fix Versions as
  supporting Set, Add and Remove, by version id or name.
- The Jira Cloud OpenAPI description of `PUT /rest/api/3/issue/{issueIdOrKey}` says the
  endpoint "doesn't check screen configurations to determine if a field is editable".

**Contradiction to resolve live.** The epic lists adding Fix versions to REB's edit screen
as a prerequisite for rebar→Jira writes, based on the editmeta above; the edit endpoint's
own description says it does not check screen configuration. The production-verification
child records which holds. Until then, the screen change stays listed as a prerequisite in
`docs/jira-sync-setup.md`.

## Consequences

- A team can read and set release membership in rebar, and `list --release` answers "what
  is in release X?" offline.
- Releases inherit labels' known trade-off: a Jira-side removal of a name rebar set is
  undone by the next pass (local wins), because no per-field baseline arbitrates
  collections. This keeps one rule for both collections.
- A version renamed in Jira after rebar set it leaves the ticket carrying both names (the
  new one arrives inbound) and an alert naming the old one until someone removes it. The
  alert makes the drift visible instead of silent.
- `RELEASE_DELTA` adds one replayed event type and schema version 6; older clones keep the
  events and ignore them until they upgrade.
