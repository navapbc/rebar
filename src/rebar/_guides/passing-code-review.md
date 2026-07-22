# Writing a change that passes code review

**Audience: the change author, about to push a commit to Gerrit for the `LLM-Review` +
`Verified` gates.** This is the on-ramp. The authoritative detail lives in
[review-policy.md](review-policy.md) (what the votes mean), [review-kernel.md](review-kernel.md)
(how the LLM reviewer decides), and [CONTRIBUTING.md](../CONTRIBUTING.md) §2 (the exact
mechanics). Read those when you need depth. See also its sibling,
[writing-a-passing-plan.md](writing-a-passing-plan.md), for the *plan*-review gate that runs
before you claim a ticket. `rebar explain review` prints this file. Just as `rebar review-plan`
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

## Before you push — the commit checklist

CI's `Verified` gate rejects the push outright if any of these is missing:

- **A `rebar-ticket: <id>` trailer** (or a leading `<id>:` subject) — every commit references a
  claimed ticket (`docs/commit-ticket-trailer.md`).
- **A DCO sign-off** — exactly `Signed-off-by: <real name> <email>`, added with `git commit -s`.
  Real name, no pseudonyms; enforced at push time.
- **A `Change-Id`** — auto-stamped by the Gerrit `commit-msg` hook. A fresh worktree needs the
  hook installed (see CONTRIBUTING.md §1b / AGENTS.md).
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
  `export ANTHROPIC_API_KEY=…`). Without them there's nothing to run the LLM passes with.
- **The capability is off by default** — enable it for the run with
  `REBAR_VERIFY_ENABLE_CODE_REVIEW=1` (or `verify.enable_code_review = true` in config).
  Left off, `review-code` returns an inert empty result (zero findings, zero LLM calls) and
  you'll see nothing — that's the disabled state, not a clean pass.

The local run is a **preview, not a vote**: it never touches Gerrit and its findings are keyed
to your session, so a local review never seeds the change's first bot review — the bot still
reviews from scratch. Treat a local finding exactly as you would the bot's: fix `security` and
secret/high-critical findings before you push (they block), and address the advisories.

## Responding to votes

Read the `LLM-Review` tag — it tells you whose problem it is:

- **`BLOCK — finding`** (with inline comments): a real issue in *your* code. Fix it, amend,
  re-push, and mark each inline comment **Done** (submit requires no unresolved comments).
- **`BLOCK — coverage-gap (…)`** (gate-disabled / llm-unavailable / scanner / review-error /
  indeterminate / merge-review): an **infrastructure veto, not your diff** — a maintainer
  re-triggers; re-pushing the same commit or pinging the maintainer is the move, not a code
  change.
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
