# rebar — agent guide

rebar is an event-sourced ticket system + Jira reconciler exposed as a Python
library (`import rebar`), a CLI (`rebar`), and an MCP server (`rebar-mcp`), all
over one git-backed store. This guide is for **agents** driving rebar (especially
over MCP). For internals see `docs/architecture.md`, `docs/event-schema.md`, and
`docs/concurrency.md`. For the LLM/agent surfaces see `docs/llm-framework.md`, the
workflow-engine usage guide `docs/workflow-engine.md` (when to author a workflow vs a
bespoke op, the YAML DSL, the three-pass review pattern, the prompt-library + eval
seam), the reusable-machinery API reference `docs/reuse-surface.md` (signing + LLM
runtime + prompt/contract + output-schema seams), and the plan-review gate
`docs/plan-review-gate.md`. When developing rebar itself — running the gates/LLM
ops or testing config behavior — run the **repo checkout's** build, not a global
install (a stale global build silently ignores newer config keys and may lack the
`[agents]` extra): see `docs/local-dev-env.md`. **Bootstrap the env with `make install`
(not a bare `pip`/`uv pip install`)** so the pre-commit hook is wired — that is the commit
gate that runs `make lint` (ruff check + format-check) on every `git commit`. A bare
editable install skips it, and lint/format errors then slip through to CI; if you are in a
checkout set up that way, run `make hooks` once to (re)install and verify the hook.

> **Record your work in rebar, not in scratch notes.** Before starting, `search`/
> `list` for an existing ticket; if none fits, `create` one and capture the plan
> (and its acceptance criteria) in the description. As you work, write progress,
> decisions, and emergent findings back as `comment`s on the ticket (and `create`
> + `link … discovered_from` for new work you uncover), so the plan and its trail
> live in the store — durable, shared on every write, and visible to other agents
> — rather than in ephemeral TODOs or commit messages alone. Close with
> `transition <id> in_progress closed` when the acceptance criteria are met.
>
> **CLAIM BEFORE YOU WORK — always.** Every unit of work must have a ticket that YOU
> hold in `in_progress` *before* you touch code, run gates, or open a PR for it. Run
> `claim <id> --assignee <you>` (which atomically moves `open → in_progress` and sets the
> assignee) as the FIRST step of working a ticket — never start editing against an `open`
> (unclaimed) ticket, and never leave active work running under a ticket still marked
> `open`. This applies at the level you are actually working: claim the **story/task/bug**
> you are implementing, and when you begin executing an **epic**, move the epic itself to
> `in_progress` too (claim it, or `transition <epic> open in_progress`) so the board
> reflects that work is live under it. If you cannot claim (a `ConcurrencyError`/exit 10
> means someone else holds it, or a gate blocks the claim), resolve that FIRST — pick
> another ticket, or earn the required attestation — rather than working unclaimed.

## The parallel-agent workflow

```
list / search ──▶ ready ──▶ next-batch ──▶ claim ──▶ (work) ──▶ transition closed
                                              │
                                   discovered new work? ──▶ create + link discovered_from
```

1. **Find work** — `search <query>` (full-text over titles/descriptions/comments/
   tags) or `list --status=open`; `ready` returns tickets whose blockers are all
   closed; `next-batch <epic>` returns a conflict-aware unblocked batch (uses each
   ticket's recorded file-impact).
2. **Grab work atomically** — `claim <id> --assignee <you>`. This is the
   concurrency primitive: it moves an **open** ticket to `in_progress` and sets the
   assignee in one atomic step. If another agent already claimed it you get a
   **ConcurrencyError / exit 10** — do not retry the same ticket; pick another.
   Never hand-roll claim as `transition`+`edit` (that races).
   **Parent-first cascade:** if the ticket has a parent that is still **`open`**,
   the claim first claims the parent (recursively up the chain, with the same
   assignee) **before** the child — you can't be working a child while its parent
   is still merely open. A parent already `in_progress`/`closed`/`blocked` is left
   as-is. If the parent claim fails, the **child is not claimed** and the error
   names the **parent** as the cause. The same cascade applies to a bare
   `transition <id> open in_progress`. See "Parent-first claim/transition cascade".
3. **Record provenance** — when work surfaces new work, `create` the ticket and
   `link <new> <parent> discovered_from` so the emergent-work trail is captured.
4. **Finish** — `transition <id> in_progress closed` (optimistic-concurrency:
   pass the status you believe is current; a mismatch is exit 10). Use `reopen`
   to move a closed ticket back to open.

## The `idea` status (a parking lot for undesigned work)

`idea` is a first-class ticket **status** (any ticket type can hold it) for future
work that is **captured but not yet designed or ready to implement** — a durable
parking lot, distinct from `open` (which means "designed enough to work; eligible
for `ready`/`next-batch`"). It exists because the only other pre-work status is
`open`, and an `open` ticket is immediately claimable work; `idea` gives you a place
to record a rough idea without it becoming dispatchable. It is a status rather than a
tag deliberately: `claim` only accepts `open` tickets, so an `idea` ticket is
**structurally unclaimable** with no genesis window where it is momentarily `open`.

- **Transitions are free.** rebar does not enforce a rigid state machine — you can
  `transition <id> open idea`, `idea open`, `idea in_progress`, etc. (`idea` is a
  valid `current`/`target` status everywhere `transition` is used).
- **Excluded from dispatch (by omission).** `idea` tickets **never** appear in
  `ready` or `next-batch` — those surfaces only consider `open`/`in_progress`, so an
  undesigned idea is never scheduled as parallel work.
- **Fully listable/searchable.** `list --status=idea` returns them and `search`
  matches them, so ideas can always be found and later promoted (`idea → open`).
- **`idea → closed` skips the completion gates.** Rejecting/dropping an idea closes
  with **no** completion-verifier / signature / bug-close-reason gate (an undesigned
  idea has nothing to verify) — but the **structural open-children guard still
  holds** (you cannot close a parent that has open children).
- **Exempt from noisy `validate` findings.** `idea` tickets do not contribute
  empty-epic / orphan / missing-description / interface-contract / count findings to
  the store-health score (an idea is *expected* to be loosely specified); genuine
  structural checks (e.g. cycles) still apply.
- **Jira: `idea ↔ IDEA`.** `idea` round-trips to the Jira status `IDEA` through the
  reconciler, subject to the usual workflow-transition prerequisite (the target Jira
  workflow must permit the transition into `IDEA`) — see
  [docs/jira-sync-setup.md](docs/jira-sync-setup.md) "The `idea` status ↔ Jira `IDEA`"
  for the operator prerequisite, deployment sequencing, and the convergence quirk.
- **Capture in one atomic step.** `rebar idea "<title>"` (and the MCP `create_idea` /
  library `rebar.idea(...)`) creates a ticket **directly** in status `idea` in a
  single genesis event — never momentarily `open`/claimable.

## Gate protocols (MANDATORY)

These two gates are not advisory — adhere to them strictly.

**Plan-review protocol (before you claim).** A ticket must have a **successful plan
review before it can be claimed**. Run `rebar review-plan <id>` (MCP `review_plan`)
first. If the review **fails** (a BLOCK verdict), you must **remediate the failure
and re-run the review** until it passes — do not claim a ticket with a failing or
absent review. Even on a review that **passes**, you must **remediate all valid
advisory findings** before continuing (triage each: fix the valid ones; a finding
you judge invalid must be justified, not silently ignored). The coaching notes tell
you the productive next move per finding.

**Completion-verifier protocol (to close).** Closing a work ticket runs the
completion verifier. If it **fails**, you must **remediate the failure and try to
close the ticket again** — repeat until it passes (the signed verdict is the proof
the criteria are met).

## MCP tool set

**Reads (always available):** `show_ticket`, `list_tickets`, `search`,
`recent_session_logs`, `ticket_deps`, `ready_tickets`, `next_batch`,
`clarity_check`, `check_ac`,
`quality_check`, `validate`, `get_file_impact`, `get_verify_commands`,
`verify_signature`, `fsck`, `summary`, `bridge_fsck`, `reconcile` (dry-run by
default), `get_workflow_status`, `get_workflow_result`, `render_workflow` (the
workflow-engine read tools — run status/result by `run_id`, and a dry-render of a
`.rebar/workflows/*.yaml` workflow), `grounding_info` (the code-grounding oracle's
static integration contract — the closed dimension-ID vocabulary + version, the
reference kinds, the closed abstain-reason enum, and the available backends + their
detected versions; a fast, repo-independent read — see
[docs/grounding.md](docs/grounding.md)). The
typed read tools advertise an `outputSchema` (a documented, validated return
shape) drawn from the canonical JSON Schemas — see
[docs/output-schemas.md](docs/output-schemas.md).

**Writes (gated by `REBAR_MCP_READONLY=1`):** `create_ticket`, `create_idea`
(capture an undesigned idea — an `epic` born in status `idea` in one CREATE),
`transition_ticket`, `claim_ticket`, `reopen_ticket`, `comment_ticket`,
`edit_ticket`, `link_tickets`, `unlink_tickets`, `tag_ticket`, `untag_ticket`,
`archive_ticket`, `compact_ticket`, `set_file_impact`, `set_verify_commands`,
`log_session` (capture helper — appends a verbose entry to the current
`session_log`, creating one on first use),
`sign_manifest` (HMAC-signs a manifest of verified steps with the environment key;
`verify_signature` certifies it — pass `kind` to certify a specific attestation kind,
e.g. `plan-review` / `completion-verifier`), and `run_workflow` (executes a
`.rebar/workflows/*.yaml` workflow against a ticket — a lean-runtime capability
that does not itself need the `[agents]` extra, though individual LLM steps do).

> **Attestations are kind-keyed + additive (epic dark-acme-lumen).** A ticket holds a
> `attestations` map (rendered by `show`, HMAC hex stripped): independent attestations of
> different **kinds** (`plan-review` at claim, `completion-verifier` at close, future kinds)
> coexist instead of clobbering one slot. Records are **immutable**; "valid for a gate right
> now" is computed on read (`plan_review.attest.compute_validity`) — a reopen / code-drift /
> material-edit makes a still-`certified` record read as not-valid. The top-level `signature`
> is a back-compat mirror of the most-recent attestation. See `docs/plan-review-gate.md` +
> ADR 0009.

> **Authoring workflows + prompts.** The visual editor is
> [docs/workflow-editor.md](docs/workflow-editor.md); the contract-bearing
> prompt/step model behind it (prompt front-matter + closed key set + overrides, the
> prompt↔op↔registry contract conventions, the derived prompt index + CI drift gate,
> `execution_mode`, and the 3-state + runtime validation) is
> [docs/workflow-authoring-v2.md](docs/workflow-authoring-v2.md), with ADRs under
> [docs/adr/](docs/adr/).

There is no `init` over MCP (operator bootstrap only). `reconcile` `live` mode
additionally requires `REBAR_MCP_ALLOW_JIRA_SYNC=1`. Both env gates accept
any case-insensitive truthy value (`1`/`true`/`yes`, whitespace tolerated);
anything else (including unset) is off.

## Session logs (`session_log` ticket type)

`session_log` is a first-class ticket type for **verbose, durable, agent-facing
logs** stored in the rebar store and surfaced later by keyword. It is
deliberately kept out of the dependency-graph / store-health hot paths so its
large bodies never tax the operations that run constantly during the
parallel-agent workflow. Its behavior differs from work tickets:

- **Gate- and lifecycle-exempt.** `clarity_check` / `check_ac` / `quality_check`
  treat it as exempt (always pass), and `validate` never flags it
  (orphan/empty/etc.). It **cannot** be `claim`ed or `transition`ed; `show`,
  `comment`, and `edit` work normally.
- **Visibility — searchable, hidden from `list`.** Included in keyword `search`
  and in single-ticket `show`, and listed by `recent_session_logs` /
  `list_tickets(ticket_type="session_log")`, but **excluded** from default
  `list` and from `ready` / `next_batch` / `deps` / `validate` (the graph/health
  compiles), so log size/count never affects those.
- **Non-blocking links only.** `relates_to` / `discovered_from` are allowed (so a
  log can reference the work it documents); `blocks` / `depends_on` are refused on
  either endpoint, and logs never enter the dependency graph.
- **Never synced to Jira.** `reconcile` excludes `session_log` (it is in the
  reconciler's `EXCLUDED_SYNC_TYPES` and absent from the local→Jira type map), and
  it never appears in `bridge_fsck`.
- **Title convention (guidance, NOT enforced).** Titles should carry a short
  summary of the work (not merely a date/time/session id); nothing validates this.

**Capture helper.** Rather than hand-assembling `create` + `comment`, use the
helper: library `rebar.append_session_log(entry, *, summary=…, relates_to=…,
discovered_from=…)`, CLI `rebar session-log append "<entry>"` (and
`rebar session-log start --summary=…` to rotate to a fresh log), or the
write-gated MCP `log_session(entry)`. The first call creates one `session_log`
(titled by `summary`) and records it as the current log via a **local,
git-ignored** pointer (`.rebar/current_session_log`); subsequent calls append to
that same log. Retrieve recent logs with `rebar.recent_session_logs(limit=5)` /
`rebar session-logs [--limit=<n>]` / the MCP `recent_session_logs` tool (newest
first, default 5).

**Auto-rotation per session (no manual `start` needed).** The pointer stores a
session fingerprint alongside the log id, taken from the shared session-id resolver
(`REBAR_SESSION_ID`, then `CLAUDE_CODE_SESSION_ID`, then `SESSION_ID`; see
`docs/config.md`). When a NEW session's first `append` sees a
pointer whose fingerprint *differs* from this session's, it auto-rotates to a fresh
log — so distinct agent sessions get distinct logs without anyone running
`session-log start`. A *differing* pointer fingerprint includes a **missing** one: if
this session has a fingerprint but the pointer has none (`session=None` — a legacy
bare-id, or a pointer written by a prior run that had no session id), it still rotates,
so a fingerprinted session never pollutes a fingerprint-less stranger's log (defensive
rotation, bug slum-shoal-gully). It degrades safely only when **this** session cannot
identify itself: if no session id is set (fingerprint `None`), it NEVER rotates — a
single continuous no-id session keeps appending to one log. The fingerprint
deliberately does NOT fall back to git HEAD (a commit within one session must not
rotate the log).

**LLM agent operations (optional, gated):** `review_ticket(ticket_id, reviewer_id,
graph)` runs a tool-using LLM agent that reviews a ticket (or its graph) and
returns a `review_result` (`{findings[], …}`). `verify_completion(ticket_id,
graph)` runs the **completion-verifier** agent that checks a ticket's completion
requirements (acceptance/success/close criteria, definitions of done; for bugs,
that the bug is resolved) are demonstrably met by the implementation and returns a
`completion_verdict` (`{verdict: PASS|FAIL, findings[], …}`; on FAIL each finding
cites the failing criterion + a source-code citation). `review_code(...)` reviews a
diff/commit range and `scan_spec(spec_text, batch_size)` scans prose for
spec-implied work — both emit structured findings like `review_ticket`. All are
**disabled unless `REBAR_MCP_ALLOW_LLM=1`** (they make a live, billable LLM call)
and need the `nava-rebar[agents]` extra + `ANTHROPIC_API_KEY`. Part of the optional
`rebar.llm` framework (CLI: `rebar review` / `rebar verify-completion`; library:
`rebar.llm.review_ticket` / `rebar.llm.verify_completion`) — see
[docs/llm-framework.md](docs/llm-framework.md).

> **Completion-verification close gate (optional).** When
> `verify.require_completion_verification_for_close=true` (default off; **on for
> this project**), closing a work ticket runs `verify_completion` first: a **FAIL**
> verdict (or an unavailable LLM) **blocks** the close (fail-closed), and on a **PASS**
> that is also `certifiable` the verdict is HMAC-**signed** onto the ticket (the
> trustworthy attestation, only meaningful under the MCP server's environment key; CI
> verifies it). A PASS with **`certifiable: false`** — a parent whose direct child is
> closed but uncertified (e.g. force-closed) — still **closes**, judged on its own
> criteria, but is **left unsigned**. `--force-close` closes without verifying or
> signing. So a **closed-without-signature** ticket means "not certified" — the gate was
> bypassed *or* a descendant is still uncertified; it no longer implies the ticket's own
> validation failed (re-close the uncertified child to earn its signature). It is an
> *alternative* to the signature gate (`require_signature_for_close`), not composed with it.

## Quality gates

The **per-ticket** gates each take a ticket id and self-check a single ticket
before you dispatch/close it: `clarity_check` (score/verdict), `check_ac` (has an
Acceptance Criteria block), `quality_check` (dispatch readiness). Separately,
`validate` is a **repo-wide** tracker-health check — it takes **NO ticket id**
(passing one errors); it scans the whole store and returns an overall health
score (1-5) bucketed into critical/major/minor/warning findings (e.g. orphaned
tasks, cycles, cross-epic child deps). Use the per-ticket gates on the ticket
you're working; use `validate` for store-level health.

The per-ticket gates are **structural floor checks**, not semantic scoring:
`clarity_check` is a heading/length/bullet heuristic, so it confirms a ticket is
*shaped* like dispatchable work — it can't judge whether the content is actually
good. Treat a pass as "well-formed enough to dispatch," not "high quality."

### Ticket template the gates enforce

Author descriptions to this per-type matrix so the gates pass first time. An
**`## Acceptance Criteria`** block with `- [ ]` checklist items is the universal
floor — `check_ac` requires it on **every** type, and `clarity_check` will not
pass without it either (the two gates share one vocabulary). The per-type
headings below are what `clarity_check` additionally rewards:

| Ticket type | Required (all)            | Type-specific headings clarity rewards            |
|-------------|---------------------------|---------------------------------------------------|
| `task`      | `## Acceptance Criteria`  | file paths (e.g. `src/…/x.py`)                    |
| `story`     | `## Acceptance Criteria`  | `## Why`, `## What`, `## Scope`                   |
| `bug`       | `## Acceptance Criteria`  | `## Reproduction Steps`, Expected vs Actual       |
| `epic`      | `## Acceptance Criteria`  | `## Success Criteria`, `## Context`               |
| `session_log` | *(none — gate-exempt)*  | n/a (always passes the gates; see Session logs)   |

Plus, for all types **except `session_log`**: a description ≥ ~200 chars and at
least one bullet/checklist line. A ticket missing the `## Acceptance Criteria`
checklist fails both gates regardless of how rich the rest of the description is.
(`session_log` is exempt from all of this — see the Session logs section above.)

Record `set_file_impact` (the `{path,reason}` array that `next_batch` uses to
avoid scheduling file-conflicting tickets together) and `set_verify_commands`
(DD-level verification) so downstream scheduling and verification work.

### Project-supplied criteria (both review gates)

A project can add its OWN review criteria — naming rules, layering, banned APIs,
architectural invariants — to **both** the plan-review and code-review gates through one
`.rebar/criteria_routing.json` overlay over the shared `rebar.llm.criteria` registry (no
plugin code runs in the gate; an absent overlay fails open to the packaged behaviour).
The overlay is gate-keyed with a shared `activate` list:

```json
{
  "plan_review": { "project.<name>": { "exec": "1-TURN", "block_threshold": 0.9, … } },
  "code_review": { "project.<name>": { "exec": "1-TURN", "blocking_enabled": false, … } },
  "activate":    ["project.<name>"]
}
```

The author → activate → eval → block loop:

1. **Author** — add a `project.<name>`-prefixed routing entry under the gate(s) you want
   (an un-prefixed built-in id instead *re-tunes* or `"disabled": true`-disables that
   built-in). For an LLM criterion, write its rubric prompt at
   `.rebar/prompts/<gate>-project.<name>.md` (e.g. `plan-review-project.<name>.md`); an
   `exec: "DET"` criterion is prompt-less (a grounding detector — see ADR 0016).
2. **Activate** — list the id in `activate` (presence in the file ≠ active; built-ins are
   always active). An id activated for a gate it has no routing entry in is simply inactive
   *there*, not an error, so one `activate` list serves both gates.
3. **Eval / block** — the criterion runs like a built-in. Blocking posture differs by gate
   (the deliberate divergence): plan-review blocks on `default_posture: "blocking"`;
   code-review blocks on an explicit `blocking_enabled: true`. Everything ships
   advisory-by-default (coach-not-block) until you opt into blocking.

Overlay edits are picked up automatically (the merged views are content-signature-keyed +
repo-isolated). See **ADRs 0015 (plan-review overlay), 0016 (DET invariants), 0017 (the
unified cross-gate registry)** and `docs/plan-review-gate.md`.

## Module-size policy (when editing rebar itself)

rebar is built to be edited by agents that load a unit whole. **Target 200–500
LOC per file; soft cap 800.** When a unit grows past the cap, split it **only
along call-graph seams that already exist** (extract a cluster of functions that
already call each other) — never mechanically to hit a number, and **never create
files < 100 LOC** by splitting. Prefer **deleting** oversized bash via the
bash→Python strangler-fig migration over carving it into more bash. The current
over-cap offenders and their planned remedies are tabulated in
`docs/architecture.md`, and a CI **module-size gate** (`.github/workflows/test.yml`)
**fails the build** when a *new* file exceeds the soft cap and is not in
`.github/module-size-allowlist.txt` (the grandfathered set), so the over-cap set
cannot silently grow.

## Navigating the codebase (when editing rebar itself)

This checkout has the **Serena** MCP server configured (LSP-backed, Pyright over
`src/rebar`) for *semantic* code navigation. **Prefer its symbol tools over
`grep`** when finding or following references — `find_symbol`,
`find_referencing_symbols`, `get_symbols_overview`, and symbol-precise edits
(`replace_symbol_body`, `insert_after_symbol`). It resolves "who calls / imports
this?" reliably (definitions + references, not text matches), which is exactly
what cross-cutting refactors (e.g. the bash→Python migration's importer sweeps)
need. Serena's tools load at **session start**; if they're absent, the server is
registered in local MCP config — verify with `claude mcp get serena` (re-add with
`claude mcp add serena -- uvx --from git+https://github.com/oraios/serena serena
start-mcp-server --context ide-assistant --project "$(git rev-parse --show-toplevel)"`).
Its per-developer cache/config lives in the git-ignored `.serena/`. `grep`/the
search tools remain the fallback when Serena is unavailable or for non-symbol
(text/comment/string) searches.

## Ticket hierarchy (parent/child)

Containment (epic→story→task/bug) is the **`parent_id`** hierarchy, **not** a `link` relation:
parent work to the epic/story it belongs to with `create --parent <id>` or `edit --parent <id>`
(see `rebar create --help`). Don't attach an epic's workstreams with a `depends_on`/
`discovered_from` link — **parent** them, or they aren't its children (the hierarchy is what
`ready`/`next-batch`/`validate`/the completion gate's child-closure check operate on).

### Parent-first claim/transition cascade

Starting work on a child **pulls its open parent into progress first.** When you
`claim <id>` (or run the equivalent `transition <id> open in_progress`), rebar
checks the ticket's `parent_id`:

- **Parent is `open`** → the same operation runs on the **parent first**, then the
  child. This walks **up the whole chain** (grandparent before parent before child),
  so claiming a leaf task moves its open story and its open epic to `in_progress`
  too. A `claim` cascade carries the **same `--assignee`** up the chain.
- **Parent is `in_progress` / `closed` / `blocked`** (or there is no parent) → **no
  cascade**; only the requested ticket moves. (The cascade triggers on an `open`
  parent only — the goal is just to ensure ancestors aren't left merely `open` while
  you work a descendant.)
- **Parent operation fails** → the **child is left untouched** (not claimed / not
  transitioned), and the error message names the **parent** as the cause, e.g.
  `cannot claim <child>: claiming its parent <parent> failed first …`. The exit code
  is propagated from the parent failure (a parent concurrency conflict still surfaces
  as **exit 10 / `ConcurrencyError`** so you re-read and retry). The cascade is
  cycle-safe (a malformed parent cycle can't recurse forever).

Only the `open → in_progress` direction cascades. `close`, `reopen`, and a move to
`blocked` are **never** cascaded to the parent (closing a parent has its own,
separate open-children guard — see the completion gate).

**Gate interaction (read this if the plan-review claim gate is enabled).** The
cascaded parent claim is a *full* claim — so it runs the parent's **own**
plan-review claim gate (`verify.require_plan_review_for_claim`). Epics/stories are
**not** gate-exempt (only `bug`/`session_log` are), so when the gate is on,
claiming a leaf task can be **blocked by the parent's missing/stale attestation**,
and the error will name the parent. Earn the parent's attestation
(`rebar review-plan <parent>`) — or claim the parent yourself first — before
claiming the child, or pass `--force` (which propagates up the cascade and bypasses
the gate at each level with an audit note). The same "operation = full operation"
rule means the cascade also sets your `--assignee` on the ancestors it claims.

## Linking (relations + hierarchy promotion)

- `link <id1> <id2> <relation>` **requires** a relation. The six relations:
  `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
  `discovered_from`.
- `unlink <source> <target>` takes **no** relation argument — it is pair-scoped
  and removes the **most-recently-created** link between that ordered pair (one
  per call). If a pair has multiple links, call `unlink` repeatedly.
- **Hierarchy promotion (blocking links only).** For `blocks`/`depends_on`, rebar
  promotes the link endpoints up the parent hierarchy so the dependency lands
  between tickets at a comparable level (epic↔epic, story↔story,
  task/bug↔task/bug), emitting a `REDIRECT: A→B promoted to …` note when it does.
  This is why a blocking link you point at a child ticket can land on its epic.
  Non-blocking relations (`relates_to`/`duplicates`/`supersedes`/
  `discovered_from`) are linked exactly as given, with **no** promotion.
  Consequence: because a blocking link may be promoted to an ancestor, `unlink`
  must target the **promoted (ancestor)** endpoint to remove it.

## Tags (convergent add/remove deltas)

Tags mutate via **add/remove deltas** (`TAG_DELTA` events), so two clones adding
different tags both survive (no whole-field clobber). The surface:

- `tag <id> <t>` / `untag <id> <t>` — single-tag add/remove (idempotent).
- `edit <id> --add-tag=a,b --remove-tag=c` — batch add/remove in one event.
- `edit <id> --set-tags=x,y` — replace the tag set. **It is compiled to a delta
  against the tags this clone has observed (add-wins): a concurrent tag another
  clone added that you haven't synced is NOT removed — so "set" is convergent, not
  an authoritative reset.** `--set-tags=""` clears the *observed* tags only.
  `--set-tags` cannot be combined with `--add-tag`/`--remove-tag` (error).
- `--tags` is **not** an `edit` flag (it would clobber); it remains only on
  `create` (genesis). Library/MCP `edit_ticket(tags=…)` is a deprecated alias for
  `set_tags`; prefer `add_tags`/`remove_tags`/`set_tags`.
- Tag names are trimmed; empty/whitespace-only/control-char names are rejected.

Rollout note: a new event type is preserved-and-ignored by older clones, so a
`TAG_DELTA` is invisible there until they upgrade — `fsck` WARNs when the store
holds event types newer than the running binary. **Upgrade reconcile hosts first.**

## The store shares every write immediately

Every write (`create`/`edit`/`transition`/`claim`/`link`/…) auto-commits its
event to the `tickets` branch **and** auto-pushes it to the configured **sync
remote** (`<sync.remote>/tickets`) when that remote exists — so your local ticket
activity (including test tickets) propagates to the shared remote immediately.
Push is best-effort: no remote means no push, and a push failure never fails the
write (the commit stays local and diverged). `fsck` reports `PUSH_PENDING` when
the local branch is ahead of that remote. The sync remote is **`sync.remote`**
(env `REBAR_SYNC_REMOTE`, default `origin`) — set it when the tickets branch's
source of truth is a remote **other** than `origin` (this repo keeps it on
`origin` = GitHub while code review lives on a separate `gerrit` remote; see the
remotes note under "Git workflow"). The **`REBAR_SYNC_PUSH`** env var tunes push
timing (default `always`): `async` pushes in the
background so per-write network latency doesn't serialize a batch claim, and `off`
keeps commits local — both still surface `PUSH_PENDING` via `fsck` (see
`docs/concurrency.md`).

## Git workflow (code changes) — land changes THROUGH GERRIT, not GitHub PRs

**This repo dogfoods its own premise: every change to `main` must pass two independent
Gerrit gates before it can land — the `LLM-Review` vote (the rebar review-bot's LLM code
review) AND the `Verified` vote (CI: build/test/lint/typecheck on GitHub Actions). `main`
flows through Gerrit; GitHub is a read-only mirror.** Do **not** open GitHub PRs or push to GitHub `main` — a repository
ruleset rejects direct pushes and PR merges there (only Gerrit's replication deploy key
can advance the mirror). The full contributor guide is **[CONTRIBUTING.md](CONTRIBUTING.md)**;
the short version for agents:

> **Work in a fresh worktree — not the main checkout.** `main` moves fast, so before you
> edit, branch from current `origin/main` in a dedicated worktree
> (`git fetch origin && git worktree add ../<name> -b <branch> origin/main`) and set up its
> local venv (see [`docs/local-dev-env.md`](docs/local-dev-env.md)). Editing in the main
> checkout risks building on stale code and a painful rebase at submit time.

> **Remotes in this checkout (split residency).** This working checkout has **two** remotes
> with distinct roles — don't conflate them:
> - **`origin` → GitHub** (`navapbc/rebar`): the read-only **code mirror** AND the source of
>   truth for the **`tickets`** branch (rebar's ticket events auto-push here; GitHub Actions
>   sync that branch to Jira). rebar's ticket sync targets this remote — it is the configured
>   **`sync.remote`** (default `origin`).
> - **`gerrit` → `rebar.solutions.navateam.com`**: the **code-review** remote — push code for
>   review here (step 2), **not** to `origin`.
>
> So **code review goes to `gerrit`; ticket events go to `origin`.** A plain
> `git push origin HEAD:refs/for/main` here would push the review ref to GitHub (rejected).
> (An external contributor who instead *clones from Gerrit* has `origin` = Gerrit and uses
> CONTRIBUTING.md's `origin` commands verbatim; this note is THIS checkout's dual-remote layout.)

> **Every commit needs a rebar ticket.** CI's `Verified` gate rejects a commit to `main` whose
> message does not reference a rebar ticket that RESOLVES in the store — add a
> `rebar-ticket: <id>` trailer (preferred) or a leading `<id>:` subject; `<id>` = alias / full /
> short / Jira key. Enforced by `verify.require_ticket_for_commit` (on for this repo); see
> [`docs/commit-ticket-trailer.md`](docs/commit-ticket-trailer.md).

> **Every commit needs a DCO sign-off (agent trailer).** Gerrit rejects an unsigned push to
> `refs/for/*` (`receive.requireSignedOffBy`). Agent commits are authored by the bot identity
> (`RebarBotNava` / committer `joeoakhart+bot@navapbc.com`), but the DCO certifier is the
> **responsible human**, so the sign-off trailer must be **exactly**:
> `Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>` — the human's real name with his
> registered plus-addressed mailbox (which routes to the human; kernel practice accepts
> plus-addresses). Add it with `git commit -s` when committing as the bot (the configured
> committer name is `RebarBotNava`, so pass `-c user.name="Joe Oakhart"` if you need the
> trailer name to read as the human), or append the trailer verbatim. See the "Sign your work
> (DCO)" section of [CONTRIBUTING.md](CONTRIBUTING.md).

1. **Get Gerrit access once.** Sign in at `https://rebar.solutions.navateam.com` via
   GitHub OAuth, generate an HTTP password (Settings → HTTP Credentials), clone from
   Gerrit (`https://<user>@rebar.solutions.navateam.com/a/rebar`), and install the
   `commit-msg` hook
   (`curl -Lo .git/hooks/commit-msg https://rebar.solutions.navateam.com/tools/hooks/commit-msg && chmod +x .git/hooks/commit-msg`)
   so commits carry a `Change-Id`.
2. **Push for review:** `git push gerrit HEAD:refs/for/main` (the magic ref — creates a
   Gerrit change, does not touch `main`). In this checkout the Gerrit remote is named
   `gerrit` and `origin` is GitHub (see the remotes note above); an external clone-from-Gerrit
   uses `origin` here instead.
3. **The gate — two votes:** two bots vote independently. The rebar review-bot casts
   `LLM-Review` (LLM code review) and CI casts `Verified` (build/test/lint/typecheck on
   GitHub Actions). A change is submittable only at **`LLM-Review = +1` (MAX) AND
   `Verified = +1` (MAX) AND no unresolved comments** — only the bots/admins cast either
   label, so you cannot self-approve or self-verify. On `LLM-Review`, a `-1` tagged
   `[LLM-Review: BLOCK — coverage-gap (…)]` is an infra veto (not your code); a `-1` tagged
   `[LLM-Review: BLOCK — finding]` names real findings in your diff. On `Verified`, a `-1`
   is a CI failure — open the linked run; if it's a flake, comment `recheck` to re-run CI
   on the same patchset.
4. **Iterate:** fix findings, `git commit --amend --no-edit` (keep the `Change-Id`),
   re-push (each new patchset re-runs both votes). **Submit** once both are green → Gerrit
   merges and **replicates the new `main` to GitHub** (where branch CI runs on the push).

> **Multi-story features → a feature branch (not one giant change).** Steps 1–4 above are
> the path for **one** change. When you're driving a **multi-story feature** — especially
> several agents in parallel — don't stack it into one change or a fragile chain: use a
> **server-side feature branch** (epic 88ab, ADR-0025). Each story is reviewed *into*
> `refs/heads/feature/<name>` (push to `git push gerrit HEAD:refs/for/refs/heads/feature/<name>`)
> and passes both gates there; then a **single `--no-ff` merge change** lands the whole
> branch into `main` (`git merge --no-ff gerrit/feature/<name>` → `git push gerrit
> HEAD:refs/for/main`), gated identically and submitted atomically. The full recipe —
> catch-up merges, conflict/abandon handling, the driver-group prerequisite — is in
> **[CONTRIBUTING.md](CONTRIBUTING.md) §4**.
>
> - **When to use it:** genuinely multi-story / multi-agent work. **A single small change
>   → just push one change to `refs/for/main`** (steps 1–4) — a feature branch is overhead
>   you don't need for a one-shot fix.
> - **Who can create it:** branch creation and the merge-commit push are restricted to the
>   `feature-branch-drivers` Gerrit group; ordinary story pushes into the branch are not.
>   If you're not a driver, a driver creates the branch and lands the merge-back.
> - **Re-merge cost (ADR-0025):** when `main` advances under an open merge change,
>   re-merging carries `LLM-Review` but **re-runs `Verified`** (new merge tree). Changing
>   the feature tip is REWORK and wipes **both** votes.
> - **Fresh-worktree hook note:** a merge commit needs a `Change-Id`, and a fresh worktree
>   does **not** carry the `commit-msg` hook — install it before the merge-back
>   (`curl -sLo .git/hooks/commit-msg https://rebar.solutions.navateam.com/tools/hooks/commit-msg && chmod +x .git/hooks/commit-msg`),
>   and re-stamp an already-made merge commit with `GIT_EDITOR=/bin/true git commit --amend`.

(This governs *code*. rebar's own **ticket events on the `tickets` branch** still
auto-commit/auto-push as described above — that is unchanged and does NOT go through
Gerrit.)

## Library quick reference

```python
import rebar
tid = rebar.create_ticket("task", "title", return_alias=True)   # -> {"id","alias"}
rebar.claim(tid["id"], assignee="me")                            # raises ConcurrencyError if taken
hits = rebar.search("login")                                     # replay-derived list
rebar.link(child, parent, "discovered_from")
rebar.transition(tid["id"], "in_progress", "closed")

import rebar.llm                                                  # optional [agents] extra
review = rebar.llm.review_ticket(tid["id"], "ticket-quality")    # -> review_result {findings[], …}
```
