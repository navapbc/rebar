# Mutation testing

Mutation testing checks whether tests constrain behavior rather than merely execute code.
`mutmut` makes small source changes; tests provide useful evidence when they detect those
changes. The policy is phased so coverage can become blocking without hiding existing debt or
turning historical measurements into permanent thresholds.

## Manifest authority

`.github/mutation-shards.toml` is authoritative for every shard's source, selected tests,
support inputs, enforcement mode, reviewed equivalent fingerprints, and timeout policy. The
driver validates these inputs and generates the effective tool configuration; do not duplicate
them in `pyproject.toml` or operator documentation. Changes to global support inputs select all
shards because they can affect every mapping or execution environment.

## Enforcement modes

The manifest assigns one of the following modes to each shard. A configuration failure or
execution failure fails the job in every mode; modes change the comparison policy, not whether
the driver itself must complete successfully.

### Advisory

Advisory is a head-only run. `survived`, `no tests`, and `timeout` results remain visible in
head counts, raw evidence, and survivor fingerprints, but they are non-blocking and are not
serialized as comparison findings. In particular, a parsed survivor without a reviewed
fingerprint remains non-blocking in this mode.

Advisory still blocks on zero mutants, missing evidence, a non-decisive result, an unrecognized
status, a configuration failure, or an execution failure. These conditions mean the run did not
produce trustworthy mutation evidence; they are not quality debt that an advisory rollout may
silently accept.

### Ratchet

Ratchet collects fresh base and head evidence. Only a legacy survivor or legacy `no tests`
result on an AST-identical mutation unit is grandfathered. A new `no tests` result is advisory,
and the per-shard summary serializes it in `advisories`. Every head timeout blocks. A new
survivor, incomplete evidence, a killed regression, or a missing killed mutant blocks.

The only new-survivor exception is a reviewed equivalent fingerprint. That exception remains
subject to the killed-regression invariant: on an AST-identical unit, a mutant killed on base
must still be present and killed on head. A reviewed fingerprint cannot excuse that regression.

### Strict

Strict requires every result to be killed, with only one survivor exception: `survived` is
accepted only when it has a reviewed equivalent fingerprint. `no tests`, `timeout`, incomplete
or otherwise non-decisive evidence, and zero mutants all block.

The base/head invariant still applies to AST-identical units. A mutant killed on base must
remain killed on head even if its head survivor has a reviewed equivalent fingerprint; the
fingerprint does not override regression evidence.

## Promotion evidence

Promotion is decided per candidate shard. Before moving a shard out of advisory, require three
retained successful fresh base/head pilots, each with its summary and artifact. For each
candidate shard, repeatedly run CI against an unmerged patch that temporarily changes its mode
to ratchet, or otherwise explicitly invokes fresh base/head execution under the candidate
policy. Ordinary advisory head-only runs do not qualify. The pilots must demonstrate stable
mapping from the shard's source, tests, and support inputs to its results, with a maximum runtime
of 24 minutes inside the 30-minute job.

Narrow the candidate shard otherwise, then repeat the pilots. Promote to ratchet only with that
evidence. Promote to strict after mutation debt is resolved or each remaining equivalent is
individually reviewed and recorded.

## Selection and CI cadence

`scripts/mutation_gate.py select` emits selector JSON for targeted selection; `--all-shards`
selects every manifest shard. The diff parser includes both sides of renames and includes
deletions. An empty selection is an explicit successful skip, while unresolved refs or a failed
diff fail selection. Unmatched Python tests produce an advisory warning so mapping gaps are
visible.

The Gerrit lane checks out the exact Gerrit patchset. Push/PR parity comes from the same reusable
workflow used by the branch and pull-request lane. The independent weekly and manual sweep uses
`--all-shards`, and its concurrency setting cancels stale all-shard runs.

The matrix creates one 30-minute job per shard. Each job runs with no secrets declared,
inherited, or forwarded from callers or the repository on a GitHub-hosted `ubuntu-latest`
runner. GitHub's automatic token exists only as platform-provided read-only checkout context.
Current mutation evidence is therefore Ubuntu-only; Windows and macOS portability is not
established.

## Fresh execution and evidence

Advisory extracts and runs head only. Ratchet and strict extract base and head into separate
fresh archives, and there is no verdict cache. Dependency downloads may be cached, but mutation
state is not restored. Each executed side must pass its selected clean-test preflight before
mutation begins. The driver uses manifest-generated configuration, Python 3.12, the locked
`mutmut==3.7.0`, and the `--max-children 1` concurrency contract. Timeout values remain solely
in the authoritative manifest.

The driver writes `mutation-results/summary.json` with counts, fingerprints, failures, and
advisories. Available raw artifacts contain clean-test and `mutmut` raw result output, survivor
diffs, and diagnostic output. Artifact upload uses `if: always()` and is attempted when a shard
matrix job runs, even when its driver step fails. This does not guarantee an artifact: evidence
can be absent after a hard timeout, cancellation, selector failure, empty selection, or any
termination before matrix creation. Current mutation evidence runs on the GitHub-hosted
`ubuntu-latest` environment; Windows and macOS support is not established by these artifacts.

## False positives and nondeterminism

A failing comparison can trigger a diagnostic rerun of head's non-killed mutants. The diagnostic
rerun cannot turn red into green; it only labels changed outcomes as nondeterminism and adds
evidence for investigation.

Equivalent fingerprints are individually reviewed. Fingerprint normalization is
location-insensitive so harmless line movement does not invalidate a decision, but it remains
mutation-sensitive so a different normalized mutation diff produces a different fingerprint.
Diagnostics and fingerprints narrow investigation; neither may erase a killed regression or
incomplete run.

## Local use

Run the same driver from the repository environment. **The gate wraps its test
subprocesses in an OS sandbox and aborts if none is available** — install
`bubblewrap` on Linux (`sudo apt-get install -y bubblewrap`); macOS needs nothing,
`sandbox-exec` ships with the OS. `REBAR_MUTATION_ALLOW_UNSANDBOXED=1` overrides the
abort and logs a WARNING, but a mutant that reaches a destructive code path can then
delete real files — see `docs/local-dev-env.md` before using it.


```sh
uv run --locked python scripts/mutation_gate.py select --base HEAD^ --head HEAD
uv run --locked python scripts/mutation_gate.py run --base HEAD^ --head HEAD
uv run --locked python scripts/mutation_gate.py run --base HEAD^ --head HEAD --all-shards
uv run --locked python scripts/mutation_gate.py smoke --shard compact-policy \
  --base HEAD^ --head HEAD
```

The default `run` reproduces targeted selection; the all-shard command reproduces the scheduled
sweep. Inspect `mutation-results/summary.json` first, then the shard's raw evidence and survivor
diffs.

## What the sandbox does and does not restrict

The sandbox exists to stop a mutant DESTROYING things, and its scope is deliberately
that and no wider. Recorded here so the residual surface is a decision rather than an
accident:

- **Filesystem writes are denied** outside an explicit allow-list (the scratch tree and
  the pytest basetemp). The venv is deliberately excluded, so a mutant cannot drop
  executable code into site-packages for a later un-sandboxed phase to import.
- **Reads are NOT restricted.** A mutant can read anything the invoking user can.
- **Network is NOT restricted.** The macOS profile is `(allow default)` with
  `(deny file-write*)`, so a mutant could open a socket. The suite's own network guard,
  not the sandbox, is what constrains that.

Write-denial was the 2026-08-26 hazard and is what the sandbox is scoped to. Closing the
read and network surface would need a different profile shape on both platforms and is
NOT claimed today — treat mutation testing as running code that can read your files and
reach the network, and do not run it with credentials in the environment you would not
give a test.

## Emergency recovery

If mutation infrastructure freezes the submit path, an operator must follow
`infra/runbooks/two-vote-gate-rollback.md`. The two-vote runbook temporarily backs out the
`Verified` submit requirement through the reviewed Gerrit configuration path while retaining the
independent `LLM-Review` vote. It is an operator recovery procedure, not a contributor bypass;
restore the two-vote gate after the CI path is repaired and proven end to end.
