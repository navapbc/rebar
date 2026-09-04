# Exit codes — rebar's CLI process-status contract

rebar's exit codes are **load-bearing for agents**: the parallel-agent workflow
keys off them (a claim that loses a race is exit 10, not a crash; a missing
ticket is exit 1, not 0-with-empty). This document is the single source of truth
for what each code means and which code each subcommand emits.

`tests/interfaces/lifecycle/test_exit_codes.py` pins it, and it is worth being exact
about how much: that test exercises the load-bearing exit paths (0/1/2/10/11) against
the live CLI, and — mirror F12 — cross-checks this file's per-command table against the
route registry, so a route with no row here, or a row naming a command that no longer
exists, fails the build. It does NOT verify the per-command exit values in the table
below; those columns are hand-authored. Until 2026-09 this file claimed a stronger
guarantee than any test made, and the table had drifted in both directions while
carrying that claim.

This contract is **frozen** as of the 2026-06-09 breaking-change window. Changes
to an emitted code are contract changes and must be called out in release notes.

## The codes

| Code | Name | Meaning |
|-----:|------|---------|
| `0`  | success | The command did what it was asked. (Read commands that find nothing still succeed — an empty list is exit 0.) |
| `1`  | runtime error | Ticket not found, invalid input value, a missing **required positional** argument, a failed precondition, or a per-ticket gate's **fail verdict**. The general-purpose error code. |
| `2`  | usage error | An unrecognized CLI `--option` on a **structured read command** (`show`, `list`, `deps`, `ready`, `search`), which reject unknown options rather than silently ignoring them. Also the not-found/usage path of `clarity-check` (see the gate note below). The plan-review gate's **INDETERMINATE** verdict also exits `2` — either from a non-retryable LLM degrade **or** from the deliberate **not-claimable fast-fail** (`review-plan` refusing a ticket that can't be claimed yet — a `ticket-not-claimable` finding with `coverage.llm_ran=false`; **no LLM ran**). `review-plan --retry` also exits `2` when it **refuses an ineligible resume** (the latest result is a PASS/BLOCK, a non-retryable indeterminate, or a missing/legacy/corrupt/stale/digest-mismatched journal) — before any model call, no sidecar, the full-review remedy on stderr — and when `--retry` is combined with `--force`/`--status`/`--check` (a flag conflict). Predates and is unchanged by exit 11. |
| `10` | concurrency mismatch | Optimistic-concurrency rejection: a state-dependent op (`transition`/`claim`/`reopen`) re-read the ticket under lock and the actual status no longer matched the expected one. **Normal under parallelism** — re-read and pick another, never force. Emitted by `_commands/txn.py` (`ConcurrencyMismatch`). |
| `11` | retryable gate fault | An LLM gate could not produce a usable result and the caller may retry without changing the reviewed work. `review-plan`, `review-code`, `verify-completion`, and the `close` completion gate emit `11` for provider degradation classified as retryable. `verify-completion` and the `close` gate also emit `11` when bounded completion recovery returns `verdict_obtainable=false`. An ordinary completion FAIL remains `1`. A nonretryable raised completion error also remains `1`. A nonretryable plan-review or code-review INDETERMINATE remains `2`. See "Recorded decisions". |
| `3`  | stale-rebase sentinel / poor health | `fsck-recover --detect-only` found a paused rebase or merge; `validate` scored repo health 2/5; or a **legacy reconciler route** found another pass in flight. Canonical bridge routes translate that last benign state to 0. |
| `4`  | critical health / legacy phase gate | `validate` scored repo health 1/5, or a **legacy reconciler route** was stopped by its historical phase gate. Canonical bridge routes translate the phase-gate state to 0. |
| `12` | review attestation stale or absent | `review-plan --status` found the ticket's plan-review attestation is not current — the plan changed since the last review, the attestation was never written, or the verified-at SHA is no longer reachable. Exit 0 means current; 12 means a fresh `rebar review-plan` is needed before `claim` will pass the gate (`src/rebar/_cli/_llm_commands.py` — `return 0 if status["ok"] else 12`). |
| `75` | rebase / merge guard / legacy reschedule | A ticket-store write was refused during a paused rebase or merge (`RebaseGuard`), or a **legacy reconciler route** requested another pass. Canonical bridge routes translate the benign reconciler reschedule to 0; the store guard remains 75. |
| `78` | store schema incompatible | The tracker store was written by a newer rebar that this version cannot safely read or write (`StoreIncompatibleError`). Upgrade this rebar installation. Emitted by `src/rebar/_store/compat.py` (`returncode = 78`), surfaced the same way as 75. |

> **Forward compatibility:** a client or agent encountering an **unknown or unlisted non-zero exit code MUST treat it as a failure**; new codes may be added in future breaking-change windows.

### Bridge routes: canonical 0/1/2 and retained compatibility sentinels

`rebar bridge preview`, `rebar bridge sync`, and the direct-engine `preview` / `sync`
routes expose only this canonical contract: **0** success or a benign scheduler state,
**1** operational failure, and **2** invalid invocation or configuration. The reconciler
classifies one execution once; the route adapter changes only its process status and message,
never the selected policy, repository effects, or refs.

Canonical benign states return 0 with one stable stderr line:

| State | Canonical line |
|-------|----------------|
| converged | `BRIDGE_STATE: converged` |
| paused | `BRIDGE_PAUSED: {…}` (the existing single-line JSON marker) |
| another pass in flight | `BRIDGE_STATE: in-flight` |
| reschedule requested | `BRIDGE_STATE: reschedule` |
| historical phase gate | `BRIDGE_STATE: legacy-gated` |

The remaining rolling-migration compatibility routes — `rebar reconcile --mode ...`,
direct-engine `--mode`, and argument-less direct-engine invocation — retain their historical
results and defaults. Converged and paused remain 0; another pass in flight remains **3**;
the phase gate remains **4**; and reschedule remains **75**. Their established messages remain
unchanged (`reconcile: ... another pass in flight`, `reconcile: ... gate blocks advancement ...`,
and `RESCHEDULE: ...`). The former Python and MCP `reconcile(mode=...)` adapters are removed;
new automation should use the canonical bridge routes and ordinary 0/1/2 handling.

### Cross-cutting rules

- **Unknown option → `2` (structured reads + a few additional commands).** `rebar list --bogus`,
  `rebar show <id> --bogus`, `rebar ready --bogus`, `rebar search q --bogus`,
  `rebar deps <id> --bogus` all exit `2`. (`show`/`list` historically returned
  `1` here; aligned to `2` in the 2026-06-09 window — see "Recorded decisions"
  below.) The five structured reads are the **contracted, pinned** set for option-validation
  — `metrics`, `doctor`, `fsck`, and `composer`/`create` also exit `2` on an unknown option
  today but are not yet pinned by the test suite. See "Unknown-option handling" for the full
  scope note.
- **Missing required positional → `1`.** `rebar show` (no id), `rebar create`
  (no type/title), `rebar link a b` (no relation), `rebar deps` (no id) → `1`.
  A missing *positional* is a runtime error (1); a malformed *option* on a
  structured read is a usage error (2).
- **State mismatch on a status-dependent op → `10`.** Includes `transition`
  with a stale `current` status, `claim` of a non-open ticket, and `reopen` of a
  non-closed ticket.
- **Option-value syntax → accept BOTH `--opt value` and `--opt=value`.** Every
  value-taking option on the structured read commands (`list --status`,
  `session-logs --limit`, `search --status/--type/--has-tag/--sort`, `ready
  --epic/--sort`, plus `--output`) accepts
  the space form *and* the equals form interchangeably — matching the
  write/composer commands (`claim --assignee <you>`) and the `--opt <value>`
  convention used throughout `AGENTS.md`. So `rebar session-logs --limit 30` and
  `rebar session-logs --limit=30` are equivalent; a space-form flag is **never**
  mistaken for an unknown option (the historical footgun where a parse error read
  as "no results"). The one exception is a value that itself begins with `-`
  (e.g. the descending `--sort=-priority`): pass those via the equals form, since
  the space form would ambiguously consume the following token.

### Unknown-option handling (scope + known gap)

The five **structured read commands** are the contracted, pinned set that
validate their options and exit `2` on an unrecognized `--option`: `show`,
`list`, `deps`, `ready`, `search` (all route through
`_engine_support/reads_cli.py`; option parsing lives in
`_engine_support/output.py::parse_output`). These are pinned by
`test_exit_codes.py::test_unknown_option_exits_2`.

Several additional commands also exit `2` on an unknown option today —
notably `metrics`, `doctor`, `fsck`, and the composer family (`create`,
`edit`) — but they are **not yet pinned** by the test suite and therefore
represent a larger validated (but uncontracted) set.

Other subcommands do **not** uniformly validate options: most mutation commands
either silently ignore an unknown `--option` (e.g. `comment`, `tag`, `claim`,
`check-ac` → exit `0`) or fail incidentally (e.g. `archive`, `unlink` →
exit `1`). Standardizing option validation across the mutation commands is a
**known gap deliberately left out of this freeze** (sub-effort (a) scoped the
contract + the structured-read alignment; a broader option-parsing sweep is
follow-up work). The `bad-opt` column is therefore omitted from the table below;
assume only the five reads guarantee `2`.

## Per-command table

Codes below are the **observed, tested** codes for each public dispatcher arm.
"miss" = invoked against a non-existent ticket id; "concurrency" = the code on an
optimistic-concurrency state mismatch; "—" = not applicable. For unknown-option
behavior see "Unknown-option handling" above (only the five structured reads
guarantee `2`).

| Subcommand | success | miss | concurrency | notes |
|------------|:------:|:----:|:-----------:|-------|
| `archive` | 0 | 1 | — | idempotent on an already-archived ticket (still 0) |
| `bridge fsck` | 0 | — | — | audit; no ticket id |
| `bridge-fsck` | 0 | — | — | compatibility alias for `bridge fsck`; preserves the same audit exit behavior |
| `bridge-status` | 0 | — | — | compatibility alias for `bridge status`; no ticket id; reads the durable bridge status snapshot, and `--max-age` makes a stale snapshot a failure |
| `bridge` | 0 | — | — | no ticket id; canonical `preview`/`sync` use 0/1/2 as documented above; `pause`/`resume` control scheduled reconciliation |
| `check-ac` | 0 | 1 | — | **gate**: 0=has-AC, 1=missing-AC **or** not-found |
| `claim` | 0 | 1 | 10 | 10 when the ticket is not open (already claimed) |
| `clarity-check` | 0 | **2** | — | **gate**: 0=pass, 1=fail-verdict, 2=not-found/usage |
| `comment` | 0 | 1 | — | |
| `compact` | 0 | 1 | — | |
| `compact-all` | 0 | — | — | no ticket id |
| `create` | 0 | — | — | missing `<type>`/`<title>` → 1 |
| `delete` | 0 | 1 | — | requires `--user-approved`; otherwise 1 |
| `deps` | 0 | 1 | — | structured read (unknown option → 2) |
| `edit` | 0 | 1 | — | |
| `exists` | 0 | 1 | — | **by design**: 0=exists, 1=not-found (presence probe) |
| `format` | 0 | 0 | — | tolerant read: unknown id renders empty, still 0 |
| `fsck` | 0 | — | — | no ticket id |
| `fsck-recover` | 0 | — | — | no ticket id; `--detect-only` exits **3** if stale rebase found; bad args → 2 |
| `tracker-maintenance` | 0 | — | — | no ticket id; **1** = refused (unpushed ticket commits, or `origin/tickets` missing so they cannot be ruled out) without `--force=<reason>`; bad args / no tracker → 2 |
| `tracker-footprint` | 0 | — | — | no ticket id; opt-in descriptive read; **1** = configured tracker/source cannot be measured; bad args → 2 |
| `get-file-impact` | 0 | 1 | — | |
| `get-verify-commands` | 0 | 1 | — | |
| `init` | 0 | — | — | idempotent |
| `link` | 0 | 1 | — | missing relation arg → 1 |
| `list` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `list-descendants` | 0 | 0 | — | tolerant read: unknown root → empty buckets, 0 |
| `next-batch` | 0 | 1 | — | |
| `quality-check` | 0 | 1 | — | **gate**: 0=dispatch-ready, 1=not-ready **or** not-found |
| `ready` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `reclaim-collapse` | 0 | — | — | offline shadow-clone-only history rewrite engine; dry-run/apply success → 0, safety refusal / invalid boundary / ineligible horizon → 2 |
| `reopen` | 0 | 1 | 10 | 10 when the ticket is not closed |
| `resolve` | 0 | 1 | — | |
| `revert` | 0 | 1 | — | missing `<ticket_id> <uuid>` → 1 |
| `scratch` | 0 | 1 | — | |
| `search` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `set-file-impact` | 0 | 1 | — | malformed JSON arg → 1 |
| `set-verify-commands` | 0 | 1 | — | malformed JSON arg → 1 |
| `attach-commits` | 0 | 1 | — | missing args → 2; an unresolvable SHA → 1 (nothing recorded) |
| `show` | 0 | 1 | — | structured read (unknown option → 2); not-found also emits a parseable JSON error on stdout |
| `summary` | 0 | 0 | — | tolerant read: unknown id renders `[unknown]`, still 0 |
| `tag` | 0 | 1 | — | |
| `transition` | 0 | 1 | 10 | 10 on stale `current` status. A close guarded by completion verification also returns 11 for a retryable raised verifier error or `verdict_obtainable=false`. |
| `unlink` | 0 | 1 | — | |
| `untag` | 0 | 1 | — | removing an absent tag is still 0 |
| `validate` | **0-4** | — | — | **exception**: exit is a health-severity bucket, not the standard contract; takes **no** ticket id (passing one → 1) |
| `audit` | 0 | — | — | no ticket id; `audit show <ticket>` reads the audit trail; unknown subcommand or bad option → 2; `audit serve` with missing `[ui]` extra → 1 |
| `bridge check-access` | 0 | — | — | no ticket id; subprocess passthrough (jira-capability-probe.py); 0 = PROBE_PASS, non-zero = PROBE_FAIL |
| `bridge-probe` | 0 | — | — | compatibility alias for `bridge check-access`; preserves the same subprocess exit status |
| `config` | 0 | — | — | no ticket id; reads or writes rebar config; error → 1 |
| `criteria` | 0 | — | — | no ticket id; `criteria eval <id>` runs calibration fixtures live; `criteria eval --changed-since <ref>` prints selected criterion ids and exits 0 (a null/all-zero `before` SHA from a branch's first push selects nothing and exits 0 with a stderr note); an unmappable rubric path is named on stderr and still exits 0; with `--require-live`, selected criteria that cannot run live (no LLM backend/credentials) → 1 (otherwise a stderr warning and 0); positional id and `--changed-since` together → 2; empty or missing id with no `--changed-since` → 2; unknown criterion → 1 |
| `doctor` | 0 | — | — | no ticket id; 0 = no outstanding findings (or all repaired); 1 = findings remain; bad args → 2 |
| `enrich` | 0 | — | — | no ticket id; cross-ticket overlap drain; always 0 on a clean run |
| `explain` | 0 | — | — | no ticket id; `explain <criterion-id\|guide>`; unknown id or guide name → 1 |
| `export` | 0 | — | — | no ticket id; NDJSON ticket export; bad args → 2 |
| `grounding-info` | 0 | — | — | no ticket id; static code-grounding contract info; unexpected args → 1; bad `--output` → 2 |
| `idea` | 0 | 1 | — | promotes an `idea` ticket to `open`; bad args → 2 |
| `identity` | 0 | — | — | no ticket id; shows or configures operator identity; no args → 1; error → 1 |
| `import` | 0 | — | — | no ticket id; NDJSON ticket import; bad args → 2 |
| `bridge setup` | 0 | — | — | no ticket id; interactive Jira config wizard; error or user abort → 1 |
| `jira-onboard` | 0 | — | — | compatibility alias for `bridge setup`; preserves the same wizard behavior and error or user-abort exit 1 |
| `llm` | 0 | — | — | no ticket id; `llm setup` FakeRunner dry-run; 0 = dry-run OK, 1 = dry-run failed or write error; no subcommand → 1 |
| `metrics` | 0 | — | — | no ticket id; repo-wide metrics report; bad args → 2 |
| `prompt` | 0 | — | — | no ticket id; `prompt eval <id>` validates a prompt's eval spec; error → 1; no subcommand → 1 |
| `reconcile` | 0 | — | — | no ticket id; compatibility subprocess passthrough retaining reconciler codes 3/4/75 |
| `remote-cert` | 0 | — | — | no ticket id; trusted op-cert gate service client; bad args → 2; error → 1 |
| `review-code` | 0 | — | — | no ticket id; PASS → 0, BLOCK → 1, INDETERMINATE → 2, retryable degrade → 11 |
| `review-plan` | 0 | 1 | — | PASS → 0, BLOCK → 1, INDETERMINATE → 2, retryable degrade → 11; `--status`: 0 = current, 12 = stale/absent; `--retry`: resumes the latest INDETERMINATE (same 0/1/2/11 dispositions), or exits 2 refusing an ineligible resume / a `--force`/`--status`/`--check` conflict |
| `scan-spec` | 0 | — | — | no ticket id; `--spec-file` read error or LLMError → 1 |
| `session-log` | 0 | 1 | — | appends or reads session log events; error → 1 |
| `session-logs` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `sign` | 0 | 1 | — | signs the ticket's current event manifest; bad args → 2 |
| `sign-review` | 0 | 1 | — | cheap re-sign of an existing PASS verdict (no LLM); refused when sidecar absent, non-PASS, or plan changed → 1 |
| `trusted-env` | 0 | — | — | no ticket id; maintains `.rebar/trusted_environments.yaml`; bad args → 2; error → 1 |
| `verify-authorship` | 0 | — | — | back-compat alias for `verify-identity`; same codes: 0 = verified, 1 = not-verified, 2 = bad args |
| `verify-commit-ticket` | 0 | — | — | no ticket id; 0 = commit has a valid rebar-ticket trailer, 1 = missing or invalid, 2 = bad args |
| `verify-completion` | 0 | 1 | — | PASS → 0, ordinary FAIL → 1, nonretryable raised `LLMError` → 1, retryable raised `LLMError` → 11, `verdict_obtainable=false` → 11, insufficiency-only FAIL (`evidence_sufficient=false`) → 11 |
| `verify-identity` | 0 | — | — | authenticated-authorship merge gate; 0 = verified, 1 = not-verified, 2 = bad args |
| `verify-opcert` | 0 | — | — | no ticket id; 0 = op-cert valid, 1 = invalid or not found, 2 = bad args |
| `verify-signature` | 0 | — | — | no ticket id; certifies a manifest attestation (shape-aware: an asymmetric op-cert, or a legacy record); 0 = signature verified, 1 = not-verified (or, for a legacy HMAC record, a missing key), 2 = bad args |
| `workflow` | 0 | — | — | no ticket id; `run` → 0 = succeeded, 1 = not-succeeded; `validate` → 0 = valid, 1 = invalid; other errors → 1 |

(The meta `help` arm and `rebar` with no subcommand are excluded: `help` exits 0,
a missing/unknown subcommand prints the overview and exits 1.)

## Documented exceptions

These commands deliberately depart from "0=success / 1=error":

1. **The per-ticket gates** (`check-ac`, `quality-check`, `clarity-check`)
   overload exit `1` as a **fail verdict** — a ticket that exists but does not
   meet the gate. Because `1` is spent on the verdict, `clarity-check` signals a
   not-found/usage condition with `2` instead; `check-ac` and `quality-check`
   fold not-found into `1` (their negative verdict and not-found are the same
   code). Treat a gate's `1` as "did not pass," not "crashed."

2. **`validate`** is a repo-wide health check whose exit code encodes the overall
   health **severity** (a 0-4 bucket: lower is healthier), not the standard
   contract. It takes no ticket id; passing one is a usage error (1).

3. **`exists`** intentionally uses `0`/`1` as a boolean presence answer
   (0=exists, 1=not), so a "1" there is the normal negative result.

4. **Tolerant reads** (`summary`, `list-descendants`, `format`) return `0` for an
   unknown ticket id rather than `1` — they render an empty/placeholder result
   so batch callers don't have to pre-filter ids.

## Recorded decisions (2026-06-09 window)

While writing this contract, two deviations from "unknown option → 2" were found
and **resolved by fixing the code** (not the doc), because every other read
command already returned 2 and `reads_cli._cmd_deps` even documented the
cohort as "matching list/show/ready/search":

- `rebar show <id> --bad-opt`: was `1`, now **`2`** (`reads_cli._cmd_show`).
- `rebar list --bad-opt`: was `1`, now **`2`** (`reads_cli._cmd_list`).

The gate convention (clarity-check not-found = 2) and `validate`'s health-bucket
exit were **kept as-is and documented** rather than changed, to avoid altering
verdict/severity semantics that agents already depend on.

### Exit 11 block-but-retryable outcomes

Exit 11 distinguishes a retryable gate fault from a judgment about the reviewed work. The provider-degradation mapping is recorded by story `authorial-hated-blackbear` and epic `jira-reb-687`. The bounded completion recovery mapping is recorded by `xenophobic-allpurpose-serval`.

- **Review degradation.** `review-plan` and `review-code` return `11` when `coverage.retryable` is true. A nonretryable INDETERMINATE returns `2`.
- **Raised completion errors.** `verify-completion` and the `close` completion gate inspect the raised `LLMError` outcome. A retryable outcome returns `11`. A nonretryable outcome fails closed with `1`.
- **Unusable completion results.** Bounded recovery can return `verdict_obtainable=false` after it cannot obtain an itemized completion judgment, or a FAIL whose evidence search was truncated or exhausted before a criterion could be refuted (top-level `evidence_sufficient=false`, recorded by bug `2dcb-5468-b734-4b60`). Both `verify-completion` and the `close` gate return `11` for either result: the run could not produce a trustworthy verdict and is worth re-running. An ordinary completion FAIL — one that refuted the criteria on sufficient evidence (`evidence_sufficient` is not false) — remains `1` because it states that acceptance criteria are unmet.
- **Consumer guidance.** Retry `11` after the indicated backoff. Treat every other nonzero code according to its documented contract. Consumers that treat every nonzero code as failure remain compatible.
- **Other consumers.** Project CI does not invoke these gate commands. The Gerrit review bot consumes verdict dictionaries instead of CLI exit codes.
- **Rollback.** Removing the mapping would restore the earlier `2` result for retryable review degradation and the earlier `1` result for retryable completion faults. The optional classifier fields require no data migration.

### The `--review` flag on `edit` and `claim` (2026-07 window, story `a114-8f96-ff2d-461d`)

`rebar edit <id> ... --review` and `rebar claim <id> --review` fuse the common
"mutate, then re-run the plan review" loop into the consuming verb. **These codes
apply ONLY when the flag is passed; the flagless `edit` and `claim` contracts in
the tables above are unchanged.**

- **`edit --review`**: the EDIT event commits first (and stays committed whatever
  the verdict); the process then exits with the review's disposition mapping —
  `0` PASS, `1` BLOCK, `2` INDETERMINATE (non-retryable), `11` retryable degrade
  (the same ADR-0040 mapping `review-plan` itself uses). An edit-side failure
  before the review still exits with edit's own codes (`1`).
- **`claim --review`**: when the plan-review gate applies and the attestation is
  stale/missing, the signed review runs BEFORE the claim. The claim proceeds only
  on a PASS (then the normal codes apply: `0`, or `10` on a concurrency loss);
  on BLOCK / INDETERMINATE / retryable degrade the claim core is **never
  invoked** and the exit is `1` / `2` / `11` respectively, with the review
  summary printed. When the gate is disabled or the ticket type is exempt, a
  one-line notice is printed and the claim proceeds normally.
- Neither flag holds the store write lock while the review runs, and neither is
  atomic against concurrent store reconvergence — check attestation currency
  cheaply with `rebar review-plan <id> --status` (exit `0` current / `12` not).
