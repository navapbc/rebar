# ADR 0098 — Operation-scoped configuration and provider bindings

**Status:** Proposed  
**Date:** 2026-08-17  
**Ticket:** `miniscule-unschooled-snowmonkey` / `23e9-cad1-64bf-4bb7`  
**Epic:** `vibrant-legal-hind` / `e215-ab23-ed6a-4791`  
**Amends:** [ADR 0049](0049-opcert-asymmetric.md) only at the trusted service's
application/runtime key-retrieval cadence; its algorithm, trust-root and era model,
producer protocol, and SSM-at-rest deployment choice remain authoritative.  
**Relates to:** [ADR 0008](0008-secrets-source-ssm.md),
[ADR 0014](0014-inbound-webhook-auth.md),
[ADR 0041](0041-llm-diagnostic-sanitization.md),
[ADR 0046](0046-security-posture-and-accepted-limitations.md),
[ADR 0050](0050-mcp-optional-auth-resource-server.md),
[ADR 0059](0059-llm-provider-seam-and-support-tiers.md), and
[ADR 0083](0083-reconciler-vendor-adapter-seam.md).

## Context

Rebar currently has more than one authority for effective configuration during a
single operation. The central resolver defines a layered contract, yet some helpers,
reconciler modules, and backend properties reread environment, files, cwd, or local
defaults after composition. The RP-04 probe demonstrated concrete splits: explicit
CLI `tracker.dir` lost to `REBAR_TRACKER_DIR`; malformed TOML fell back for tracker
directory while failing for branch; a constructed Jira Cloud backend changed project
when only ambient state changed; and both Jira factories ignored the `config` object
the registry passed them. The audited reconciler also contained 19 ambient
`load_config(` sites and seven hard-coded `.tickets-tracker` path constructions.
These findings and the current-behavior test baseline are recorded on research ticket
`relevant-liberal-xantus` and in its recovered RP-04 artifacts.

Authentication makes the split more dangerous but is not ordinary configuration.
Rebar integrates with mechanisms that have different owners and lifecycles:

- boto3/botocore discovers and refreshes AWS credentials through its native chain
  [R4];
- the official Anthropic and OpenAI SDKs expose provider/callable/workload-identity
  objects with expiry, refresh, invalidation, and concurrency behavior [R2, R3];
- Git commonly delegates acquisition, storage, and OAuth refresh to credential
  helpers or SSH agents [R5, R6]; and
- Jira, Jenkins, Gerrit, and some hosted endpoints still require static material at
  the final provider-specific adapter [R12, R13, R14].

Flattening those mechanisms into `get_secret(name) -> str` would transfer refresh,
cache, invalidation, precedence, and storage responsibilities from their authoritative
owners to Rebar. PydanticAI already accepts official OpenAI, Anthropic, and Bedrock
clients, so Rebar can preserve those native lifecycles while retaining its existing
model, retry, timeout, cache, structured-output, error, and close policies [R1]. The
maintained OSS comparison converges on the same asymmetric pattern: Git and its tools
delegate to helpers/keychains or CI environment identity; provider SDKs retain rich
auth objects; and Jira integrations terminate static or OAuth material inside a Jira
adapter. Airflow's universal secret backend is useful counter-evidence: its documented
precedence, collisions, worker-specific backend, and connection formats are complexity
appropriate to a workflow orchestrator whose domain includes secret orchestration,
not evidence that every integration host needs such a broker [R15].

The research also found a real propagation defect. `_bridge_runner` copies the broad
parent environment to the selected bridge child and later ticket-delivery child, so
an unrelated delivery child can receive Jira and LLM credentials. Conversely, a
strict application-wide allowlist would break provider-native Git/SSH/AWS/proxy/CA
chains. Python subprocesses inherit their environment by default; pip, pre-commit,
GitHub Actions, and Jenkins use inheritance with scoped overlay/removal, while tools
such as tox reserve strict environments for an explicit isolation contract [R16,
R17, R18, R19]. The defect is indiscriminate sibling propagation after Rebar has
materialized known credentials, not `os.environ.copy()` itself.

Finally, the trusted op-cert service is an inconsistent exception. ADR 0049 places
the Ed25519 key in SSM for the current deployment, while application code retrieves
and materializes it once per job and temporarily patches process-global key,
principal, and push variables. RP-04 experiments proved that concurrent or timed-out
workers can observe and remove one another's patched values; later jobs also fail if
runtime SSM becomes unavailable even when startup had a valid key. The same probes
validated file and multiline-environment startup input, an immutable process-owned
copy, context-local signer state, restart-only replacement, and cleanup without a
runtime AWS dependency. ADR 0008 already establishes deployment-time SSM
materialization before container startup, so the application-owned retrieval is not
needed.

## Decision

### 1. Compose one non-secret snapshot per operation

Every operation resolves one immutable, serializable, non-secret
`OperationSnapshot`. The precedence contract, highest to lowest, is:

1. explicit operation input;
2. environment;
3. selected project configuration;
4. user configuration; and
5. the central schema default.

Repository root is a separate decision: explicit `repo_root` > `REBAR_ROOT` > Git
top-level/current directory. Only after the winning value is selected are relative
paths interpreted against that root. Effective defaults live in the central schema;
wire and protocol constants remain adapter-owned. The snapshot and its provenance use
the same layer implementation and vocabulary.

Snapshot lifecycle is surface-specific but the invariant is shared:

- CLI composes once after parsing and before lock, network, or write.
- MCP composes once per operation/tool call. Invalid operation input fails that call,
  not the server process.
- Python composes once per public operation or accepts an explicitly supplied
  snapshot as authoritative.
- The direct reconciler module may retain a compatibility entry point, but it composes
  through this contract exactly once.
- Long-running services additionally own startup deployment policy and live runtime
  bindings. They do not create a redundant long-lived bootstrap operation snapshot.

An in-progress operation never rereads environment, configuration files, cwd, or
process-global configuration. A new operation may observe changed non-secret inputs;
an existing snapshot is never mutated or partly reloaded.

### 2. Compose live capabilities beside, never inside, the snapshot

The trusted application boundary combines the snapshot with non-serializable runtime
bindings to form an operation context:

```text
OperationSnapshot                 RuntimeBindings
(immutable, serializable,         (live capabilities; repr-safe,
 non-secret)                       unhashable, non-serializable)
          |                                  |
          +----------- OperationContext -----+
                               |
                 +-------------+-------------+
                 |             |             |
             GitRuntime     LLMRuntime    BridgeRuntime
```

Only required capabilities are constructed. Ticket operations need Git; an LLM gate
needs Git plus LLM; reconciliation needs Git plus the one selected bridge. Each
consumer receives a focused immutable projection rather than the application-wide
snapshot or environment. Snapshot fingerprints may include behavior-bearing policy
such as selected provider/model, retry, timeout, output, and cache settings. They
exclude source paths, credentials, clients, provider capability objects, subprocess
environments, and other live capabilities.

The snapshot and durable diagnostics may record non-secret selection/provenance such
as provider, endpoint/host, region, logical profile name, expected account/project,
and a redacted auth-source kind. They never record tokens, passwords, authorization
headers, private-key contents, credential-file contents, SDK/provider objects, or a
subprocess environment. A non-secret path/profile reference may appear only where an
operation's policy requires it; because it can still grant access indirectly, it is
excluded from fingerprints and unrelated children and is dereferenced only by the
owning composition/binding seam.

### 3. Preserve provider-native authentication ownership

The upstream SDK, Git helper/SSH agent, CI runtime, deployment, or embedding host owns
credential issuance, retrieval, storage, refresh, expiry, invalidation, and its native
precedence. Rebar owns composition across its seams:

- choose the configured provider/backend once;
- accept that provider's strongest native pre-client credential, identity, or session
  capability at a trusted Python/startup seam;
- construct and close the official client inside a Rebar-owned factory;
- retain Rebar's transport, retry, timeout, cache, output, error, and lifecycle policy;
- instantiate only the selected integration; and
- fail closed if an explicit provider/profile/project/auth selection cannot be
  composed, without trying anonymous access, another provider, or another ambient
  principal.

Examples include a boto3 `Session`, Anthropic `AccessTokenProvider`, OpenAI callable
key/provider runtime/workload identity, or a provider-specific static Jira value [R1,
R2, R3, R4]. Provider SDKs differ, so this is an ownership rule, not a universal
credential protocol or closed credential union. The initial public seam does not
accept arbitrary fully constructed SDK clients, Git runners, or bridge runners: those
could bypass the Rebar policies above. A future provider that cannot preserve rich
authentication without whole-client injection requires a separate evidence-backed
decision.

MCP tool inputs and CLI arguments never accept raw credentials, credential-file
contents, secret-manager paths, or caller-selected stronger server identities.
Server/startup policy owns the available bindings. If an authoritative provider
exposes safe identity metadata, a binding may validate configured non-secret
host/account/project expectations; RP-04 does not require token introspection or an
extra identity-discovery network call for every provider.

### 4. Keep unavoidable static material provider-specific and narrow

Where no richer native seam exists, use a provider-specific, non-serializable static
auth container backed by Pydantic `SecretStr` or `SecretBytes`. Pydantic masks normal
string/repr and default JSON rendering, but requires explicit unwrapping and does not
encrypt memory or prevent deliberate disclosure [R20]. RP-04 therefore also excludes
the secret field from value equality, hashing, snapshots, fingerprints, caches,
provenance, and serialization. The sending adapter unwraps it at the last responsible
moment. Behavioral canaries—not wrapper semantics alone—verify logs, exceptions,
outputs, argv, URL/query strings, subprocesses, and CLI/MCP schemas.

There is no core `SecretProvider`, universal credential union, automatic keyring
fallback, or Rebar-owned SSM/Secrets Manager/Vault/1Password retrieval. Deployment or
the embedding host may use any of those systems and inject an environment value, file,
or native provider capability. A later optional acquisition adapter requires concrete
demand and a separate decision covering bootstrap identity, precedence, cache,
rotation, errors, and supply-chain risk.

### 5. Inherit ambient capability; remove exact known sibling secrets

Trusted same-capability child processes inherit the ambient environment so Git
credential helpers, SSH agents, AWS chains, proxies, enterprise CAs, and platform
tooling continue to work. Rebar does not mutate global `os.environ` to install
adapter credentials.

Every adapter declares the exact secret environment names it owns. A child-environment
builder:

1. starts from ambient inheritance;
2. removes exact secret names owned by unrelated Rebar adapters;
3. overlays a Rebar-materialized static value only for its owning child; and
4. preserves unknown variables and native helper chains.

Deployment step/container/service scoping remains the primary isolation boundary.
This rule prevents accidental propagation of Rebar-known material to unrelated
siblings; it is not a process sandbox, cannot identify unknown third-party secret
names, and does not protect against every malicious same-user peer.

### 6. Make static rotation a restart/redeploy contract

A short-lived process sees static material supplied at startup. A long-running MCP,
review-bot, reconciler, or hosted service retains its startup static bindings until
restart/redeploy. This matches ECS Parameter Store injection, which requires a new
task or forced deployment to receive an updated secret [R8]. If the old credential is
revoked first, operations fail authentication; they do not fall through to another
identity. SDK-native temporary/workload credentials are different: their official
provider continues refreshing them in process without a Rebar watcher or cache.

Authentication that authorizes access or affects a Rebar decision fails closed.
Optional write-only Langfuse/OTLP telemetry is the deliberate exception: missing,
partial, or rejected observability credentials disable tracing without changing the
operation's result. Langfuse and the OTLP exporter capture credentials/headers when
their client/exporter is constructed, supporting a startup binding and reconstruction
on rotation [R9, R10]. This exception does not apply to MCP, Jira, Gerrit, signing, or
other security- or decision-bearing integrations.

### 7. Move op-cert key retrieval out of the application runtime

The trusted op-cert service accepts exactly one startup source:

- `REBAR_OPCERT_KEY_PATH` (preferred), following provider-mounted symlinks and
  accepting common 0444/0644 read-only secret-volume source modes; or
- multiline `REBAR_OPCERT_PRIVATE_KEY`.

Missing, ambiguous, unreadable, non-regular, or invalid input fails before queue,
worker, network, or workspace creation. Startup copies the validated key into a
process-owned 0700 runtime directory and 0600 file because the existing SSHSIG path
requires a file. The process holds one immutable, unhashable, context-local binding
for signer, principal, and push policy. Jobs receive that binding without modifying
`REBAR_OPCERT_KEY_PATH`, `REBAR_OPCERT_ENV_ID`, `REBAR_SYNC_PUSH`, or any other global
environment state. Graceful shutdown deletes only Rebar's copy, never the deployment
source; the hosted deployment backs the runtime directory with ephemeral tmpfs so a
kill/restart cannot retain the copy.

The application removes `REBAR_OPCERT_SSM_KEY_PARAM`, `SsmKeyFetcher`, runtime boto3
and region requirements, and per-job provisioning. Deployment may retain ADR 0049's
SSM SecureString as the at-rest source and ADR 0008's fail-fast boot materialization,
then mount/inject the resolved source before the Rebar process starts. A running
process intentionally does not observe source replacement; restart/redeploy does.

This is the only amendment to ADR 0049. ADR 0049 remains authoritative for Ed25519 and
DSSE/SSHSIG, environment attribution, trusted-environment keys and era validity,
authoritative-state fetching, the single producer and client-persisted envelope,
deployment SSM storage, and its accepted security limitations.

## Explicit behavior changes

Implementation must treat these as decisions with regression tests, not accidental
parity:

- explicit CLI input wins over environment for `tracker.dir`;
- malformed operation configuration fails before lock/network/write instead of
  selectively falling back;
- a supplied Python configuration/snapshot remains authoritative;
- backend settings and project properties stop changing after construction;
- configurable defaults move from adapter sentinel/fallback logic to the central
  schema and provenance;
- configured tracker directory, branch, and remote reach every Git, lock, snapshot,
  binding, compatibility, and hosted-workflow owner;
- the Jira Cloud write-side `DIG` fallback is removed in favor of explicit
  safety-critical project scope; and
- op-cert key replacement becomes visible on restart/redeploy instead of the next
  job.

Existing lessons around auth preflight, retry classification, output shape, TLS,
diagnostic sanitization, and redaction remain unless an implementation ticket names
and tests a separate deliberate change.

## Alternatives considered

### Continue ambient/per-consumer loading

Rejected. It is the source of the measured within-operation contradictions and makes
explicit Python/CLI input advisory rather than authoritative.

### Add one string-returning secret provider

Rejected. It cannot represent Git helpers/SSH agents, usernames plus tokens,
refreshable access-token providers, workload identity, 401 invalidation, boto3
sessions, or provider-owned concurrency. It would make Rebar reimplement behavior
that official SDKs already own [R2, R3, R4, R5].

### Add a universal typed credential union and refresh broker

Rejected. Stronger typing does not remove the lifecycle duplication, and the union
would grow with every provider. Making it open recreates plugin selection,
precedence, and collision problems without secret orchestration being Rebar's domain
[R15].

### Add built-in cloud-secret and keyring adapters now

Rejected. CI, deployment, and embedding hosts already inject material or native
identity. Core adapters would add bootstrap credentials, naming, caching, rotation,
and headless/backend-selection policy. Python keyring is useful when explicitly
selected by a local tool, but its pluggable backend selection and headless setup make
it unsuitable as an automatic portable fallback [R21].

### Use a strict child-environment allowlist everywhere

Rejected. It would require Rebar to predict every provider helper, agent, proxy, CA,
runtime, and platform variable. Maintained Python and CI tooling uses inherited
environments with scoped overlay/removal for ordinary trusted children [R16, R17,
R18, R19]. A future hermetic mode requires a concrete, tested capability contract.

### Install adapter secrets through global `os.environ`

Rejected. Deterministic thread/timeout probes reproduced cross-operation observation,
premature removal, and stale restoration. Explicit child environments and
context-local bindings are composable and avoid process-global races [R22].

### Expose arbitrary constructed clients/runners

Rejected for the initial seam. Although PydanticAI can consume constructed clients
[R1], exposing them at Rebar's public boundary can bypass transport, retry, timeout,
output, cache, and ownership invariants. Native pre-client auth/session injection
preserves rich authentication while keeping client construction inside Rebar.

### Keep per-job op-cert SSM retrieval or add hot reload

Rejected. Per-job retrieval couples completed startup to runtime SSM availability and
global mutation. Watchers/hot reload would give op-cert a lifecycle inconsistent with
other static bindings and would make Rebar own cloud-secret rotation. Restart/redeploy
is explicit, portable, and experimentally validated.

## Migration and rollback

Use expand/contract sequencing:

1. Add snapshot/projection and subsystem runtime-binding types plus the central
   composer in shadow mode. Emit only a redacted non-secret fingerprint/witness.
2. Prove cross-surface precedence, root selection, lifecycle, fingerprint inclusion,
   and structural exclusion of secrets/live capabilities.
3. Adapt provider factories and consumers in call-graph slices behind compatibility
   facades. Only the selected integration is constructed.
4. Thread tracker layout and policy through reconciler owners. Keep legacy composition
   callable until parity or an approved behavior delta is demonstrated.
5. Add structural ownership and generated-document drift gates.
6. Run AWS review-bot and reconciler check, dry-run, bounded-live, and E2E canaries
   before removing legacy paths.

Before write cutover, rollback disables shadow composition. During expansion,
compatibility facades may delegate to legacy composition. After cutover, rollback is
only to a compatibility floor that understands the snapshot envelope. RP-04 changes
no stored ticket, event, binding, or bridge format. Credentials are never persisted or
logged to diagnose parity.

For op-cert, deployment expands first by supplying the startup source while the
legacy per-job path remains available; startup/context-local behavior and hosted
canaries must pass before the legacy SSM fetch/global patch path is removed. Rollback
before removal selects the legacy path. After removal, rollback requires the same
deployment-injected source contract; ADR 0049's signatures and stored envelopes are
unchanged.

## Verification obligations

Implementation stories must provide behavioral oracles for:

- CLI/MCP/Python/direct-reconciler precedence, root selection, and one-operation
  stability across environment/file/cwd mutation;
- custom tracker directory/branch/remote propagation to every owner;
- supplied factory configuration remaining authoritative after composition;
- exact official client/provider/session injection without token extraction, while
  preserving retry, timeout, cache, output, and close policy;
- failure before network/write for missing or conflicting decision-bearing auth,
  with no anonymous/cross-provider/alternate-principal fallback;
- static rotation for a new CLI, unchanged long-running process, post-restart process,
  revoked old value, and native refreshable provider as distinct cases;
- canary absence from repr, equality/hash, serialization, snapshots, fingerprints,
  caches, provenance, exceptions, logs, output, argv, URL/query strings, unrelated
  siblings, and CLI/MCP schemas;
- preservation of unknown ambient variables/native helper chains, owning-child-only
  overlays, exact sibling removal, and zero global-environment mutation under
  concurrency;
- op-cert source exclusivity/validation before service work, 0700/0600 ownership,
  stable startup copy, restart visibility, graceful and kill/restart cleanup,
  context-local concurrency/timeout isolation, existing producer signatures, no
  workspace push remote, and no application runtime SSM/boto3/region dependency;
- generated precedence/lifecycle/operator documentation and structural ownership
  gates; and
- operator-attested hosted review-bot and reconciler check/dry-run/bounded-live/E2E
  canaries with redacted provenance.

Research established the current baseline and feasibility—not implementation proof.
The final RP-04 research run passed 139 focused auth/security regressions, bounded live
Anthropic and Bedrock calls through Rebar/PydanticAI, Gitleaks over the recovery
artifacts, and hosted reconciler check/dry-run canaries. Those scenarios remain
post-change obligations.

## Consequences

### Positive

- One operation has one explainable non-secret configuration authority.
- Provider SDKs/helpers retain the authentication lifecycles they are designed to
  own.
- Rebar can preserve hard-won retry, timeout, caching, output, and error behavior
  around official clients.
- Credentials and live capabilities are structurally excluded from snapshots and
  narrowed at subprocess/adapter boundaries.
- Op-cert no longer depends on SSM or process-global mutation after successful
  startup.
- Local environment, CI secret injection, and hosted provider identity use the same
  composition model without making Rebar a secret store.

### Negative

- Provider bindings are deliberately heterogeneous; each adapter needs a typed seam
  and focused lifecycle tests.
- Static credential changes require restart/redeploy of long-running processes.
- Ambient inheritance still exposes unknown parent variables to trusted children and
  cannot provide process isolation.
- Expand/contract migration touches every major surface and requires structural gates
  plus hosted canaries before legacy removal.
- Pydantic secret wrappers are defense in depth only; boundary-specific redaction and
  leakage tests remain mandatory.

### Neutral

- Deployment may continue using SSM, GitHub Secrets, Jenkins Credentials Binding,
  mounted files, a keyring, or another store; that choice remains outside Rebar core.
- Existing security protocols remain owned by their specific ADRs. This decision
  governs composition and propagation, not credential issuance or authorization
  semantics.
- Ordinary ticket and bridge storage formats do not change.

## Security limitations and non-goals

- The snapshot/carrier design does not encrypt process memory or prevent deliberate
  unwrapping by trusted code.
- Targeted sibling filtering is not a sandbox and does not claim protection from every
  same-user process or unknown secret name.
- Multiline `REBAR_OPCERT_PRIVATE_KEY` is a compatibility source, not the preferred
  hosted transport: environment values may remain visible through process/container
  inspection. The file source avoids placing key bytes in the environment, and child
  environments remove the inline key's exact name unless they own the signer binding.
- RP-04 does not redesign webhook auth, MCP OAuth/resource-server semantics, op-cert
  cryptography/trust, or diagnostic sanitization. ADRs 0014, 0050, 0049, 0041, and
  0046 remain authoritative.
- RP-04 does not add universal hot rotation, a cloud-secret client, automatic keyring,
  arbitrary client/runner injection, or concrete future GitHub/GitLab/Jenkins bridge
  support.
- Optional observability's no-op failure behavior applies only to telemetry that
  cannot authorize or alter a Rebar decision.

## Prior-ADR relationship

- **ADR 0008:** deployment-time, fail-fast SSM materialization remains valid. RP-04
  makes restart/redeploy the application visibility boundary for static bindings;
  regenerating the deployment `.env` or source file alone does not rebind a running
  process.
- **ADR 0014:** inbound webhook authentication remains unchanged; RP-04 only narrows
  how its static material is represented and composed.
- **ADR 0041:** allowlist-first durable diagnostic sanitization remains the output
  boundary. Secret canaries supplement it.
- **ADR 0046:** its accepted security limitations bound all isolation and assurance
  claims here.
- **ADR 0049:** amended only as described in Decision 7.
- **ADR 0050:** MCP verifier and request-token semantics remain unchanged; startup
  composition avoids duplicate ambient secret reads.
- **ADR 0059:** its provider registry, model capabilities, and per-run session remain;
  RP-04 enriches the factory input with provider-native auth/session capabilities.
- **ADR 0083:** its vendor-neutral backend registry remains; RP-04 requires factories
  to consume the focused config/runtime binding they receive instead of ambient reload.

## Research sources

- **R1 — PydanticAI client injection:** provider implementations for
  [OpenAI](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/providers/openai.py),
  [Anthropic](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/providers/anthropic.py),
  and [Bedrock](https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/providers/bedrock.py)
  (installed PydanticAI 1.107.2 during research).
- **R2 — Anthropic native credential lifecycle:**
  [official credential package](https://github.com/anthropics/anthropic-sdk-python/tree/main/src/anthropic/lib/credentials)
  (installed Anthropic 0.121.0 source was inspected).
- **R3 — OpenAI callable/workload identity:**
  [client](https://github.com/openai/openai-python/blob/main/src/openai/_client.py) and
  [workload identity](https://github.com/openai/openai-python/blob/main/src/openai/auth/_workload.py)
  (installed OpenAI 2.53.0 source was inspected).
- **R4 — AWS credential chain:**
  [boto3 credential guide](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
  and [configure-aws-credentials](https://github.com/aws-actions/configure-aws-credentials).
- **R5 — Git credential helpers:**
  [gitcredentials](https://git-scm.com/docs/gitcredentials).
- **R6 — Git Credential Manager:**
  [credential stores](https://github.com/git-ecosystem/git-credential-manager/blob/release/docs/credstores.md)
  and [GitLab provider](https://github.com/git-ecosystem/git-credential-manager/blob/release/docs/gitlab.md).
- **R7 — GitHub Actions secret injection:**
  [Using secrets in GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
- **R8 — ECS Parameter Store injection and restart semantics:**
  [AWS ECS documentation](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-ssm-paramstore.html).
- **R9 — Langfuse constructor-time credentials:** pinned source for the
  [client](https://github.com/langfuse/langfuse-python/blob/f9b875fee0a79878462c82411141c57d01825167/langfuse/_client/client.py)
  and [resource manager](https://github.com/langfuse/langfuse-python/blob/f9b875fee0a79878462c82411141c57d01825167/langfuse/_client/resource_manager.py).
- **R10 — OpenTelemetry exporter constructor-time headers:** pinned
  [OTLP HTTP exporter source](https://github.com/open-telemetry/opentelemetry-python/blob/c49f6f456406e3b2fe4dd993a007abe5bb5b3eb1/exporter/opentelemetry-exporter-otlp-proto-http/src/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py)
  (installed OpenTelemetry 1.44.0 during research).
- **R11 — MCP verifier seam:** pinned
  [MCP Python SDK provider protocol](https://github.com/modelcontextprotocol/python-sdk/blob/2a1cc9449335faa29826ad3c4d44be174255b12b/src/mcp/server/auth/provider.py)
  (installed MCP SDK 1.29.0 during research).
- **R12 — Jira Cloud/Data Center auth analogue:**
  [MCP Atlassian authentication](https://github.com/sooperset/mcp-atlassian/blob/main/docs/authentication.mdx)
  and [Jira client](https://github.com/sooperset/mcp-atlassian/blob/main/src/mcp_atlassian/jira/client.py).
- **R13 — Jira adapter-boundary materialization:**
  [Airflow Jira connection](https://github.com/apache/airflow/blob/main/providers/atlassian/jira/docs/connections.rst)
  and [Jira hook](https://github.com/apache/airflow/blob/main/providers/atlassian/jira/src/airflow/providers/atlassian/jira/hooks/jira.py).
- **R14 — Provider-specific Jira constructors:**
  [Python Jira SDK examples](https://github.com/pycontribs/jira/blob/main/examples/auth.py),
  [Jira Cloud auth](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/),
  and [Jira Data Center PATs](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html).
- **R15 — Universal broker counterexample:**
  [Airflow secrets backends](https://airflow.apache.org/docs/apache-airflow/stable/security/secrets/secrets-backend/index.html).
- **R16 — Python/pip subprocess environment:**
  [Python subprocess](https://docs.python.org/3/library/subprocess.html) and
  [pip launcher](https://github.com/pypa/pip/blob/main/src/pip/_internal/utils/subprocess.py).
- **R17 — pre-commit inherited launcher:**
  [pre-commit util.py](https://github.com/pre-commit/pre-commit/blob/main/pre_commit/util.py).
- **R18 — CI environment scoping:**
  [GitHub Actions runner](https://github.com/actions/runner/blob/main/src/Runner.Sdk/ProcessInvoker.cs)
  and [Jenkins Credentials Binding](https://github.com/jenkinsci/credentials-binding-plugin).
- **R19 — Explicit hermetic alternative:**
  [tox `pass_env` / `disallow_pass_env`](https://github.com/tox-dev/tox/blob/main/docs/reference/config.rst).
- **R20 — Pydantic secret wrappers:**
  [Pydantic `SecretStr`/`SecretBytes`](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr)
  (installed Pydantic 2.13.4 and RP-04 representation probes).
- **R21 — Python keyring portability trade-offs:**
  [jaraco/keyring](https://github.com/jaraco/keyring).
- **R22 — Context-local process state:**
  [Python `contextvars`](https://docs.python.org/3/library/contextvars.html)
  plus the deterministic RP-04 concurrency/timeout probes on
  `relevant-liberal-xantus`.
- **R23 — Deployment/file analogues:**
  [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/),
  [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/),
  and pinned [oauth2-proxy configuration](https://github.com/oauth2-proxy/oauth2-proxy/blob/1f049e5ebdc8d2d03c4651adc998d47916ca965e/docs/versioned_docs/version-7.12.x/configuration/overview.md).

The internal research record is `relevant-liberal-xantus` and its RP-04 recovery
artifacts, audited 2026-08-16–17. No credential values, shell startup files, local
credential stores, or secret-manager values were read during that research.
