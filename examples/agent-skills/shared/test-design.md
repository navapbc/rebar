# Test design — the shared TDD standard

This standard governs **choosing and designing the right test**. Your skill's own
protocol governs RED→GREEN integrity (RED-first ordering, the held-out oracle, mutation
checks, the refactoring litmus) and is unchanged. Load this file:

- **rebar-brainstorm** — before recording tickets whose acceptance criteria involve
  testing, so each AC names the tier and the observable oracle.
- **rebar-debug** — at Phase 2 entry, to design the RED oracle.
- **rebar-implement** — at Phase 4a/4b, to author the test set.

A test can be perfect on RED→GREEN mechanics and still be the wrong test. Work through
the sections below to choose the right one.

## Scale ceremony to the work

Sections 1, 3, 4, 5A–B, and 8 apply to every test. The rest fire on their trigger:
5C–D on stateful/atomic operations, 6 on input-class and stateful defects, 7 on
recovery/cleanup/concurrency tests, 10 on LLM surfaces. A pure-function fix clears this
file in minutes; give heavier work the sections it triggers.

## 1. Inventory existing coverage first

The required artifact is a **RED oracle** — an existing failing test, an existing test
strengthened into its final RED form, or a new test, in that order of preference.
Before writing anything, search the suite by: the contract's wording, the public entry
point, the implementation symbols, the exact error text, the bug/ticket identifier, and
neighboring test filenames. Read the nearest tests. Strengthening an existing weak test
upgrades coverage where a duplicate would only add maintenance.

## 2. Freeze a test contract card

Before test code, write a compact design record and check it is internally consistent:

```yaml
authoritative_contract:   # what SHOULD happen, with citation (spec/doc/ADR/prior test)
trigger_preconditions:    # the exact state that forces the mechanism to execute
production_path:          # the smallest real path that crosses the faulty seam
test_tier:                # from §3, plus why one tier lower is insufficient
observable_postcondition: # the exact contractual consequence to assert
ci_gate:                  # the gating CI command the test runs under
# conditional:
negative_control:         # nearest input/state where behavior must NOT change (§6)
collateral_invariants:    # state that must remain untouched (§5C)
recovery:                 # required clean-retry behavior (§5D)
fault_injection:          # where and how the fault is injected (§7)
```

Ground `authoritative_contract` in a citation, so the oracle encodes the contract
rather than the reporter's assumption. The card is also the piece you may share with a
held-out fixer or implementer — your skill says which parts.

## 3. Select the test tier

**Use the smallest tier that still crosses every component involved in the proven
mechanism.** This is a gate: every component of the mechanism must run for real.

| Bug class | Default test shape |
|---|---|
| Pure calculation / validation branch | In-process unit test against the real function |
| Producer/consumer shape mismatch | Contract test: real producer output fed to the real consumer through real serialization |
| CLI parsing, streams, env, exit codes | Real CLI subprocess + persisted-state assertion |
| Git refs, merging, reachability, data loss | Real temporary git repositories and refs |
| Locking / race | Multiple real processes with a deterministic barrier (§7) |
| Parent exit, signals, fd inheritance | Parent/child subprocess test |
| Crash recovery | Kill the real process after a controlled failpoint, then restart normally |
| External-service mapping | Stateful verified fake fed production-shaped fixtures; live canary where justified |
| LLM instruction behavior | Behavioral eval with pinned config and repeated trials (§10) |

Work spanning several bug classes takes the tier per mechanism-component. At planning
time this rule is a readiness check: acceptance criteria name both the observable oracle
and the tier, and a plan whose mechanism is too unclear to pick a tier has an unresolved
design decision — resolve it (research, a spike, a clarifying question) before recording
the ticket. Mock only the irreducible external boundary (network, third-party service,
wall clock), preferably with a stateful fake verified against production-shaped data.

## 4. Trigger the mechanism; assert the contract

Arrange the conditions that force the confirmed mechanism to execute through the
smallest production path crossing the faulty seam. Assert the **public or contractual
consequence**: the return value, persisted state, emitted event, written file, exit
code, or semantic round-trip the contract promises. Structure (a call sequence, an
intermediate shape, source text) is assertable only when it is itself the documented
contract. Examples of contract-level oracles:

- reconvergence: the local commit remains reachable after the operation and aggressive
  pruning;
- schema handling: real producer output fed to the real consumer yields the semantically
  correct result;
- rollback: state is restored exactly, no partial artifacts remain, and a retry succeeds.

## 5. The minimum behavioral oracle

**A. Prove the preconditions.** Assert the fixture reached the dangerous state before
exercising the mechanism (the branch ref differs from detached HEAD; local is ahead of
origin; the first rename completed before the injected failure).

**B. Assert an exact postcondition.** Assert the specific value, state, or effect the
contract promises — an assertion a regression is forced to fail. Existence,
non-emptiness, and no-exception checks are not oracles.

**C. Collateral invariants (stateful operations).** Also assert what must remain
unchanged: unrelated fields, the complete event set, exact bytes, commit reachability, a
clean index/worktree, no stale lock or temp artifacts.

**D. Recovery (atomicity/recoverability contracts).** Include a successful clean retry
after the failure; restoration plus retry is what proves the contract.

## 6. Contrast case (default for input-class and stateful defects)

Pair the failing case with its nearest meaningful control — the input or state where
behavior must not change. For an input-class defect, add a boundary or second
representative input; parameterize when the mechanism applies to a family. The control
proves the test distinguishes broken from working. Omit only for a genuinely
single-point bug, and say so.

## 7. Fault realism

Inject the fault **inside the code whose recovery/cleanup behavior is under test** — at
the exact operation the contract covers: fail the second real `os.rename`; block the
detached child at the actual `git push`, observe parent exit, then release it; kill the
process after the first retirement rename; use a loopback server for transport
timeouts. A fault injected above the seam leaves the recovery code unexecuted.

For races, synchronize with a barrier, pipe, marker file, socket, or explicit failpoint.
Timing sleeps may only be secondary settling waits, never the primary mechanism.

## 8. Prove the test gates

Confirm and record compactly (e.g. `collected: yes · executed: yes · gate: make test`):

- the runner collects the exact node;
- it executes — not SKIP/XFAIL — under the supported CI environment;
- required executables/imports hard-fail when broken;
- any temporary `xfail` is `strict=True` with a stated removal condition;
- the test runs under at least one gating CI command.

## 9. Defect-seeded mutation

When your skill's mutation check runs, derive the mutation from the confirmed defect:
re-introduce the exact root cause (restore the wrong ref, re-omit the missing field,
delete the rollback loop) and prove the test goes RED. Optionally add one near-miss
fix — superficially plausible but incomplete — and show the test rejects it.

## 10. LLM-surface evals

For a prompt/skill/instruction change, quantify:

- pin the model/version and decoding settings;
- pair before/after prompts differing only in the proposed change;
- run multiple samples against a predeclared threshold and report both failure rates
  (e.g. `baseline 9/20 violations → fixed 1/20; gate: ≤10%`);
- include a negative control where behavior must not change;
- score deterministically (schema/rule-based) where possible.

A single-sample RED/GREEN pair is sufficient only when the assertion is deterministic
under the pinned settings, and the report states that determinism.
