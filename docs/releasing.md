# Releasing rebar — updating the distribution channels

rebar ships from one Python package (`nava-rebar` on PyPI) across three channels.
**PyPI is the base; Homebrew and the MCP Registry reference it.** This is the
step-by-step runbook for cutting a release and updating every channel.

## Channels & where they live
- **PyPI** — `nava-rebar` (import pkg `rebar`; commands `rebar` / `rebar-mcp`).
  Published automatically by `.github/workflows/release.yml` on a `vX.Y.Z` tag,
  via **Trusted Publishing (OIDC)** — no token stored anywhere.
- **Homebrew tap** — repo `navapbc/homebrew-rebar`, `Formula/rebar.rb`. Users:
  `brew install navapbc/rebar/rebar`.
- **MCP Registry** — manifest `server.json` (`io.github.navapbc/rebar`),
  published with the `mcp-publisher` CLI.
- **GitHub Releases** — the human-facing "what's the latest version" surface at
  `github.com/navapbc/rebar/releases`. Not a distribution channel (nothing
  installs from it), but it must not lag the others. Created **automatically** by
  `.github/workflows/release.yml` on a `vX.Y.Z` tag (auto-generated notes, marked
  Latest, with the built sdist + wheel attached) — no manual step.

## Accounts / prerequisites (one-time — already done)
No separate accounts; everything rides on GitHub.
- PyPI **pending publisher** is registered: project `nava-rebar`, repo
  `navapbc/rebar`, workflow `release.yml`, environment `pypi`.
- GitHub **environment `pypi`** exists, restricted to `v*` tags.
- The Homebrew tap repo exists and is public.
- MCP Registry publishing authenticates in CI via GitHub Actions OIDC
  (`mcp-publisher login github-oidc`); the `io.github.navapbc/*` namespace is
  authorized by the workflow running in `navapbc/rebar`. See step 4 (manual
  `mcp-publisher login github` remains a fallback).

## Hard rules
- **PyPI is immutable.** A version can never be re-uploaded or amended (even after
  deletion). Every change ships as a NEW version (you can only *yank* a bad one).
  → metadata fixes (license, etc.) require a version bump.
- Keep `pyproject.toml` `version`, `server.json` `version` + `packages[].version`
  in lockstep, and the Homebrew formula's `url`/`sha256` pointing at the matching
  PyPI sdist.

## Supply-chain attestations (PyPI digital attestations)

Every PyPI release carries **PEP 740 digital attestations** — signed provenance
that ties each published artifact back to *this* repo's `release.yml` run. They
are produced automatically: `release.yml`'s publish step uses
`pypa/gh-action-pypi-publish@release/v1` with `id-token: write` and
`attestations: true`, so the action signs each wheel/sdist with the release job's
short-lived OIDC identity (the same identity Trusted Publishing uses — no stored
keys) and uploads the attestation alongside the artifact. There is nothing extra
to run at release time.

**How consumers verify them.** PyPI records each artifact's attestations under
the release's "provenance"; a downstream can confirm an artifact was built by
this repo's release workflow (not re-uploaded by a leaked token) with GitHub's
attestation tooling:

```bash
# Download the artifact you want to check, then verify its provenance names
# this repo + the release workflow as the signing identity.
pip download nava-rebar==X.Y.Z --no-deps -d /tmp/nava-rebar-verify
gh attestation verify /tmp/nava-rebar-verify/nava_rebar-X.Y.Z-py3-none-any.whl \
  --repo navapbc/rebar
```

A passing verify proves the file was built and signed by `navapbc/rebar`'s
`release.yml` via OIDC. (The attestations are also visible on the release's PyPI
page under each file's *"Provenance"* / *"View details"*.)

**Confirm a cut release actually exposes them** (add to the post-release smoke
test): after step 2 below shows the version live, the release's file listing on
`https://pypi.org/project/nava-rebar/X.Y.Z/#files` should show a *"Provenance"*
link per artifact, and the `gh attestation verify` above should pass. If it does
not, `id-token: write` / `attestations: true` was dropped from `release.yml` —
fix and ship a new version (PyPI is immutable; see Hard rules).

---

## Pinning — every release executable is an immutable reference

The release workflow (`.github/workflows/release.yml`) hardens its supply chain so that
**every executable it runs is identified by an immutable reference**, not a mutable tag. A
tag like `actions/checkout@v7` or `.../releases/latest/...` can be silently repointed at new
(possibly hostile) code after review; a commit SHA or a content digest cannot. The generic
checks (full-SHA action pins, `persist-credentials: false`, over-broad/OIDC permissions) are
enforced by **zizmor + actionlint scoped to `release.yml`** under `make lint`; the
rebar-specific structure is pinned by `tests/unit/test_release_workflow_pins.py`.

**(a) Actions are pinned to full 40-char commit SHAs.** Every `uses:` names a specific
commit with a trailing `# vX.Y.Z` comment for readability, e.g.
`actions/checkout@9c091bb...  # v7.0.0`. Resolve the SHA for an action's current stable
release tag (dereferencing an annotated tag to its commit) with `gh`:

```bash
# Lightweight tag → the ref already points at the commit:
gh api repos/actions/checkout/git/refs/tags/v7.0.0 --jq .object.sha
# Annotated tag → the ref points at a tag object; dereference it to the commit:
gh api repos/<owner>/<repo>/git/tags/<tag-object-sha> --jq .object.sha
```

To bump an action: pick the new stable tag, resolve its commit SHA as above, replace the
SHA **and** update the `# vX.Y.Z` comment. Never hand-edit a SHA — resolve it.

**(b) The build toolchain is hash-locked and built `--no-isolation`.** `build`, `hatchling`,
and `twine` (plus their transitive deps) are pinned with hashes in
`.github/release-requirements.txt`, compiled from `.github/release-requirements.in`. The
`build` job installs that lock into a fresh venv with `pip install --require-hashes --no-deps`
(so a tampered artifact fails the hash check), then builds with `python -m build
--no-isolation` (reusing that pinned env instead of an unpinned isolated one). Because
`--no-isolation` exposes the whole interpreter to the build backend,
`scripts/check_build_env_locked.py` asserts the installed set is a subset of the lock (base
`pip`/`setuptools`/`wheel` aside) and fails the release otherwise. Regenerate the lock after
editing the `.in`:

```bash
pip-compile --generate-hashes \
  --output-file=.github/release-requirements.txt .github/release-requirements.in
# (equivalently, with uv: uv pip compile --generate-hashes -o .github/release-requirements.txt \
#   .github/release-requirements.in)
```

**(c) The MCP publisher is exact-versioned + digest-verified.** The **MCP publisher** binary
(`mcp-publisher`) is pinned to an exact release version and its SHA-256, not
`releases/latest`. An unprivileged `mcp_verify` job downloads the pinned URL, verifies it
with `sha256sum -c --strict` **before** extraction, and hands the verified binary to the
OIDC-privileged `mcp_registry` job as an artifact — so the archive is never executed with the
registry-publishing OIDC identity until its digest matches. The pin lives in `release.yml` as
the `MCP_PUBLISHER_URL` / `MCP_PUBLISHER_SHA256` env pair.

To update the MCP publisher pin (version + SHA-256):

```bash
VER=v1.8.0   # the new mcp-publisher release
URL="https://github.com/modelcontextprotocol/registry/releases/download/${VER}/mcp-publisher_linux_amd64.tar.gz"
# The real digest — from upstream checksums.txt (preferred) or by hashing the download:
curl -fsSL "https://github.com/modelcontextprotocol/registry/releases/download/${VER}/registry_${VER#v}_checksums.txt" \
  | grep mcp-publisher_linux_amd64.tar.gz
#   …or: curl -fsSL "$URL" | sha256sum
```

Set both `MCP_PUBLISHER_URL` and `MCP_PUBLISHER_SHA256` in `release.yml` to the new pair,
then prove the pin matches the live artifact before committing:

```bash
make verify-mcp-pin   # downloads the pinned URL and asserts its SHA-256 == the embedded value
```

**Evidence.** Each pin-bearing job emits a named evidence fragment (`evidence-shapin` for the
action SHAs, `evidence-build` for the frozen tool versions, `evidence-artifacts` for the
built wheel/sdist digests, `evidence-mcp` for the verified publisher digest); a final
`consolidate_evidence` job concatenates them into `release-evidence.txt`, uploaded as a
release artifact.

---

## Build-once — the tested bytes are the published bytes

The release workflow **builds the wheel + sdist exactly once** (the `build` job) and flows
those *same bytes* through testing and publishing — nothing is rebuilt between "we tested it"
and "we shipped it". The shape:

- **`build` (build-once).** Runs the hash-locked `--no-isolation` build with
  `REBAR_BUILD_COMMIT=${GITHUB_SHA::7}` exported first, so the release commit's short SHA is
  baked into the artifacts (see below). It records `SHA-256` of each artifact into a
  `SHA256SUMS` manifest and uploads **one** bundle — wheel + sdist + `SHA256SUMS` — plus the
  `evidence-artifacts` fragment (the digests).
- **`wheel_test` / `sdist_test`** (`needs: build`). Each downloads that bundle and probes the
  artifact with **no repo checkout on the path**, so `import rebar` resolves to the installed
  package, never the source tree. `wheel_test` installs the wheel by filename; `sdist_test`
  installs from the sdist with `REBAR_BUILD_COMMIT` **unset** and no `.git`. Both assert a
  **non-null** `rebar._build_info.COMMIT`, load every packaged JSON schema, and run
  `rebar --help` / `rebar-mcp --help`.
- **`publish`** (`needs: [build, wheel_test, sdist_test]`). Downloads the *same* bundle,
  re-verifies it with `sha256sum -c SHA256SUMS`, and publishes **without rebuilding** (there
  is deliberately no `python -m build` in this job). A byte that changed between build and
  publish fails the hash gate.

### Baked build provenance (why `REBAR_BUILD_COMMIT`)

`python -m build` builds the sdist first, then builds the **wheel from the extracted sdist** —
a tree with no `.git`. The naive `git rev-parse` hook therefore baked `COMMIT = None` into the
published wheel, losing the gate-code provenance the signing resolver falls back to on non-git
installs. `hatch_build.py` now resolves the commit by a four-step precedence:
`REBAR_BUILD_COMMIT` env → an existing non-null `COMMIT` already baked into the sdist-shipped
`_build_info.py` (preserve-existing) → `git rev-parse --short HEAD` → `None`. The release build
sets the env var, so both the sdist and the wheel-from-sdist bake the exact release SHA; a dev
`pip install .` (env unset) still falls back to git/None and never fails. **A set-but-empty
`REBAR_BUILD_COMMIT` is a hard error** — release context must not silently lose provenance. The
`sdist_test` job (env unset) proves the preserve-existing path end-to-end; ordinary CI catches
the defect too via the `artifact-probe` job in `test.yml`.

---

## Release procedure

### 1. Bump versions (one commit)
```bash
# pyproject.toml:  version = "X.Y.Z"
# server.json:     "version": "X.Y.Z"  AND  packages[0].version: "X.Y.Z"
git add pyproject.toml server.json && git commit -m "Release X.Y.Z" && git push origin main
```
(Local sanity: `python -m build && python -m twine check dist/*`.)

### 1a. Update CHANGELOG.md (before tagging)
`CHANGELOG.md` is the **user-facing** changelog (Keep a Changelog shape),
generated from conventional commits with **git-cliff** and then hand-curated.
(Agent-visible *contract* changes stay in `docs/release-notes.md`.) Install the
pinned tool once — a standalone Rust binary, **not** a pyproject dev extra:
```bash
pipx install git-cliff==2.13.1
```
Then, for the release you are cutting:
```bash
make changelog VERSION=vX.Y.Z   # prepends the [X.Y.Z] section from unreleased commits
# hand-curate (~5 min) the freshly prepended top section: drop noise, tighten
# wording, group sensibly — the generated lines are a starting point, not the entry.
git add CHANGELOG.md && git commit --amend --no-edit   # fold into the release commit (step 1)
```
**Dial position: generate-then-edit.** Pure-generated output was rejected as
ledger-like; fully hand-written prose (mypy-style) was rejected as unsustainable
for a solo maintainer. `make changelog` is **prepend-scoped and idempotent** — it
never regenerates the whole file, so curated history is never overwritten (re-running
with an already-present `VERSION` is a no-op). The one-time bootstrap that generated
the file back through v0.1.0 is not repeated.

### 2. Tag → PyPI publishes automatically
```bash
git tag -a vX.Y.Z -m "nava-rebar X.Y.Z" && git push origin vX.Y.Z
gh run watch "$(gh run list --workflow=release.yml --event=push -L1 --json databaseId -q '.[0].databaseId')" --exit-status
```
The workflow builds + `twine check`s + publishes via OIDC, then its
`github_release` job **creates the GitHub Release** for the tag (auto-generated
notes, marked Latest, sdist + wheel attached). (A `workflow_dispatch` run builds
without publishing or releasing — use it as a dry run.)

Verify the version is live on the channel:
```bash
curl -s https://pypi.org/pypi/nava-rebar/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
```
(Full install + probe of the published artifact is step 5 below.)

### 3. Update the Homebrew tap (after PyPI has the new sdist)
```bash
# fetch the new sdist url + sha256
curl -s https://pypi.org/pypi/nava-rebar/X.Y.Z/json | python3 -c "
import json,sys
for u in json.load(sys.stdin)['urls']:
    if u['packagetype']=='sdist': print(u['url']); print(u['digests']['sha256'])"
```
Edit `Formula/rebar.rb` in `navapbc/homebrew-rebar`: update `url` + `sha256`
(and `license` if it changed), then:
```bash
brew style navapbc/rebar/rebar         # after re-tapping / pulling
git commit -am "rebar X.Y.Z" && git push     # in the tap repo
```
The formula installs the base package (zero pip deps → no `resource` blocks). The
MCP server (`rebar-mcp`) needs the `mcp` extra; brew users get it via
`pipx install 'nava-rebar[mcp]'` or `uvx --from nava-rebar[mcp] rebar-mcp`.

### 4. Update the MCP Registry — automated (OIDC in CI)
**No manual step in the normal path.** The `mcp_registry` job in
`.github/workflows/release.yml` publishes `server.json` on the `vX.Y.Z` tag using
**GitHub Actions OIDC** (`mcp-publisher login github-oidc`) — the same
no-stored-secret trust model as the PyPI Trusted Publishing above. The runner's
OIDC token proves the workflow runs in `navapbc/rebar`, which authorizes the
`io.github.navapbc/*` namespace. The job `needs: publish`, so it runs after PyPI.
Because the registry validates the package against PyPI, the job first **waits for
pypi.org to serve the new version** (up to 5 min) — this absorbs the
publish→PyPI propagation lag that once 404'd the registry check — then runs
`mcp-publisher publish` with a **bounded, idempotent retry** (it checks the registry
for the version before each attempt, so a re-run after a duplicate publish is a no-op).

Standing prereqs (already satisfied; listed so they aren't rediscovered):
- The PyPI release must carry the `mcp-name: io.github.navapbc/rebar` annotation
  (it lives in `README.md` = the PyPI long description; the registry greps it to
  verify package ownership).
- `server.json` `description` must be **≤ 100 characters** (the registry rejects
  longer with HTTP 422 — note PyPI/pyproject have no such limit, so the two
  descriptions can differ).
- The tag commit's `server.json` must carry the release version (the `Release
  X.Y.Z` commit bumps it before tagging — step 1), since the job publishes the tree as-is.

Verify after the run:
`curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.navapbc/rebar"`.

**Manual fallback** (only if the CI job is unavailable, or to publish out of band —
e.g. a metadata-only re-push): `mcp-publisher login github` does an interactive
browser/device auth as a navapbc-org GitHub user, then `mcp-publisher publish`
publishes `./server.json`. This path needs your `navapbc` membership **public**
(`gh api --method PUT orgs/navapbc/public_members/<you>`) and captures namespace
claims **at login time** — if you publicize membership *after* logging in, re-run
`mcp-publisher login github` before `publish` or you'll get a 403 listing only
`io.github.<you>/*`. The interactive JWT also expires, which is exactly why the
normal path is the OIDC job above.

### 5. Smoke test — PRE-publication, against the exact wheel/sdist
The install-and-probe smoke test now runs **before** anything is published, in-workflow,
against the **exact bytes** that will be shipped. Under build-once (above) the `wheel_test`
and `sdist_test` jobs — which `publish` `needs:` — install the built wheel/sdist with **no
repo checkout on the path**, run `import rebar`, `rebar --help`, `rebar-mcp --help`, load
every packaged JSON schema, and assert a non-null baked `_build_info.COMMIT`. A packaging
defect (missing data file, a `COMMIT = None` bake, a broken entry point) therefore **blocks
the publish** instead of surfacing only after the immutable artifact is already live. Because
`publish` promotes the *same* hash-gated bundle those jobs tested (no rebuild), a green
`wheel_test`/`sdist_test` is a guarantee about the published artifact, not a separate build.

You can still run the post-publication live probe below as belt-and-suspenders (it drives the
channel-installed binary end-to-end), but it is no longer the *only* gate against a broken
shipped package — the pre-publication jobs are.
```bash
# 1) Pull the just-published version FROM THE CHANNEL (with the mcp extra so
#    rebar-mcp is covered too). --force replaces any stale local/editable install.
pipx install "nava-rebar[mcp]==X.Y.Z" --force

# 2) Probe the INSTALLED (distribution-channel) build — not the repo's editable
#    .venv. Point $REBAR at the pipx shim so the probe drives the shipped binary.
REBAR="$(pipx environment --value PIPX_BIN_DIR)/rebar" bash scripts/probe-rebar.sh

# Optional: also exercise the REAL project store (it snapshots the existing
# tickets, removes only what it creates, and verifies the store is unchanged):
REBAR="$(pipx environment --value PIPX_BIN_DIR)/rebar" PROBE_LIVE=1 bash scripts/probe-rebar.sh
```
`scripts/probe-rebar.sh` exercises every command + edge cases and prints
`PROBE RESULT: N passed, 0 failed`; a non-zero exit means the published build is
broken. Since **PyPI is immutable**, recover by *yanking* the bad version and
shipping a fixed version bump (re-run this step on the new version).

### Fixing or retiring a bad GitHub Release
The `github_release` job is idempotent (re-running the workflow for a tag edits
the existing release rather than erroring). To correct one after the fact:
```bash
gh release edit vX.Y.Z --notes-file NOTES.md      # amend auto-generated notes
gh release edit vX.Y.Z --prerelease --latest=false # de-list a bad release
gh release delete vX.Y.Z --yes                      # remove it entirely (keeps the tag)
```
Note this only touches the GitHub Release surface — the PyPI artifact is
immutable and is recovered by *yanking* + a version bump (see Hard rules).

### Optional — live Jira capability preflight

Before relying on Jira sync (or when validating bridge changes against a real
Jira instance), run the Jira capability preflight:

```bash
export JIRA_URL=... JIRA_USER=... JIRA_API_TOKEN=...   # optional: JIRA_PROJECT
rebar bridge-probe
```

`rebar bridge-probe` runs a six-step round-trip (create → label → property-write
→ JQL-search → property-read → delete) against live Jira, creating and deleting a
single throwaway issue. It prints `PROBE_PASS`/`PROBE_FAIL` per step and exits 0
(all pass), 1 (a step failed), or 2 (missing credentials). For broader, manual
field-level pressure tests see `scripts/jira-pressure-test/`.

---

## 1.0 declaration (staged — lands in the v1.0.0 release change, NOT before)

When rebar cuts **v1.0.0**, the release change itself makes **exactly two** edits
that turn the compatibility policy into an operative SemVer promise. They are
recorded here (not only in a ticket) so the requirement survives independently:

1. **`pyproject.toml` classifier:** `Development Status :: 4 - Beta` →
   `Development Status :: 5 - Production/Stable`.
2. **`docs/api-stability.md`:** remove the "Pre-1.0 caveat" so the stability
   matrix becomes the operative post-1.0 SemVer promise (breaking changes only on
   a major bump, with a deprecation window).

Do **not** make these edits before the v1.0.0 release change — until then rebar is
0.x and the caveat stands. (These are staged only; the epic that added this note
does not execute them.)

## Release-policy decisions (1.0)

### Tag signing — SKIPPED (annotated tags + attestations instead)
We do **not** GPG/sign release tags. Rationale: the 2026 peer baseline
(CPython/pip/ruff/uv/pytest/httpx) signs zero new release tags — CPython dropped
GPG at 3.14 (PEP 761); ruff/uv/httpx use lightweight tags — and artifact
provenance is already covered by **PEP 740 attestations** + GitHub immutable
releases (see "Supply-chain attestations" above). We keep **annotated** tags and
immutable releases on. Revisit triggers: a workflow compromise entering the threat
model, or a downstream consumer asking to cryptographically verify tags.

### Release candidates — SKIPPED (with an RC-safety constraint on release.yml)
We do **not** ship `rc` release trains. Rationale: an RC only yields signal if
someone runs it; with no external adopters `--pre` installs ≈ 0, and `1.0.1` is the
de-facto rc. Peers agree (pydantic v2 went beta→final in 13 days with zero rcs;
attrs/structlog skipped 1.0 entirely). Revisit trigger: an external adopter
volunteering to test a pre-release.

> **RC-safety constraint (today's automation is RC-unsafe).** `release.yml` fires
> on **all** `v*` tags, passes `--latest` unconditionally to `gh release create`,
> and runs the MCP-registry publish job. So a hypothetical **`v1.0.0rc1`** tag
> would be marked *Latest* **and** published to the MCP registry — wrong for a
> pre-release. **No rc-form tag may be pushed** unless `release.yml` first gains a
> prerelease guard: detect `rc` in the tag → pass `--prerelease` (not `--latest`)
> and skip the MCP-registry job. Use PEP 440 spelling (`1.0.0rc1`, not `-rc.1`).

### Deferrals (non-goals for now)
- **SBOM / license-scan tooling** beyond GitHub's dependency-graph export — post-1.0.
- **Renovate** as a dependency updater — parked as idea `misogynic-cerulean-goldfish`;
  the approved posture is Dependabot advisory PRs (`.github/dependabot.yml`).
- A **lockfile for optional extras** — revisit post-1.0.

### Dependency updates — Dependabot advisory PRs
`.github/dependabot.yml` runs GitHub-Actions version updates monthly. Because PRs
cannot merge here, its PRs are **advisory**: the maintainer reads the diff and
lands the bump via a Gerrit change. This is the pip/pydantic GitHub-native shape,
deliberately *not* the bespoke Gerrit-pushing bot some Gerrit peers run. See that
file's header for the full rationale.

---

## Known follow-ups (not release-blocking)
- **Lock fallback asymmetry** — when util-linux `flock` is absent, the bash write
  paths use a `mkdir` lock while `ticket_txn.py` uses `fcntl.flock`; they don't
  mutually exclude. Pre-existing; unify on a Python-`fcntl` fallback when touched.
