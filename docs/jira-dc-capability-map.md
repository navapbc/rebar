# Jira DC capability map (ticket 259b-b7da-a346-4785)

An on-demand, `workflow_dispatch`-only CI job
(`.github/workflows/jira-dc-capability-map.yml`) that boots the pinned Jira Data Center
harness (`tests/external/live_jira_dc/`) and runs an agentic Opus mapping pass
(`scripts/jira_dc_capability_map.py`, built on `rebar.llm`'s own agent runtime — see
`docs/llm-framework.md` / `docs/reuse-surface.md` §2) against it, answering a fixed
checklist instead of an open-ended "go explore":

- issue-type inventory, project templates, and which one yields `Epic` (and whether the
  instance is even licensed for it);
- the `Epic Link` / `Epic Name` field ids, and whether they sit on the default edit/create
  screens;
- every workflow's statuses (name **and** category) and the transitions available from
  each, paired with their destination status — the exact "transition name is not the
  destination status name" distinction that caused bug 7f93;
- every value in rebar's four hardcoded Jira-family vocabularies
  (`LOCAL_PRIORITY_TO_JIRA`, `LOCAL_TYPE_TO_JIRA`, `LOCAL_STATUS_TO_JIRA`,
  `RELATION_TO_JIRA_LINK`), diffed against the live instance as present / absent /
  present-but-different, including an end-to-end link-**direction** experiment (`blocks` vs
  `depends_on` share one Jira link type name and differ only by endpoint order);
- the five hardcoded Jira length limits (summary, label, DC description, comment,
  the Cloud ADF margin), each measured live at limit-1/limit/limit+1 with a **read-back**
  comparison (the only way to catch silent accept-and-truncate);
- the `37e7`/`1a9f` link-direction/parent-clearing falsifiers, executed as raw REST calls.

## The answers, for the image we currently pin

**Everything below was measured on Jira DC 8.17.1 by run `30863672922`.** It is transcribed
from that run's `capability_map.json`, not from anyone's recollection, and every claim carries
the `req-NNNN` evidence ids that locate the raw request/response in the same run's
`evidence.json`. Because the harness image is **pinned by digest**, these answers are stable —
they change only when the pin does, which is exactly when the "When to re-run it" section below
says to regenerate them.

Read this section before designing anything against DC. Three of this epic's changes were
written against assumptions it falsifies.

### Instance identity

| | |
|---|---|
| version | 8.17.1 Server/Data Center, buildNumber 817001, build date 2021-06-15 [req-0000] |
| licensed | Jira Software (active), Jira Core; **not** Jira Service Management [req-0009] |
| measured against image | `sha256:04326628dc4ac36b2bfc1d0f2ebe5ba3807c1ec9cf9b18307d3c2ad7222537e9` |
| self-reported identity | the digest is **not** exposed over REST; the closest in-instance identifier is `scmInfo=36e93c711dfb14e6e1509a2fef6b04c4d73cc7ca` |

That digest is the pin on `tests/external/live_jira_dc/Dockerfile`'s `FROM` line, and recording
it here is what makes this page's staleness **checkable** rather than merely asserted:
`tests/unit/test_jira_dc_capability_map_workflow.py` fails the moment the Dockerfile is
re-pinned without this section being regenerated. Before that gate existed, a re-pin could land
with every answer below silently describing an image nothing runs — and since the instance does
not report its own digest, no live check could have caught it either. Treat a failure of that
cell as "re-dispatch the mapping job", never as "update the digest here".

### Epic machinery — field ids and templates

| | |
|---|---|
| `Epic Link` | `customfield_10001` |
| `Epic Name` | `customfield_10003` |
| `Epic Link` on the default **edit** screen | **No** |
| `Epic Name` on the Epic **create** screen | Yes (and required) |

All **three** software templates yield an `Epic` issue type, so the type is a provisioning
choice, never a platform limit: `gh-scrum-template` (Scrum), `gh-kanban-template` (Kanban), and
`basic-software-development-template` (Basic). The harness pins the Scrum one
(`tests/external/live_jira_dc/conftest.py`, `_PROJECT_TEMPLATE`).

> **Screen presence does NOT gate REST writes on this version.** `Epic Link` is absent from the
> standard edit screen [req-0034] and `PUT /rest/api/2/issue/{key}` with
> `{"fields":{"customfield_10001":"SCRUM-1"}}` still returned **204 and round-tripped** on
> read-back [req-0040][req-0041]. This is the single most load-bearing finding here: it is why
> the harness does **not** provision screen fields. Predicting a `400 "not on the appropriate
> screen"` here is the natural inference and it is **wrong**.

### Parent and hierarchy — the dangerous paths

> **A sub-task reparent reports success and does nothing.** Raw REST
> `PUT /rest/api/2/issue/SCRUM-6 {"fields":{"parent":{"key":"SCRUM-1"}}}` returns **HTTP 204**
> [req-0056], and read-back shows the sub-task **still parented to its original parent**
> [req-0058]. Accept-and-ignore, not rejection. **Any code path that reparents a sub-task must
> verify by read-back — the status code is a lie.**

Clearing a sub-task's parent fails, and the two spellings fail *differently* — which matters,
because it means they do not share a validation path:

| payload | result |
|---|---|
| `{"fields":{"parent":null}}` | 400 `data was not an object` [req-0055] |
| `{"update":{"parent":[{"set":null}]}}` | 400 `Field 'parent' cannot be set. It is not on the appropriate screen, or unknown.` [req-0057] |
| sub-task `editmeta` | exposes **no** `parent` field at all, so no declared operations [req-0054] |

Neither clears it. Whether it is clearable *by some other route* is **UNKNOWN** — the
second error is a screen-configuration shape, not a type error, so "intrinsically impossible"
is not supported by this evidence.

> **`Parent Link` (`customfield_10007`, Advanced Roadmaps) is a DIFFERENT field from `Epic
> Link`** [req-0038]. Any name-based lookup must match exactly and must not confuse the two.

#### RE-MEASURED 2026-08-04, live run `30951453979` (ticket 9f26)

Every claim above about `fields.parent` was re-asserted against the live instance rather than
read off this page, because this same epic RETRACTED the label-ceiling entry when a live cell
contradicted it. **A recorded measurement is a hypothesis until it is re-measured.**

The re-measurement needed no new probe — the failing cells' own text IS the measurement. Run
`30951453979` reports rebar asking for `RBJREXN-2`, Jira accepting, and a fresh REST read still
returning `RBJREXN-1` (and independently `RBJVZQW-3` -> `-2`, read back as `-1`). **req-0056 and
req-0058 hold: accept-and-ignore, confirmed, not inherited.**

**The supported parent mechanism, confirmed green in the same run.** The Epic Link on a
NON-sub-task child works in both directions and for both operations, so it — not `fields.parent`
— is the DC parent path, and it is what the parent cells are homed to:

| cell, all PASSED in `30951453979` | what it proves |
|---|---|
| `test_outbound_epic_parent_round_trips_via_the_epic_link` | transport-level Epic Link SET **and** CLEAR, raw-REST read-back |
| `test_outbound_epic_parent_reaches_dc_THROUGH_A_RECONCILE_PASS` | a real reconcile pass emits and lands it (req-0040/0041) |
| `test_inbound_clear_parent_round_trips` | an Epic Link CLEAR is observed inbound via `get_parent_map`'s Epic Link fallback |

This settles a question ticket 4b9e was holding work behind — whether `{epic_link: null}` clears
on DC. **It does**, and the evidence was already in the harness; the dedicated probe
(`scripts/jira_dc_epic_link_clear_probe.py`) was answering a question two green cells had
answered. Recorded so the next reader checks the harness before building a probe.

**Still UNKNOWN and deliberately not closed:** whether a sub-task's `fields.parent` is clearable
by some route rebar does not take (the 400 at req-0057 is a screen-configuration shape, not a
type error). Nothing here upgrades that to "intrinsically impossible"; it is pinned as a live
expectation by the sub-task-clear assertion inside `test_inbound_clear_parent_round_trips`, so a
DC that starts allowing it fails loudly.

### rebar's hardcoded vocabularies, diffed

Present and correct: all issue types (`Bug`, `Story`, `Task`, `Epic`), all five priorities
(`Highest`…`Lowest`), and the `Blocks` / `Relates` link types. Link **direction** was verified
end-to-end: `blocks` (swap=false) and `depends_on` (swap=true) resolve to true inverses of the
one `Blocks` type — no direction defect [req-0042][req-0043][req-0051][req-0052].

Every workflow is `Software Simplified Workflow for Project SCRUM` with exactly
**To Do / In Progress / Done**, all reachable via global transitions.

Known mismatches — filed as [rebar:2e47-ae62-c0cf-48a0], **do not re-file**:

- **status `IDEA` does not exist** in any workflow bound to any issue type. `LOCAL_STATUS_TO_JIRA['idea'] = 'IDEA'` has no target here [req-0033][req-0036].
- ~~**label limit is effectively 254, not 255.**~~ **RETRACTED — this claim DID NOT REPRODUCE.** A
  dedicated live cell (`test_transport.py::test_the_instance_label_ceiling_measured_at_254_and_255`)
  posted 254- and 255-character labels to this image and READ THEM BACK: **both were accepted**
  (harness run 30944241768, `[2e47-label-ceiling]` lines). So rebar's shared
  `JIRA_LABEL_MAX_CHARS = 255` is CORRECT for Data Center, there is no Cloud/DC divergence here, and
  req-0071/0072/0073's reading was wrong. The cell now pins the MEASUREMENT (255 accepted, 256
  rejected) so a future image that genuinely moves the ceiling fails loudly — which is precisely what
  the un-reproducible recorded claim failed to do.
- ~~**two distinct issue-type ids (10003 and 10004) are both named `Task`**, so name-based type
  resolution is ambiguous.~~ **RETRACTED — this claim DID NOT REPRODUCE.** Harness run
  **30951453979** (`main` @ `d394a70529`) read `GET /rest/api/2/issuetype` live (HTTP 200) and its
  `[2e47-issue-type-evidence]` line recorded the instance-wide entries as
  **`[('10002', 'Task'), ('10003', 'Sub-task'), ('10000', 'Epic')]`** — exactly **one** id named
  `Task`, and **10003 is `Sub-task`, not `Task`**. That emitter
  (`tests/external/live_jira_dc/conftest.py`) builds the list by filtering the full `/issuetype`
  response on the names `Task` / `Sub-task` / `Epic` with **no de-duplication**, so a second
  `Task` entry would have been printed had one existed.
  **This was NOT base-image drift:** the `FROM` digest at `d394a70529` is byte-identical to the
  one this page records, so that run measured the same pinned image these answers describe.
  What remains **undetermined** is *why* the two disagree. Issue-type ids are assigned as
  instance state is built, and the mapping run created a project from **each** candidate template
  while the harness provisions a **single** Scrum project — so the map's reading may reflect
  instance state its own broader provisioning produced, rather than a misreading of its evidence.
  Either way, the **conclusion** drawn from it does not hold for the environment the harness
  actually runs: on that provisioning the name `Task` resolves to exactly one id, so name-based
  type resolution is not ambiguous there. `_assert_project_capabilities` already fails
  provisioning loudly if a project ever *does* offer a duplicate name, which is the condition
  that would actually reach rebar.
  Note what made this checkable at all: a same-digest contradiction is invisible to a digest
  comparison, so the gate added by [rebar:259b-b7da-a346-4785] is what now **distinguishes** the
  two explanations — with the pin asserted, "different image" is ruled out mechanically instead of
  being argued. Closing the remaining half (a wrong answer at an *unchanged* digest) needs the
  deterministic, non-LLM re-measurement pass filed as [rebar:a9bd-6641-e603-42bc]; this is the
  **third** entry on this page contradicted by live measurement.
- **three rebar relations are unrepresentable** on stock Jira: `supersedes`, `discovered_from`, `caused_by`. Conversely the instance ships a stock `Duplicate` type that rebar does not map [req-0004].

### Length limits, measured at limit-1 / limit / limit+1 with read-back

| field | rebar constant | measured | verdict |
|---|---|---|---|
| summary | 254 | 254 accepted, 255 rejected (hard 400) | **matches** |
| label | 255 | 254 accepted, 255 **accepted**, 256 probed (run 30944241768) | **matches** — the earlier "mismatch" is RETRACTED above |
| description / comment | 32767 | governed by `jira.text.field.character.limit=32767`, enforced (non-zero) [req-0006] | **coverage gap** |

No silent truncation was observed anywhere — over-limit writes are hard 4xx. The description
boundary is a **recorded gap**: the literal 32K+ payload was not exercised in that session, so
32767 is confirmed as the configured limit but not as an observed boundary.

### Administrative hooks that DO work

Both were exercised and confirmed by read-back, so they are available if template pinning ever
stops being sufficient:

- `POST /rest/api/2/issuetype` creates a new issue type (201) under the admin PAT [req-0076].
- `POST /rest/api/2/screens/{id}/tabs/{id}/fields` adds a field to a screen, and the field then
  appears on that tab [req-0078][req-0080]. **Possible, but not needed** — see the screen-gating
  note above.

### Harness limits worth knowing

Jira DC caps a user at **10 Personal Access Tokens**, with no Cloud analogue. The
`jira_dc_pat` fixture is session-scoped for this reason, and the harness sweeps leaked tokens
on startup.

## What it produces

An artifact (`jira-dc-capability-map`) with three files: `capability_map.json` (the
agent's structured answer to every checklist item, each field carrying the
`evidence_ids` of the raw REST calls that back it), `evidence.json` (every raw
request/response the agent made, in call order), and `run_metadata.json` (the image
digest, base URL, model, and call count).

**The artifact is authored, not live.** A human reviews it and is responsible for landing
whatever should become committed data — the answers section above, and the harness's own
declared provisioning contract (`_PROJECT_TEMPLATE` / `_REQUIRED_FIELDS` in
`tests/external/live_jira_dc/conftest.py`, enforced before any test runs by
`_assert_project_capabilities`, per bug 3fe5). The workflow itself never commits anything, and
the agent is explicitly instructed to *report* mismatches as findings, never to fix the
harness, the repository, or rebar's own configuration. Any finding that implicates one of
rebar's own hardcoded vocabularies or limits should be filed as a ticket citing the
relevant `evidence_ids`, rather than acted on directly from the artifact.

## When to re-run it

Re-run whenever the harness's pinned base image changes — i.e. whenever
`tests/external/live_jira_dc/Dockerfile`'s `FROM …@sha256:…` digest is bumped. The
mapping job builds that same Dockerfile, so it always maps the image the harness will
actually run against; a stale map after a re-pin describes an image nothing runs anymore.
You do not have to remember this: the digest recorded above is asserted against the
Dockerfile's pin by a unit test, so a re-pin lands red until the map is regenerated.

To see which image a run *would* map without paying for one, `python
scripts/jira_dc_capability_map.py --print-digest` reads the pin and exits — no container, no
API key, no LLM. The digest in the artifact's `run_metadata.json` comes from that same
derivation rather than a constant, so it cannot drift from the image actually built.
It is also reasonable to re-run after any change to rebar's own Jira-family vocabularies
(`src/rebar/_engine/rebar_reconciler/adapters/jira_family/value_maps.py` and neighbors) to
re-confirm the diff still reads clean.

Dispatch from the Actions tab (needs the `ANTHROPIC_API_KEY` repository secret); never on
push, PR, or schedule — `tests/unit/test_jira_dc_capability_map_workflow.py` asserts the
trigger is `workflow_dispatch`-only.
