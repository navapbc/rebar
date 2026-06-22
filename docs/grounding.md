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
