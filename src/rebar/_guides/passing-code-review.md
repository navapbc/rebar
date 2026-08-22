# Writing a change that passes code review

**Audience: the change author, about to push a commit to Gerrit for the `LLM-Review` +
`Verified` gates.** This is the on-ramp. The authoritative detail lives in
[review-policy.md](https://github.com/navapbc/rebar/blob/main/docs/review-policy.md) (what the
votes mean),
[review-kernel.md](https://github.com/navapbc/rebar/blob/main/docs/review-kernel.md) (how the
LLM reviewer decides), and
[CONTRIBUTING.md](https://github.com/navapbc/rebar/blob/main/CONTRIBUTING.md) §2 (the exact
mechanics). Read those when you need depth. See also its sibling guide — run `rebar explain
plan` — for the *plan*-review gate that runs before you claim a ticket. `rebar explain review`
prints this file. Just as `rebar review-plan`
lets you run the plan gate locally before you claim, `rebar review-code` runs **this** gate's
reviewer locally before you push — see [Preview the review locally](#preview-the-review-locally-before-you-push).

## The loop

Every change to `main` needs **two independent `+1` votes and no unresolved comments** before
it can submit. Only bots/admins cast the votes — you cannot self-approve.

```
commit ──▶ [rebar review-code — optional local preview] ──▶ git push origin HEAD:refs/for/main
   ▲                                                              │
   │                                    ┌─────────────────────────┘
   │                                    ▼
   │             LLM-Review +1  AND  Verified +1  (no unresolved comments) ──▶ Submit
   └──── amend --no-edit + re-push ◀── findings / CI failure
```

Iterate by **amending the same commit** (`git commit --amend --no-edit`, keep the `Change-Id`)
and re-pushing — each push is a new patchset and both bots re-run.

**While a change has known unresolved issues, hold it in Gerrit's Work In Progress state** —
`git push origin HEAD:refs/for/main%wip` at push time, or `POST /a/changes/<n>/wip` on a change
already up (`%ready` / `POST …/ready` to release it). A hold recorded only in a ticket comment
is advisory and invisible to whoever submits next; WIP is enforceable, because Gerrit refuses to
submit a WIP change. See
[CONTRIBUTING.md](https://github.com/navapbc/rebar/blob/main/CONTRIBUTING.md) §2d.

## Before you push — the commit checklist

CI's `Verified` gate rejects the push outright if any of these is missing:

- **A `rebar-ticket: <id>` trailer** (or a leading `<id>:` subject) — every commit references a
  claimed ticket. Run `rebar explain commit-trailer` for the exact format and accepted id forms,
  and `rebar verify-commit-ticket` to check your HEAD locally before you push.
- **A DCO sign-off** — exactly `Signed-off-by: <real name> <email>`, added with `git commit -s`.
  Real name, no pseudonyms; enforced at push time.
- **A `Change-Id`** — auto-stamped by the Gerrit `commit-msg` hook. A fresh worktree needs the
  hook installed (see
  [CONTRIBUTING.md](https://github.com/navapbc/rebar/blob/main/CONTRIBUTING.md) §1b).
- **Push to the magic ref** — `git push origin HEAD:refs/for/main` (creates/updates a change;
  never touches `main`). GitHub is a read-only mirror; PRs don't merge.

## The `Verified` gate (CI) — approximate it locally

`Verified` runs far more than three make targets. Locally approximate with
`make lint && make typecheck && make test`, but the **workflow file is authoritative** — it
also runs the `rebar-ticket` trailer check, the **module-size gate** (hard 800-LOC cap per
`src/rebar` file), prompt-index/env-registry drift gates, security-rules freshness,
criteria-routing parity, public-types drift, `make config-check`, `pip-audit`, and both unit +
integration pytest tiers. A `Verified -1` is a CI failure: open the run link. If it's a flake,
comment **`recheck`** on the change.

## The `LLM-Review` gate — what the reviewer scores

An LLM reviews your diff across these **dimensions** (overlays). The reviewer cites evidence in
your diff; a separate verifier re-grounds each finding; a deterministic policy decides blocking
by a `priority = validity × impact` score against a per-dimension threshold — **the model never
sets severity itself.**

- **Blocking today:** `security` (authn/authz, secrets, injection, unsafe deserialization) and
  the deterministic secret / high-critical-security detectors. Treat anything these flag as a
  hard stop.
- **Advisory (coaches, won't block — but address them):** `performance`, `tests`, `api-compat`,
  `db-migrations`, `supply-chain`, `iac`, `i18n`, `a11y`, `docs`, `llm-prompts`,
  `deletion-impact` (a removed def/signature leaving dangling references), and `scope-intent`
  (your diff drifting from the union scope/acceptance-criteria of the commit's tickets).

**To pass cleanly, a change should:** keep the diff within its ticket's stated scope; add or
update tests for changed behavior (not snapshot-of-current-output); keep public API / CLI /
config / wire formats backward-compatible or call the break out; update docs that track the
change; and never introduce a secret or an unauthenticated exposure on a security-sensitive
path. Keep each `src/rebar` file **under 800 LOC** — the module-size gate is a `Verified`
failure, not an advisory.

### Fail-open checks — name every world in which your check passes

Two of the reviewer's dimensions (`tests` and `error-handling`) look specifically for a
**fail-open check**: a check, oracle, guard, or health probe whose PASS/absent/empty result is
indistinguishable from the check never having run. It reports success by producing nothing —
and nothing is also what a non-run produces, so the failure is invisible exactly when it
matters. The litmus, before you push: *for each check your change adds or modifies, name every
world in which it produces its definite result — pass or fail. If "the thing being checked
never executed", "the producer emitted nothing", or "the instrument received the wrong input"
is among them, the check cannot distinguish its verdict from its own failure.* The remediation
is cheap and uniform: **assert the producer RAN** (exit status, non-empty capture, an explicit
liveness signal) before trusting any absence derived from it.

Two worked examples from the nine recorded instances that motivated this rubric:

- **An absence assertion on a child's output (`tests`).** A test asserted a warning string was
  ABSENT from a subprocess's stderr. A signal-killed child writes nothing, so the string was
  trivially absent and a run that never executed reported GREEN. Fixed form: assert
  `proc.returncode == 0` and a known PRESENCE from the same capture (the worker's startup
  banner) *before* the absence — the same assertion, now anchored to a producer that provably
  ran.
- **A silent lookup miss (`error-handling`).** An env-var registry generator looked helpers up
  in `KNOWN_ENV_HELPERS` and silently `continue`d on a miss — absent was not an error, it was
  INVISIBLE, so operator-settable variables went undocumented while `--check` stayed green for
  days. Fixed form: an unknown helper raises, naming the variable — the miss path fails
  CLOSED.

The same shape's mirror also counts: an instrument that produces a confident **failing** result
for a reason other than the condition (wrong input, a format the matcher can't see) triggers
recovery work against a system that was never broken. If you are verifying an absence, verify
your instrument can still see a presence.

## A big bug fix needs a reviewed plan

One blocking finding is not about your code at all. If your commit's `rebar-ticket:` names a
**bug** and the diff touches **more than 150 non-test lines**, the `bugfix-size-attestation`
criterion blocks unless that bug carries a current plan-review attestation — a fix that large is a
design change wearing a bug label. The remedy is a ticket action, not a code change:

```bash
rebar review-plan <id> --status   # is an attestation current right now? (read-only, no LLM)
rebar review-plan <id>            # write the fix plan into the description first; signs on a PASS
rebar sign-review <id>            # only if the review PASSED but no attestation landed
```

Then amend and re-push. The gate asks only whether an attested plan review **was completed**, never
which machine certified it, so running the review locally satisfies it. Note the one thing that
still bites: attest *first, then* edit the plan and the attestation goes `stale-material` and blocks
again — re-review after any plan edit. Only Gerrit runs this criterion; a local `review-code`
preview never blocks on it. Details:
[plan-review-gate.md](https://github.com/navapbc/rebar/blob/main/docs/plan-review-gate.md).

## Preview the review locally before you push

You don't have to wait for the bot. `rebar review-code` runs the **same** four-pass reviewer
the bot casts `LLM-Review` with, over your diff, on your machine — the code-review analog of
`rebar review-plan` for the plan gate. Catch and fix findings before you push, so the first
patchset the bot sees is already clean.

```bash
rebar review-code --base origin/main --head HEAD -o text   # preview findings for your change
rebar review-code --diff-file change.diff -o text          # or review a saved unified diff
```

Two setup notes, mirroring `review-plan`'s requirements:

- **It needs the `[agents]` extra + a model API key** (`pip install 'nava-rebar[agents]'`,
  `export ANTHROPIC_API_KEY=…`). Without them there's nothing to run the LLM passes with —
  the run degrades to an INDETERMINATE result (exit 2), never a silent empty pass.
- **No config key gates it** — an explicit `rebar review-code` always runs the review, just
  like `rebar review-plan`. (`verify.enable_code_review` only controls whether AUTOMATED
  dispatch paths run the gate; it never blocks an explicit invocation.)

The local run is a **preview, not a vote**: it never touches Gerrit and its findings are keyed
to your session, so a local review never seeds the change's first bot review — the bot still
reviews from scratch. Treat a local finding exactly as you would the bot's: fix `security` and
secret/high-critical findings before you push (they block), and address the advisories.

## Responding to votes

Read the `LLM-Review` tag — it tells you whose problem it is:

- **`BLOCK — finding`** (with inline comments): a real issue in *your* code. Fix it, amend,
  re-push, and mark each inline comment **Done** (submit requires no unresolved comments).
- **`BLOCK — coverage-gap (…)`** (gate-disabled / llm-unavailable / scanner / review-error /
  indeterminate / merge-review): an **infrastructure veto, not your diff** — once the
  infrastructure issue clears, comment **`rerun-llm-review`** on the change to re-trigger
  the review yourself (self-service; the bot refuses the trigger only when the standing
  `-1` is a real finding). No code change and no re-push needed. The two triggers are
  parallel but deliberately share **no substring**: `recheck` re-runs CI (`Verified`),
  `rerun-llm-review` re-runs the LLM review (`LLM-Review`). The older LLM word embedded
  `recheck`, so CI's substring matcher fired on it too and cancelled in-flight runs; it was
  retired outright and now draws a refusal reply naming `rerun-llm-review`.
- **`PASS`**: nothing to do.

The gate is **required-with-human-override**: the lead maintainer owns the rubric and can waive
a finding. To dispute one, resolve the thread with a written justification or escalate (expect
a best-effort response within ~5 business days); bypasses are admin-only and audited. And note
the **responsibility clause** — you must be able to personally explain your change; "the tool
wrote it" is not an answer.

## Re-reviews converge

On a re-push, a novel low-priority advisory finding is dropped **only if the cited code region
is unchanged**; a repeat finding on code you *did* touch is re-raised. So don't expect an
advisory to disappear just because you pushed again — change the cited region or address it.
