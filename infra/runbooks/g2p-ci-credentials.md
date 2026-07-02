# Runbook — CI `Verified` gate credentials (operator steps)

The CI `Verified` vote (epic 1fa8, ADR-0020) needs **two credentials that hold live
secret material** and therefore CANNOT be provisioned from the repo. This runbook is
the operator handoff for creating, installing, **rotating**, and **retiring** them.

There are two independent legs of the CI path, each with its own secret:

| Leg | Direction | Secret | Where it lives |
|---|---|---|---|
| **Dispatch** | Gerrit → GitHub | fine-grained GitHub **PAT** | SSM `/rebar/prod/g2p-github-pat` → materialised into `gerrit_to_platform.ini` at boot |
| **Vote-back** | GitHub → Gerrit | CI service account **SSH private key** | SSM `/rebar/prod/ci-gerrit-ssh-key` + GitHub Actions secret `GERRIT_SSH_PRIVKEY` |

Both SSM slots already exist as `CHANGEME` placeholders (`infra/terraform/ssm.tf`);
this runbook populates them. **Never** commit a real secret value — the repo only
holds Terraform placeholders (`ignore_changes = [value]`) and templated/materialised
files (ADR-0008 / ADR-0022 secrets invariant).

---

## Prerequisites

- Gerrit **admin** SSH key on your workstation (`~/.ssh/gerrit_admin`), reachable at
  `rebar.solutions.navateam.com:29418`.
- AWS credentials that can `ssm:PutParameter` on `/rebar/prod/*` in `us-east-1`.
- GitHub admin on `navapbc/rebar` (to set repo variables + secret).
- `gh` CLI (optional, for the GitHub steps) or the repo Settings UI.

---

## 1. The dispatch leg — GitHub PAT (`/rebar/prod/g2p-github-pat`)

gerrit-to-platform (running in the Gerrit container) uses this token to
`workflow_dispatch` `.github/workflows/gerrit-verify.yaml`.

### 1a. Create the fine-grained PAT
GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**:

- **Resource owner:** `navapbc`; **Repository access:** *Only select repositories* →
  **`navapbc/rebar`** (single-repo scope — do NOT grant org-wide).
- **Repository permissions:**
  - **Actions:** *Read and write* (required to `workflow_dispatch`).
  - **Contents:** *Read-only* (checkout / ref discovery).
  - **Metadata:** *Read-only* (mandatory baseline).
  - Everything else: *No access*.
- **Expiration:** set a finite expiry (see §3 rotation cadence) — never "no expiration".

Copy the token once (`github_pat_…`).

### 1b. Store it in SSM
```bash
aws ssm put-parameter --region us-east-1 \
  --name /rebar/prod/g2p-github-pat \
  --type SecureString --overwrite \
  --value 'github_pat_…'      # paste the token; do NOT echo it into shell history
```
It is materialised into `gerrit_to_platform.ini` (0600) at the next boot by
`infra/gerrit/materialize-g2p-config.sh` (fail-closed). To pick it up **without** a
reboot, re-run that script on the box and reload the `hooks` plugin (or restart the
Gerrit container).

---

## 2. The vote-back leg — CI service account SSH key

GitHub Actions SSHes into Gerrit `:29418` **as the CI service account** to cast
`Verified` (ADR-0023). This needs (a) a keypair, (b) the account created with the
public key registered, (c) the private key in SSM + as the Actions secret, and (d)
four repo variables so the workflow knows where/who to SSH as.

### 2a. Generate the CI keypair (dedicated, not a personal key)
```bash
ssh-keygen -t ed25519 -C rebar-ci-gerrit -f ~/.ssh/rebar_ci_gerrit -N ''
# → ~/.ssh/rebar_ci_gerrit      (PRIVATE — goes to SSM + the Actions secret)
# → ~/.ssh/rebar_ci_gerrit.pub  (PUBLIC  — registered on the Gerrit account)
```

### 2b. Create the Gerrit CI service account + register the public key
The repo script does this idempotently (creates the account, puts it in **Service
Users** so the `label-Verified` ACL applies, registers the **public** key). It never
touches the private key.
```bash
CI_SSH_PUBKEY_FILE=~/.ssh/rebar_ci_gerrit.pub \
  bash infra/gerrit/setup-ci-service-account.sh
```
(Override `CI_BOT_USER` / `CI_BOT_EMAIL` if you want a different username; default is
`rebar-ci-bot`. That username is the value of the `GERRIT_SSH_USER` repo variable in
§2d.)

### 2c. Store the PRIVATE key in SSM
Kept in SSM as the source of record so it can be re-installed if the GitHub secret is
lost. The box never reads this param — it is GitHub-side only.
```bash
aws ssm put-parameter --region us-east-1 \
  --name /rebar/prod/ci-gerrit-ssh-key \
  --type SecureString --overwrite \
  --value "$(cat ~/.ssh/rebar_ci_gerrit)"
```

### 2d. Set the GitHub repo variables + secret
The workflow reads four **variables** and one **secret** (`gerrit-verify.yaml`):

```bash
# The secret — the CI service account PRIVATE key.
gh secret set GERRIT_SSH_PRIVKEY --repo navapbc/rebar < ~/.ssh/rebar_ci_gerrit

# The variables (non-secret).
gh variable set GERRIT_SERVER   --repo navapbc/rebar --body 'rebar.solutions.navateam.com'
gh variable set GERRIT_SSH_USER --repo navapbc/rebar --body 'rebar-ci-bot'   # the CI_BOT_USER from §2b
gh variable set GERRIT_URL      --repo navapbc/rebar --body 'https://rebar.solutions.navateam.com'
# Known-hosts line(s) for :29418, so the action can verify the host key (no TOFU).
gh variable set GERRIT_KNOWN_HOSTS --repo navapbc/rebar \
  --body "$(ssh-keyscan -p 29418 rebar.solutions.navateam.com 2>/dev/null)"
```

| Name | Kind | Value |
|---|---|---|
| `GERRIT_SERVER` | var | `rebar.solutions.navateam.com` |
| `GERRIT_SSH_USER` | var | the CI account username (`rebar-ci-bot`) |
| `GERRIT_URL` | var | `https://rebar.solutions.navateam.com` |
| `GERRIT_KNOWN_HOSTS` | var | `ssh-keyscan -p 29418 …` output for the host |
| `GERRIT_SSH_PRIVKEY` | secret | contents of `~/.ssh/rebar_ci_gerrit` |

### 2e. Clean up local key material
Once the private key is in SSM **and** the GitHub secret, remove the local copies:
```bash
shred -u ~/.ssh/rebar_ci_gerrit ~/.ssh/rebar_ci_gerrit.pub 2>/dev/null || \
  rm -f ~/.ssh/rebar_ci_gerrit ~/.ssh/rebar_ci_gerrit.pub
```

---

## 3. Rotation

Rotate on your standard cadence and immediately on any suspected exposure.

### 3a. Rotate the GitHub PAT
1. Generate a **new** fine-grained PAT (same scope as §1a).
2. `aws ssm put-parameter … --name /rebar/prod/g2p-github-pat --overwrite --value 'new'`.
3. Re-materialise on the box (`materialize-g2p-config.sh`) + reload `hooks` (or restart
   the container). Verify a fresh patchset dispatches a run.
4. **Revoke** the old PAT in GitHub (Developer settings). Overlap is fine; revoke once
   the new one is proven.

### 3b. Rotate the CI SSH key (zero-downtime, add-then-remove)
1. Generate a **new** keypair (§2a, e.g. `-f ~/.ssh/rebar_ci_gerrit.new`).
2. **Add** the new public key alongside the old one (both valid during overlap):
   ```bash
   ssh -i ~/.ssh/gerrit_admin -p 29418 admin@rebar.solutions.navateam.com \
     gerrit set-account rebar-ci-bot --add-ssh-key - < ~/.ssh/rebar_ci_gerrit.new.pub
   ```
3. Update SSM + the GitHub secret to the **new private** key (§2c, §2d `gh secret set`).
4. Trigger a `recheck` and confirm the vote still lands (now via the new key).
5. **Remove** the old public key from the account:
   ```bash
   ssh -i ~/.ssh/gerrit_admin -p 29418 admin@rebar.solutions.navateam.com \
     gerrit set-account rebar-ci-bot --delete-ssh-key '<old-key-comment-or-index>'
   ```
6. Update the SSM `/rebar/prod/ci-gerrit-ssh-key` slot to the new private key so the
   source of record matches. Shred the local new-key files (§2e).

---

## 4. Retire (decommission the CI vote entirely)

To fully retire the CI service account (e.g. rolling back to single-vote gating for
good — the temporary back-out is in `two-vote-gate-rollback.md`):

1. **Disable the gate first** — deactivate the `Verified` submit requirement per
   `infra/runbooks/two-vote-gate-rollback.md` so changes don't get stuck waiting on a
   vote that will never come.
2. **GitHub:** revoke the PAT; delete the `GERRIT_SSH_PRIVKEY` secret and the four
   `GERRIT_*` variables (`gh secret delete` / `gh variable delete`).
3. **Gerrit:** remove the CI account's SSH keys and set it inactive:
   ```bash
   ssh -i ~/.ssh/gerrit_admin -p 29418 admin@rebar.solutions.navateam.com \
     gerrit set-account rebar-ci-bot --inactive
   ```
   (Or remove it from **Service Users** so the `label-Verified` ACL no longer applies.)
4. **SSM:** overwrite both slots back to `CHANGEME` (do NOT delete the params —
   Terraform owns their existence; `ignore_changes = [value]` won't fight you):
   ```bash
   aws ssm put-parameter --region us-east-1 --overwrite --type SecureString \
     --name /rebar/prod/g2p-github-pat --value CHANGEME
   aws ssm put-parameter --region us-east-1 --overwrite --type SecureString \
     --name /rebar/prod/ci-gerrit-ssh-key --value CHANGEME
   ```

---

## Blast radius / security notes

- A leaked **`GERRIT_SSH_PRIVKEY`** lets an attacker cast `Verified` (see ADR-0023 for
  the full analysis + mitigations: Service-Users-only ACL, the key votes only, rotation).
- A leaked **PAT** is fine-grained + single-repo + Actions/Contents/Metadata only — it
  can dispatch/read `navapbc/rebar` workflows, nothing else. It cannot push to `main`
  (the mirror lock + it has no such scope).
- Neither secret ever lands in the image, container env, or a committed file — PAT is
  materialised at boot (0600); the SSH private key is GitHub-side + SSM only.

## See also
- `docs/adr/0020-two-vote-ci-gate.md` — the two-vote gate design.
- `docs/adr/0022-g2p-in-container.md` — g2p in-container + materialise-at-boot.
- `docs/adr/0023-inbound-github-gerrit-ssh-vote.md` — the inbound SSH vote path.
- `infra/runbooks/two-vote-gate-rollback.md` — activation, back-out, E2E, design notes.
