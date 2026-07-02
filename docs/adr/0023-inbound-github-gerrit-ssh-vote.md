# ADR 0023 — Inbound GitHub → Gerrit SSH vote for the CI `Verified` label

**Status:** Accepted (epic 1fa8 / story S4).
**Date:** 2026-07-01

## Context

ADR-0020 adds a second gate vote, `Verified`, cast by CI. ADR-0022 adopts
gerrit-to-platform (g2p) to translate a Gerrit `patchset-created` into a GitHub Actions
run. That covers the **outbound** leg (Gerrit → GitHub, via g2p's fine-grained PAT).
The result must then come **back**: the GitHub Actions run has to write the `Verified`
label onto the Gerrit change. Gerrit's write API for a vote is either REST (HTTP) or the
`gerrit review` SSH command; the production g2p pattern (ONAP/O-RAN-SC) uses SSH via
`lfreleng-actions/gerrit-review-action`. This ADR records that **inbound** path and its
trust boundary — an external system (GitHub Actions) reaching into Gerrit to mutate gate
state.

## Decision

1. **The vote-back is an inbound SSH call to Gerrit `:29418`.** `gerrit-verify.yaml`
   (story S5) runs `lfreleng-actions/gerrit-review-action`, which SSHes to Gerrit as a
   dedicated **CI service account** and runs `gerrit review` to set
   `Verified +1` (CI passed) or `Verified -1` (failed/cancelled — fail-closed). The run
   also clears `Verified→0` at start (GerriScary-safe; CVE-2025-1568).

2. **A dedicated CI service account, key-only, Service-Users-scoped.** The account
   (`rebar-ci-bot`) is provisioned by `infra/gerrit/setup-ci-service-account.sh`, placed
   in the **Service Users** group, and its ONLY credential is an SSH keypair (no HTTP
   password — it has no HTTP surface, unlike the review-bot). Its `label-Verified` ACL
   comes solely from Service-Users membership (project.config, ADR-0020), so a leaked key
   grants *voting*, not admin.

3. **Key custody.** The **private** key is stored in SSM `/rebar/prod/ci-gerrit-ssh-key`
   (source of record; the box never reads it) and installed as the GitHub Actions repo
   secret `GERRIT_SSH_PRIVKEY`. Only the **public** key is registered on the Gerrit
   account. The workflow verifies the Gerrit host key via the `GERRIT_KNOWN_HOSTS` repo
   variable (no trust-on-first-use). See `infra/runbooks/g2p-ci-credentials.md`.

4. **Key scope = vote-only.** The CI account is a plain Service User: it can cast the
   two gate labels its group is granted (`Verified`, and — irrelevantly — `LLM-Review`)
   and read/clone the project. It is **not** an administrator, cannot push to `main`
   (the mirror is Gerrit-replication-only), cannot edit `refs/meta/config`, and cannot
   submit changes. It votes; that is all.

## Consequences

- **Blast radius of a leaked `GERRIT_SSH_PRIVKEY`:** an attacker who exfiltrates the
  Actions secret can SSH in as `rebar-ci-bot` and cast `Verified` on arbitrary changes —
  i.e. forge *one* of the two independent gate votes. It is bounded by design:
  - **`Verified` alone does not submit** — `LLM-Review=MAX` (a separate voter, separate
    key) is still required. A single forged label cannot land code.
  - **Service-Users-only ACL** — the key can vote, not administer, push, or submit.
  - **Vote-only account** — no HTTP token, no repo write, no config write.
  - **Rotation** — add-then-remove SSH-key rotation (runbook §3b) revokes a suspected
    key with zero downtime; GitHub secret scanning + a finite-lifetime key limit the
    window.
  This is an accepted, mitigated risk: the inbound vote path is the price of reusing the
  production g2p bridge instead of a bespoke callback.
- **Two secrets, two directions, two custodians:** the PAT (outbound dispatch,
  materialised on the box) and the SSH key (inbound vote, GitHub-side). Neither is ever
  in the image or a committed file (ADR-0008/0022 secrets invariant).
- **Fail-closed everywhere:** if the SSH vote cannot be cast (bad/rotated key, host
  unreachable, missing repo var), no `+1` is written, so the change stays
  unsubmittable — never submittable-without-CI.

## Alternatives considered

- **REST vote with an HTTP token** instead of SSH: workable, but diverges from the
  battle-tested g2p/`gerrit-review-action` pattern and adds a second credential type on
  the CI account (HTTP token *and* the account) for no gain. SSH key-only is simpler and
  is what the LF actions expect.
- **A Gerrit-side poller that reads GitHub check status** (outbound-only, no inbound
  write): avoids exposing a Gerrit credential to GitHub, but requires bespoke polling
  glue, inverts the g2p model, and adds latency. Rejected — the inbound SSH vote is the
  standard, and its blast radius is acceptably bounded above.
- **Reusing the replication deploy key or the review-bot token** for the vote: rejected —
  conflates identities/blast radii; the vote path gets its own least-privilege account.
