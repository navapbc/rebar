# Mutation testing

Mutation testing checks whether tests constrain behavior rather than merely execute code.
mutmut makes small source changes; a useful test suite kills those mutants by failing.

## Source of truth

`.github/mutation-shards.toml` is authoritative for protected source modules, selected tests,
support inputs, score/count ceilings, and accepted equivalent-mutant fingerprints. Do not copy
those mappings or numbers into this document or `pyproject.toml`. `scripts/mutation_gate.py`
generates the effective mutmut configuration for each run.

The runtime contract is Python 3.12 and `mutmut==3.7.0`, locked in `uv.lock`. Mutation runs use
one child, a 30-minute job limit, and mutmut's `(clean-test duration + 1 second) x 15` per-mutant
timeout. The selected clean tests must pass before mutation starts.

## CI cadence

The Gerrit `Verified` workflow compares `HEAD^` with the exact patchset and runs only shards
selected by changed source, tests, or support inputs. Changes to global mutation infrastructure
select every shard. Renames inspect both old and new paths. An unresolved ref or diff fails; an
empty selection is an explicit successful skip. Changed Python tests that map to no protected
shard produce an advisory so mapping gaps stay visible without making all tests mutation-gated.

The push/PR mirror invokes the same reusable workflow. Its recurring main-health run skips the
targeted check because `.github/workflows/mutation.yml` performs the broad all-shard sweep weekly
and on manual request. Newer runs cancel older runs.

This split keeps the merge gate proportional to the change while the broad sweep detects stale
mappings, tool drift, and cross-shard assumptions.

## Comparison and accepted outcomes

Base and head run from separate git archives. Mutation verdict state is never restored; only
dependency downloads may be cached. This avoids treating a stale mutmut result as evidence.

Comparison is per mutation unit (module plus qualified function):

- When the unit's AST is identical, every mutant killed on base must still exist and be killed
  on head. This catches weakened tests even in a mixed source/test patch.
- Added, removed, renamed, or edited units are reported as source changes. Mutant IDs can move
  after a legitimate edit, so these units use the head shard budgets instead of an exact-ID
  comparison.
- A surviving mutant is accepted only when its normalized `mutmut show` diff matches a
  checked-in fingerprint and the shard remains within its survivor ceiling.
- `no tests` has a separate ceiling. Rebar has interface tests that exercise behavior through a
  subprocess, which mutmut's in-process coverage map cannot attribute. The mapped score is
  therefore `killed / (killed + survived + timeout)` while unattributed mutants remain visible
  and bounded.
- Timeouts have a zero ceiling. Zero parsed mutants, unknown fingerprints, unknown or incomplete
  statuses, and any exceeded ceiling fail closed.

## Flakiness and false positives

Only an independently identified setup operation may retry. Mutation failures are not retried
into a pass. When head has a non-killed result, the driver reruns it once for diagnosis; a changed
outcome is labeled nondeterministic, but the gate remains red. This preserves evidence without
letting a lucky rerun merge a regression.

Equivalent mutants are handled narrowly with normalized diff fingerprints rather than by
ignoring a file or lowering its whole score. Location headers are excluded so line movement does
not invalidate the fingerprint; the actual mutation remains part of it.

Every driver outcome writes `mutation-results/summary.json`. Clean-test logs, raw mutmut output,
parsed results, and diagnostic output are uploaded when available.

## Updating a shard

A relaxed budget must accompany the affected source or mapping, measured base/head results, and
a rationale explaining why the additional outcome is not a behavioral gap. A budget-only
relaxation is invalid. Add an equivalent fingerprint only after inspecting the exact normalized
diff and proving that no observable contract can distinguish it. Never lower a threshold merely
to make CI pass.

When adding a behavioral test for protected code, add it to that shard. A new production core
needs its own focused shard and measured baseline. Keep source selection narrow enough that the
blocking run remains bounded.

## Local use

From the repository virtualenv:

```sh
python scripts/mutation_gate.py run --base HEAD^ --head HEAD
python scripts/mutation_gate.py run --base HEAD^ --head HEAD --all-shards
python scripts/mutation_gate.py smoke --shard compact-policy --base HEAD^ --head HEAD
```

Use the first command to reproduce the merge gate and the second to reproduce the scheduled
sweep. Inspect `mutation-results/summary.json` before the raw logs.

If the gate itself blocks all changes, follow `infra/runbooks/two-vote-gate-rollback.md`. The
rollback is an operator recovery path, not a contributor bypass.
