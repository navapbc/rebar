# ADR 0058 — `.github/module-size-limit.txt` is the ONLY LOC ceiling

**Status:** Accepted (epic 061c; ticket db7f)

## Context

The module-size policy has one intended enforcement point: the hard cap in
`.github/module-size-limit.txt`, applied by the CI **Module-size gate** to every
`src/rebar/**/*.py` file. That value is **locked** — the gate compares it against `main` and
fails when a change alters it, so raising or lowering the cap requires an administrator to
override the gate. `tests/unit/test_module_size_contract.py` mirrors the gate in-process and
**reads the same file**, so the gate and the test can never disagree on the number.

Alongside that, a second, unintended enforcement layer accumulated. Individual seam tests grew
their own hard-coded ceilings — `< 700` on `runner.py`, `gate_dispatch.py`, `orchestrator.py`
and `llm/config.py`; `<= 560` on `plan_review/workflow_ops.py`; `< 500` on `structured_run.py`;
and per-file copies of the 800 cap itself. Each was added in good faith as one story's
acceptance criterion ("this split must produce real headroom") and then left behind as a
permanent ratchet.

AGENTS.md states the policy as **"Target 200–500 LOC per file; hard cap 800."** A target is
guidance for judgement. Asserting it turns it into a build failure.

## Decision

**`.github/module-size-limit.txt` is the single authoritative LOC ceiling.** It is enforced by
the CI Module-size gate and mirrored, single-sourced, by `test_module_size_contract.py`. No
other test may assert an upper LOC bound on a `src/rebar` file, and none may hard-code a
ceiling value.

The **anti-fragmentation floor is retained** and is not covered by this prohibition. AGENTS.md
states it as "never create files < 100 LOC by splitting" — a prohibition, not a target — and it
guards a failure the cap structurally cannot see: the gate only rejects files that are too
large, so it would happily accept a file mechanically shredded into 40-line fragments. A floor
is the opposite check, not a duplicate one.

## Why the stricter ceilings were wrong

**They inverted the governance.** The 800 cap is deliberately locked behind an administrator
override. The stricter per-file bounds that actually constrained those modules were bare
integer literals in test files that any contributor could edit, unreviewed. The tightest
constraint in the repo was also the least governed one.

**They failed mid-refactor by construction.** A decomposition spanning several commits
necessarily passes through intermediate states where the file has grown (new code lands) before
it shrinks (the extraction lands). A per-commit CI system checks every one of those states, so
a ceiling asserted on a hot file turns the middle of every multi-commit decomposition red — and
the change most likely to trip it is precisely the refactor the ceiling exists to encourage.
Landing such a stack then requires manual `Verified` overrides, which is the failure mode this
ADR removes.

**One of them was self-locking.** Two assertions checked that the literal strings
`"(_WORKFLOW_OPS, _HEADROOM_TARGET)"` and `"(_LLM_CONFIG, _HEADROOM_TARGET)"` appeared in
*another test file's source text* — tests whose only function was to prevent removal of a
bound. A rule that defends itself against deletion is not a test.

**The pattern had already cost a full investigation.** Bug `3a98-36c9-9f41-42ec` was opened
when `main` grew `runner.py` and made a `< 700` target unreachable. It was resolved by doing
more extraction rather than by questioning whether the bound should exist, and the file later
drifted back to a one-line margin — so the same failure was queued up again.

## Consequences

- A module may sit anywhere below the hard cap without a test objecting. "Has headroom" is a
  judgement for review and for the ADR that motivates a decomposition, not a build gate.
- Removing these ceilings does **not** weaken enforcement: the gate and its single-sourced
  mirror are untouched, and both still fail on any `src/rebar` file over the cap.
- A future story that wants headroom states it in its own acceptance criteria and verifies it
  in review. It must not encode a new ceiling in a test.
- If a global headroom target is ever wanted, it belongs beside the limit file as another
  single-sourced, locked value checked by the one mirror — not as per-file literals scattered
  across seam tests.

## Rejected alternatives

**Keep the ceilings but single-source them.** This fixes the governance inversion but not the
mid-refactor failure: a stricter bound read from a shared file still turns intermediate commits
red. It also keeps two numbers where the policy defines one.

**Keep them and rely on overrides.** Every multi-commit decomposition would continue to need
manual `Verified` overrides. That trains reviewers to override a red gate as routine, which is
corrosive to the gate's meaning — the cost is paid on exactly the changes that most deserve
scrutiny.
