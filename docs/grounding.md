# The code-grounding oracle

rebar ships a **code-grounding oracle** (`rebar.grounding`, epic 8f6c): a pure
**evidence** oracle that grounds review findings in the actual code. It answers
three deterministic questions about a repository and returns a normalized,
three-valued evidence record for each — and it **never decides** block/advisory.
That policy lives in the consuming code (the DET floor `5fd2`, the Pass-2
reviewers `9da1`); the oracle only supplies evidence.

The cardinal property is **fail-open**: an unsupported language, a missing tool, a
crash, a timeout, or a version skew becomes a recorded `abstain` (with a CLOSED
structured reason), **never** a false accusation. The refutation lanes are
*confirm-only*: they DISPROVE an asserted absence (`refuted`) or `abstain`; they
never assert that something is absent. So the oracle only ever *reduces* false
positives — it never manufactures one.

## The three query surfaces

The public API is a thin facade, `rebar.grounding.oracle` (re-exported from
`rebar.grounding`), with one method per oracle job. Every method returns the
normalized evidence model + coverage (see *Evidence + fail-open* below).

### 1. `refute_absence(reference, *, repo_root) -> evidence` — job 1, refutation

Tries to DISPROVE an asserted-absent **reference**. The facade routes by the
reference's `kind`:

* `kind=dependency` → the **T0 deps lane** (`deps.refute_package`): a
  registry-existence probe (deps.dev) wrapped in an *abstain gauntlet*
  (stdlib/builtin, workspace/monorepo member, import-vs-distribution mismatch,
  and transient/offline guards) that runs before and around the network probe, so
  an internal, stdlib, or aliased package is never called absent.
* any other kind (`symbol` / `import` / `file` / `member`) → the **T1 ctags
  lane** (`resolve.refute_absence`): a universal-ctags repo-wide tags index plus
  plain file-path existence. A unique, bare, non-member name (or an existing file
  path) → `refuted`; a name with >1 definition → `abstain(ambiguous)`; a dotted
  member reference → `abstain` (member binding is T2); not found → `abstain`.

This is the **unification**: the standalone T1 resolver abstains-and-routes for
`dependency`; the facade makes the deps call actually happen, so a consumer needs
**one** entry point for every kind.

> **T2 (semantic resolution).** The member/dotted `abstain` above is the
> **abstain-by-default T2 seam**. Epic `850f` fills it with a self-contained,
> opt-in, confirm-only semantic backend (v1: one-shot `pyright --outputjson`
> diagnostics for Python) that escalates *from this facade* — a trustworthy
> `refuted@T2` replaces the T1 abstain, and every flaky/missing/timeout case stays
> an `abstain`. T2 never asserts an absence and never sits on a deterministic
> blocking path. See [ADR 0030](adr/0030-code-grounding-t2-semantic-resolution.md)
> for the backend selection + the confirm-only mapping.

### 2. `applies(dimension, repo_root) -> evidence` — job 2, applicability

Decides whether an applicability **dimension** applies to the repo by running the
job-2 applicability detectors that declare it. Returns a `match` (the dimension
applies) if any such detector fires, else an `abstain` carrying coverage (a
visible no-match, never a silent no-op). `dimension` must be in the **closed
dimension-ID vocabulary** (below) — an unknown dimension is a malformed request
and returns `abstain(invalid_detector)`.

### 3. `scan(repo_root, *, detectors=None, dimensions=None, path_globs=None) -> [evidence]` — job 3, smell/metric

Runs every applicable smell/metric detector over the repo (Engine B) and returns
the list of evidence records — matches **and** fail-open skips, so the list is the
complete, self-describing account of what ran and what did not. The filters narrow
the result (`None` = all applicable):

* `detectors` — keep only records from these detector ids;
* `dimensions` — keep only records from detectors declaring one of these
  dimensions (each drawn from the closed vocabulary);
* `path_globs` — keep only records whose `location.file` matches a glob (a
  coverage-only skip has no file location and is never filtered out).

## The consumer integration contract

### The closed dimension-ID vocabulary (owned here, versioned)

`rebar.grounding.oracle.DIMENSIONS` is the **single source of truth** for the
applicability/overlay dimension IDs a consumer passes to `applies()` and a
detector declares in its envelope. It is versioned by `DIMENSIONS_VERSION` (bump
on any add/remove). The detector registry (`detectors/registry.py`) imports it, so
a project detector whose `dimension` is outside the set is flagged
(`Detector.unknown_dimension`) rather than silently accepted.

Current set (v1): `web_frontend`, `has_iac`, `touches_auth`, `has_migrations`,
`has_tests`, `smell_generic`.

### The reference-in schema (defined in S2, exposed here)

The reference-in contract is `{kind, name, in_file?, container?, language?,
ecosystem?}` where `kind ∈ {symbol, import, dependency, file, member}` (closed).
It is **defined and validated** by `resolve.validate_reference` (story S2); the
oracle only **exposes** it (re-exporting `REFERENCE_KINDS`).

### Discovering the contract — `grounding-info`

The static, repo-independent integration contract is surfaced as a fast read tool
(library `rebar.grounding_info()`, CLI `rebar grounding-info [--output json]`, MCP
`grounding_info`). It returns: the closed dimension vocabulary + `dimensions_version`,
the reference kinds, the closed abstain-reason enum (+ outcome/job/tier
vocabularies), and the available backends with their **detected** availability and
version. Its shape is pinned by `src/rebar/schemas/grounding_info.schema.json`
(registered in `OUTPUT_SCHEMAS`). This is exactly what `5fd2`/`9da1` use to
discover the vocabulary they must draw from.

## Evidence + fail-open

Every probe returns ONE evidence record (`rebar.grounding.evidence`,
`grounding.schema.json` is canonical). It is **three-valued**: `refuted` / `match`
(resolved) or `abstain`. Match and abstain share ONE shape — a skipped backend
uses the same record with `outcome=abstain` + `coverage.status=skipped`, so the
visible skip **is** the coverage record.

The **CLOSED reason enum** (no open `…`): `unsupported_lang`, `no_tool`,
`parse_error`, `timeout`, `ambiguous`, `private_or_internal_suspected`,
`network_error`, `rate_limited`, `version_skew`, `invalid_detector`, and the
explicit catch-all `other`. `version_skew` (the #1 real failure) and
`invalid_detector` (a project detector failing validation) are first-class.

**Coverage semantics.** Each record carries a `coverage` record
(`{backend, status, version?, reason?}`). `status=ran` records what executed (with
the tool version, so version skew is visible); `status=skipped` records what did
NOT run and **why** (the closed reason). A scan's record list is therefore the
complete account: matches plus every skip's coverage.

### Worker-process callable contract

`rebar.grounding.harness.run_in_worker` isolates in-process bindings in a
subprocess and uses Python's `spawn` multiprocessing context by default. The
callable must therefore be defined at module scope and importable by the child;
the positional arguments, keyword arguments, and result must all be pickleable.
Pass plain data into the worker and construct C-extension or other process-local
state inside the child instead of attempting to serialize that state.

Advanced callers may pass an explicit multiprocessing context with
`mp_context=...`. This includes a `fork` context on platforms that provide it,
but the caller then owns fork's thread-safety and inherited-state risks. Rebar
does not select `fork` implicitly. Context setup, process start/serialization,
and result receive/unpickling failures all preserve the oracle's fail-open
contract by returning `abstain(other)` rather than raising.

## Detectors

### The detector envelope format

A *detector* is a thin rebar envelope riding on a **verbatim native matcher
payload** (the Trivy model): the file IS a valid OpenGrep/semgrep rule YAML (or an
ast-grep rule), and rebar's metadata lives in `metadata.rebar_envelope`, preserved
untouched by the engine. The envelope carries:

* `tier` (`T0`/`T1`/`T2`), `job` (`refute`/`applies`/`smell`), `namespace`;
* `dimension` (from the closed vocabulary), `attention_only` (routes attention,
  does not assert a defect);
* `thresholds` (metric cutoffs, e.g. `oversize_loc` / `max_complexity`);
* `backend` (`opengrep` / `ast-grep` / `metric`) when not inferable.

Three backends run detectors and normalize every match (or fail-open skip) to the
evidence model: **OpenGrep** (primary; pre-validated `--validate` then `scan
--sarif`), **ast-grep** (structural secondary; `scan --json`), and **metric**
(`scc`/`lizard`; size/complexity with configurable thresholds).

### The `.rebar/detectors/` convention

Detectors are discovered from two sources, unioned at load (project last-wins, so
a project file transparently overrides a built-in of the same id):

1. **Built-in** detectors shipped under `detectors/builtin/`.
2. **Project-local** detectors under `<repo>/.rebar/detectors/`.

An absent project dir is not an error (fail-open). The registry is process-local,
built-once and **mtime-cached** per detector-dir signature, so concurrent scans
share one immutable snapshot and a detector-dir change rebuilds on the next load.

### Loader pre-validate / quarantine

The registry only catalogs *parseable-as-YAML* detectors — a file that is not even
YAML is dropped at parse with a recorded `parse_error` note. A
structurally-bad-but-YAML rule survives to be **quarantined engine-faithfully** by
the evaluator: OpenGrep `--validate` (which needs no target) is run per detector
first, and a schema-invalid rule is dropped as `invalid_detector` so the scan
never aborts on one bad rule (the engine would otherwise exit nonzero on the whole
run). ast-grep validates a rule as part of `scan -r`; a parse complaint is its
per-backend `invalid_detector` signal.

## T2 semantic resolution (opt-in) — the pyright backend

The refutation lane's member/dotted `abstain` is the **T2 seam** (see the callout
under surface 1). Epic `850f` (ADR
[0030](adr/0030-code-grounding-t2-semantic-resolution.md)) fills it with a
self-contained, opt-in, **confirm-only** semantic backend. v1 ships one backend:
**one-shot `pyright --outputjson` diagnostics for Python.**

**How it resolves.** When enabled, `oracle.refute_absence` escalates a T1 `abstain`
on a member/dotted (or not-found bare symbol/import) reference to
`grounding.semantic.refute_semantic`, which dispatches to the pyright backend. The
backend runs pyright once over the project root (so cross-module imports resolve)
and decides, for a reference in file `F`:

* **`refuted` at `T2`** iff pyright ran, its JSON parsed, `F` has **no**
  import-resolution diagnostic (`reportMissingImports` / `reportMissingModuleSource`
  — the "environment built" precondition), and **no** diagnostic in `F` names the
  reference's leaf. A trustworthy semantic confirmation replaces the T1 abstain.
* **`abstain` at `T2`** (closed reason) otherwise: `no_tool` (pyright absent),
  `unsupported_lang` (not Python), `ambiguous` (no locatable file), `parse_error`,
  `timeout`, or `other` (env-not-built / a diagnostic sits at the reference / an
  unrecognized diagnostic at the reference).

**Confirm-only.** T2 can only ever *upgrade* a T1 abstain to a `refuted`; it never
downgrades a resolved record and **never asserts an absence** — a pyright diagnostic
saying the reference does not resolve becomes an `abstain` (a suspected-absent), so
T2 is never on a deterministic blocking path. Every flaky/missing/timeout case is an
`abstain`, never a false accusation.

**Enabling it.** Off by default. Install the extra and opt in:

```toml
# .rebar/grounding.toml
[grounding]
t2_enabled = true          # master opt-in (default false)
t2_backend = "pyright"     # the selected backend (default null)
t2_timeout_seconds = 30    # bounded per-invocation subprocess timeout (default 30)
```

```sh
pip install 'nava-rebar[grounding-t2]'   # pulls pyright; absent it, T2 abstains(no_tool)
```

With `t2_enabled = false` (the default) or the extra uninstalled, no T2 code path
runs and the oracle is byte-identical to the T0+T1 floor. `grounding-info` reports
the pyright backend with its detected availability/version.

## The `.rebar/` language-extensibility slot + thresholds

The oracle is **polyglot-extensible without a recompile**, via the `.rebar/` slot:

* **ctags optlib** (the T1 refute lane) — `.rebar/grounding.toml`
  (`[grounding] ctags_optlib_dirs`, `ctags_options`, `supported_languages`) threads
  project ctags `--optlib-dir` / `--options` through, so a custom `--langdef` regex
  grammar indexes an otherwise-unsupported language. A language listed in
  `supported_languages` is treated as resolvable even if the stock ctags build does
  not know it.
* **ast-grep customLanguages** (the structural backend) — a project
  `.rebar/sgconfig.yml` (or a path declared in `.rebar/grounding.toml` under
  `[grounding] astgrep_sgconfig`) registers a tree-sitter custom grammar; its
  `customLanguages` extensions also let a custom-language detector route as
  applicable. An unconfigured language fails open (skipped + coverage).
* **Configurable metric thresholds** — a metric detector's envelope carries
  `thresholds` (e.g. `oversize_loc`, `max_complexity`) with shipped defaults, and a
  project detector under `.rebar/detectors/` overrides them.

A missing or malformed slot simply means no extensibility — never a raise.

## Entry points (read-only)

| interface | refutation | applicability | smell scan | static contract |
|-----------|------------|---------------|------------|-----------------|
| library   | `rebar.grounding.refute_absence(ref, repo_root=…)` | `rebar.grounding.applies(dim, repo_root)` | `rebar.grounding.scan(repo_root, …)` | `rebar.grounding_info()` |
| CLI       | — | — | — | `rebar grounding-info [--output json]` |
| MCP       | — | — | — | `grounding_info` |

The three query surfaces are a **library** API (the oracle's consumers call them
in-process). The static integration contract — the discovery surface — is exposed
across all three interfaces as the typed `grounding_info` read tool, mirroring
rebar's other read tools (a canonical `.schema.json` registered in
`OUTPUT_SCHEMAS`, validated across CLI/library/MCP in CI).

## Terraform structural grounding (opt-in: the `grounding-terraform` extra)

A separate, opt-in surface (`rebar.grounding.terraform_tools`, epic
`a374-849c-c8f2-4234`) grounds the plan-review infra/IaC overlay (`T10`) in real
Terraform structure. Like the oracle above it is **refutation-only** and
**fail-open**, but it is a per-agent-call **session** rather than a stateless query,
and it parses **`.tf`/`.tf.json`** with the pinned pure-Python `python-hcl2==8.1.3`
parser — **in process, never** shelling out to `terraform`/`opentofu`/a provider/
`tfparse`/`tflint`/`trivy`/`terraform-ls`/`terraform-docs` (ADR 0115). The only
subprocess is the shared grounding worker (`run_in_worker`, 60 s) that runs the pure
parse fail-open.

### Install & routing

```
pip install 'rebar[grounding-terraform]'   # or: uv sync --extra grounding-terraform
```

`hcl2`/`lark` import **lazily** (inside the worker only), so `import rebar` and every
non-Terraform review stay parser-free. The tools are routed **only** to the
Terraform-scoped criterion `T10`; no other criterion sees them. A Terraform-scoped
Pass-1 finding drives the **agentic** Pass-2 branch, whose verifier issues its **own**
structural query (it may reuse the immutable parse cache but never accepts a Pass-1
receipt as verification). When the extra is absent, `available()` is `False` and every
query returns a closed `no_tool`/`missing_extra` abstention — never a raise.

### The session API

* `open_session(repo_root, selected) -> TerraformSession` — builds a bounded, frozen
  snapshot over the `selected` `.tf` seeds (following literal in-repo child `module`
  `source`s forward and discovering in-repo reverse callers), owning an immutable parse
  cache + a query ledger.
* `TerraformSession.lookup_declaration(address, module_path="")` — refute an asserted
  ABSENCE of a declaration by canonical address (`variable.region`, `aws_instance.web`,
  `data.aws_ami.base`, `module.vpc`, …).
* `TerraformSession.resolve_reference(reference, from_file)` — refute an asserted
  ABSENCE of a referenced member/output (`var.region`, `module.vpc.vpc_id`, …).
* `TerraformSession.finalize() -> Usage` — free the cache/ledger and report
  `concrete_reads` (the `.tf`/`.tf.json` actually read) + `membership_globs` (e.g.
  `infra/**/*.tf`) for the signed read-set.

Each query returns a `Result` with `.evidence` (a grounding record — `refuted` or
`abstain`, **never** `match`, **never** an asserted absence; validated against the
`GROUNDING` schema) and `.receipt` (the canonical, credential-redacting receipt,
validated against `TERRAFORM_GROUNDING_RECEIPT`). All digests are `sha256:`-prefixed;
attribute literals and `default` values are redacted.

### Limits

`terraform_index.LIMITS` bounds a snapshot; over any bound the build raises
`TerraformLimitError` (with `.detail`) and yields **no partial snapshot**:

| bound | default | `.detail` |
|-------|---------|-----------|
| modules | 64 | `module_limit` |
| files | 5000 | `file_limit` |
| bytes | 33554432 | `byte_limit` |
| timeout_ms | 60000 | (worker boundary) |

An absolute/out-of-repo `selected` path or an escaping symlink raises
`TerraformPathError`.

### Outcomes & abstention reasons

Every non-refuted query abstains with a CLOSED `(evidence.reason / receipt.reason_detail)`
pair:

| situation | `evidence.reason` / `receipt.reason_detail` |
|-----------|---------------------------------------------|
| missing extra | `no_tool` / `missing_extra` |
| wrong parser version | `version_skew` / `parser_version` |
| non-Terraform call | `unsupported_lang` / `not_terraform` |
| invalid/undecodable input, or worker parse_error | `parse_error` / `invalid_input` |
| unreadable capture | `parse_error` / `unreadable_file` |
| worker timeout | `timeout` / `worker_timeout` |
| worker other failure | `other` / `worker_failure` |
| duplicate address | `ambiguous` / `duplicate_address` |
| no unique hit | `ambiguous` / `no_unique_address` |
| dynamic `source`/expression | `ambiguous` / `dynamic_source` \| `dynamic_expression` |
| computed value | `ambiguous` / `computed_value` |
| provider attribute | `ambiguous` / `provider_attribute` |
| splat/index | `ambiguous` / `splat_index` |
| unknown tfvars | `ambiguous` / `unknown_tfvars` |
| path outside snapshot (abs/out-of-repo/escaping symlink) | `private_or_internal_suspected` / `path_outside_snapshot` |
| module/file/byte bound | `other` / `module_limit` \| `file_limit` \| `byte_limit` |
