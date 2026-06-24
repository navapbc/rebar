# Exit codes — rebar's CLI process-status contract

rebar's exit codes are **load-bearing for agents**: the parallel-agent workflow
keys off them (a claim that loses a race is exit 10, not a crash; a missing
ticket is exit 1, not 0-with-empty). This document is the single source of truth
for what each code means and which code each subcommand emits. It is pinned by
`tests/interfaces/lifecycle/test_exit_codes.py`, which fails if the codes drift.

This contract is **frozen** as of the 2026-06-09 breaking-change window. Changes
to an emitted code are contract changes and must be called out in release notes.

## The four codes

| Code | Name | Meaning |
|-----:|------|---------|
| `0`  | success | The command did what it was asked. (Read commands that find nothing still succeed — an empty list is exit 0.) |
| `1`  | runtime error | Ticket not found, invalid input value, a missing **required positional** argument, a failed precondition, or a per-ticket gate's **fail verdict**. The general-purpose error code. |
| `2`  | usage error | An unrecognized CLI `--option` on a **structured read command** (`show`, `list`, `deps`, `ready`, `search`), which reject unknown options rather than silently ignoring them. Also the not-found/usage path of `clarity-check` (see the gate note below). |
| `10` | concurrency mismatch | Optimistic-concurrency rejection: a state-dependent op (`transition`/`claim`/`reopen`) re-read the ticket under lock and the actual status no longer matched the expected one. **Normal under parallelism** — re-read and pick another, never force. Emitted by `_commands/txn.py` (`ConcurrencyMismatch`). |

### Cross-cutting rules

- **Unknown option → `2` (structured reads only).** `rebar list --bogus`,
  `rebar show <id> --bogus`, `rebar ready --bogus`, `rebar search q --bogus`,
  `rebar deps <id> --bogus` all exit `2`. (`show`/`list` historically returned
  `1` here; aligned to `2` in the 2026-06-09 window — see "Recorded decisions"
  below.) **This is the full set of commands that validate options today** — see
  "Unknown-option handling" for the scope and the known gap on other commands.
- **Missing required positional → `1`.** `rebar show` (no id), `rebar create`
  (no type/title), `rebar link a b` (no relation), `rebar deps` (no id) → `1`.
  A missing *positional* is a runtime error (1); a malformed *option* on a
  structured read is a usage error (2).
- **State mismatch on a status-dependent op → `10`.** Includes `transition`
  with a stale `current` status, `claim` of a non-open ticket, and `reopen` of a
  non-closed ticket.

### Unknown-option handling (scope + known gap)

Only the five **structured read commands** validate their options and exit `2`
on an unrecognized `--option`: `show`, `list`, `deps`, `ready`, `search` (all
route through `ticket_reads.py` / `parse_output`). These are pinned by
`test_exit_codes.py::test_unknown_option_exits_2`.

Other subcommands do **not** uniformly validate options: most mutation commands
either silently ignore an unknown `--option` (e.g. `comment`, `tag`, `claim`,
`check-ac` → exit `0`) or fail incidentally (e.g. `archive`, `edit`, `create` →
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
| `bridge-fsck` | 0 | — | — | audit; no ticket id |
| `bridge-status` | 0 | — | — | no ticket id |
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
| `fsck-recover` | 0 | — | — | no ticket id |
| `get-file-impact` | 0 | 1 | — | |
| `get-verify-commands` | 0 | 1 | — | |
| `init` | 0 | — | — | idempotent |
| `link` | 0 | 1 | — | missing relation arg → 1 |
| `list` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `list-descendants` | 0 | 0 | — | tolerant read: unknown root → empty buckets, 0 |
| `list-epics` | 0 | — | — | exit reflects epic availability: 0=an unblocked epic, 1=no open epics, 2=all blocked |
| `next-batch` | 0 | 1 | — | |
| `purge-bridge` | 0 | — | — | no ticket id |
| `quality-check` | 0 | 1 | — | **gate**: 0=dispatch-ready, 1=not-ready **or** not-found |
| `ready` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `reopen` | 0 | 1 | 10 | 10 when the ticket is not closed |
| `resolve` | 0 | 1 | — | |
| `revert` | 0 | 1 | — | missing `<ticket_id> <uuid>` → 1 |
| `scratch` | 0 | 1 | — | |
| `search` | 0 | — | — | structured read (unknown option → 2); empty result still 0 |
| `set-file-impact` | 0 | 1 | — | malformed JSON arg → 1 |
| `set-verify-commands` | 0 | 1 | — | malformed JSON arg → 1 |
| `show` | 0 | 1 | — | structured read (unknown option → 2); not-found also emits a parseable JSON error on stdout |
| `summary` | 0 | 0 | — | tolerant read: unknown id renders `[unknown]`, still 0 |
| `tag` | 0 | 1 | — | |
| `transition` | 0 | 1 | 10 | 10 on stale `current` status |
| `unlink` | 0 | 1 | — | |
| `untag` | 0 | 1 | — | removing an absent tag is still 0 |
| `validate` | **0-4** | — | — | **exception**: exit is a health-severity bucket, not the standard contract; takes **no** ticket id (passing one → 1) |

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
command already returned 2 and `ticket_reads._cmd_deps` even documented the
cohort as "matching list/show/ready/search":

- `rebar show <id> --bad-opt`: was `1`, now **`2`** (`ticket_reads._cmd_show`).
- `rebar list --bad-opt`: was `1`, now **`2`** (`ticket_reads._cmd_list`).

The gate convention (clarity-check not-found = 2) and `validate`'s health-bucket
exit were **kept as-is and documented** rather than changed, to avoid altering
verdict/severity semantics that agents already depend on.
