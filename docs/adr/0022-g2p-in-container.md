# ADR 0022 — Adopt gerrit-to-platform for CI, run it IN the Gerrit container

**Status:** Accepted (epic 1fa8 / story S3).
**Date:** 2026-07-01

## Context

Epic 1fa8 adds a CI `Verified` vote (ADR-0020) run on GitHub Actions. Something must
translate a Gerrit `patchset-created` event into a GitHub Actions run and route the
result back. We surveyed how actively-maintained Gerrit projects do CI (Gerrit itself,
Go, Chromium, AOSP, OpenStack, Eclipse) — all run self-hosted CI (Jenkins/Zuul/LUCI).
The ONE production-proven Gerrit→GitHub-Actions bridge is the Linux Foundation's
**`gerrit-to-platform` (g2p)**, deployed by ONAP, O-RAN-SC, OpenDaylight, FD.io.

## Decision

1. **Adopt `gerrit-to-platform` wholesale** as the bridge (not a bespoke hybrid). g2p
   is a set of Gerrit `hooks`-plugin console-scripts (`patchset-created`,
   `comment-added`, `change-merged`) that `workflow_dispatch` a `gerrit-verify.yaml`
   with the standard `GERRIT_*` inputs; the workflow votes back via SSH (story S5,
   `lfreleng-actions/gerrit-review-action`). This is the ONAP/O-RAN-SC pattern applied
   to a single repo (no org `.github` "magic repo" needed).

2. **Run g2p INSIDE the Gerrit container**, via an extended image
   (`infra/compose/Dockerfile.gerrit`: `FROM gerritcodereview/gerrit:3.14.1` + Python
   ≥3.11 + `pip install gerrit-to-platform`). **This is load-bearing:** the `hooks`
   plugin execs `$site/hooks/<event>` *from inside the container*, so g2p's Python
   interpreter MUST live in the container. A host-side g2p (hooks symlinked into a
   bind-mount) is broken — the container process cannot resolve a host interpreter
   path. (This corrected an earlier host-side plan that the plan-review gate BLOCKed.)
   The base image is Java-only, hence the added Python layer; a build-time assertion
   fails the build if the base Python is < 3.11.

3. **Enable the `hooks` core plugin** (bundled in `gerrit.war`, no download/sha256 —
   unlike the non-core `events-log`/`oauth`). `replication` (also core) is already
   enabled for S5 mirroring. Both are seeded into the site `plugins/` volume at boot.

4. **Materialise the g2p config + PAT pre-boot**, mirroring `materialize-deploy-key.sh`
   (NOT via container env / an entrypoint rewrite). `infra/gerrit/materialize-g2p-config.sh`
   renders `gerrit_to_platform.ini` (0600) from the SSM SecureString PAT onto a bind
   mount, fail-closed, and symlinks `replication.config` so g2p can discover the GitHub
   owner/repo. This keeps the token off container env / `ps`, consistent with how the
   Gerrit deploy key is handled. (Refines story S4's "container-env" wording to the
   materialise-pre-boot pattern used for all Gerrit-side secrets.)

## Consequences

- The Gerrit image is now a **locally built** extension, not the stock image. The added
  apt/pip layers are arch-neutral (still runs on the arm64 t4g box). Rebuild on a g2p
  or base-image bump; the base OS/Python is the one line to revisit (build-time check
  guards it).
- g2p coexists with the existing `webhooks`→review-bot path: `hooks` (server-side exec,
  new, CI dispatch) and `webhooks` (HTTP POST, existing, LLM-Review) fire independently
  on the same `patchset-created` event — the two gate votes are fully decoupled.
- Alternatives rejected: **Jenkins/Zuul** (self-hosted CI fleet — over-weight for one
  repo, and would not reuse `test.yml`); a **bespoke bridge** (mirrors no production
  Gerrit deployment); **host-side g2p** (breaks in-container hook exec).
