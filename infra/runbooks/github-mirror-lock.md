# Runbook — S6 GitHub mirror-lock cutover & rollback

Lock `navapbc/rebar` `main` and tags so **only the Gerrit replication deploy
key** (`rebar-gerrit-replication`, S5) can update them. Every human/admin push,
PR-merge, force-push, deletion, and tag write is rejected; GitHub becomes a
read-only mirror and contributions flow through Gerrit.

## How the lock works (the `update` rule)

A repository ruleset's **`update`** rule means *"restrict updates"*: only the
ruleset's `bypass_actors` may update the matched refs. A PR merge into `main` is
itself an **update to `main`** by a non-bypass actor, so the `update` rule
rejects **both direct pushes and PR merges** — there is no merge path left for a
non-bypass actor. We pair it with `deletion` and `non_fast_forward` to block
deleting and force-pushing, and a second ruleset locks all tags
(`creation`/`update`/`deletion`). The sole bypass actor is the deploy key.

**DeployKey bypass actor (exact form — keep TF and gh-api in sync):**

HCL (provider `integrations/github >= 6.8.0`):

```hcl
bypass_actors {
  actor_type  = "DeployKey"
  bypass_mode = "always"
  # actor_id intentionally OMITTED — a DeployKey bypass has no numeric id.
}
```

REST JSON (`POST /repos/navapbc/rebar/rulesets` → `bypass_actors[]`):

```json
{ "actor_type": "DeployKey", "bypass_mode": "always" }
```

`actor_id` is **omitted entirely** (do not send `"actor_id": null` — the API
rejects that). `bypass_mode` must be `always` (not `pull_request`, which is not
valid for a deploy key).

## Prerequisites

- A token with **Administration:write** on `navapbc/rebar`
  (`GITHUB_TOKEN` for Terraform / `gh auth` or `GH_TOKEN` for the scripts).
  **Never** put the token in a file or this doc — env / CI secret only.
- **S5 deploy key present**: `rebar-gerrit-replication`, write-enabled. Confirm:
  `gh api /repos/navapbc/rebar/keys --jq '.[].title'` lists it.
- **S6-pre snapshot present**: `infra/github/main-protection.snapshot.json`
  (the pre-existing `main-protection` ruleset, id `18048287`) — needed for
  rollback.

## Apply

Two equivalent paths. **Always `plan`/dry-run before applying.**

### Path A — Terraform (`infra/terraform-github/`)

Requires provider `>= 6.8.0` (native DeployKey bypass). A separate state key
(`rebar/prod/github.tfstate`) from the AWS stack.

```bash
export GITHUB_TOKEN=…            # Administration:write; NOT committed
cd infra/terraform-github
terraform init
terraform plan                   # MUST plan before apply; review the two rulesets
terraform apply                  # creates gerrit-mirror-lock-main + -tags
```

The `check "deploy_key_present"` block fails the plan/apply loudly if the S5 key
is absent. Removing the old `main-protection` ruleset is **not** done by
Terraform (it does not import it) — delete it via the gh-api step below or
`apply-mirror-lock.sh --delete-main-protection`.

If the available provider is **< 6.8.0** (no DeployKey bypass), use Path B.

### Path B — gh-api script (`infra/github/apply-mirror-lock.sh`)

The reliable live path and the provider-version fallback. No Terraform state.

```bash
gh auth status                                   # Administration:write token
./infra/github/apply-mirror-lock.sh              # create the two locks
# Optionally remove the pre-existing main-protection ruleset in the same run:
./infra/github/apply-mirror-lock.sh --delete-main-protection
```

The script gates on the deploy key existing (fails loudly otherwise), is
idempotent-ish (skips a lock that already exists by name), and echoes the
created ruleset ids.

## Verify empirically (do all of these)

The lock is only proven by rejected operations. As a **non-bypass** user
(normal credentials, *not* the deploy key):

1. **Direct push to main rejected:**
   `git push origin HEAD:main` → rejected by the ruleset (`update`).
2. **PR merge rejected:** open a PR into `main` and try to merge it (UI or
   `gh pr merge`) → blocked (the merge is an update to `main`).
3. **Force-push rejected:** `git push --force origin HEAD:main` → rejected
   (`non_fast_forward` / `update`).
4. **Branch deletion rejected:** `git push origin :main` → rejected
   (`deletion`).
5. **Tag push rejected:** `git push origin refs/tags/test-lock` → rejected
   (tag ruleset `creation`).
6. **Replication still works:** confirm a Gerrit→GitHub replication push (via
   the `rebar-gerrit-replication` deploy key) still updates `main`/tags — the
   bypass actor must pass while everyone else is blocked.

Inspect state any time:
`gh api /repos/navapbc/rebar/rulesets --jq '.[] | {id,name,enforcement}'`.

## Mirror-hygiene (OPTIONAL cutover steps)

Make the GitHub side read-through-only. These are **gh-api / repo-settings**
steps, not Terraform (Terraform here does not manage the `github_repository`):

```bash
# Disable Issues; reduce merge surface (PRs are already unmergeable via the lock)
gh api -X PATCH /repos/navapbc/rebar -F has_issues=false

# Disable Actions (mirror does not run CI)
gh api -X PUT /repos/navapbc/rebar/actions/permissions -F enabled=false

# Add a "mirror — contribute via Gerrit" banner to the repo description / README
gh api -X PATCH /repos/navapbc/rebar \
  -f description="Mirror of the Gerrit canonical repo — contribute via Gerrit, not GitHub."
```

## Rollback

### When to roll back (trigger)
Roll back **immediately** if, after the cutover, **any** of these hold — they mean `main` is
effectively frozen with no working path in:
- **Gerrit is unavailable or the `LLM-Review` bot is down / not voting**, so no change can reach
  `LLM-Review = +1` and nothing can submit.
- **Replication to GitHub `main` is failing** (GitHub `main` stops advancing while Gerrit `main`
  moves — check `replication_log` on the box or compare the two `main` SHAs).
- **A critical hotfix must land now** and the Gerrit path is blocked for any reason.

Do **NOT** roll back for a single rejected human push (`GH013: Cannot update this protected ref`) —
that is the lock working as designed.

### Fastest un-lock (seconds) — add a temporary bypass actor
To restore a human merge path **without deleting the lock** (land an emergency fix, then re-tighten),
add a temporary bypass actor to `gerrit-mirror-lock-main` instead of removing it. The ruleset update
API replaces the whole ruleset, so fetch it, append a bypass actor, and PUT it back:
```bash
RID=$(gh api repos/navapbc/rebar/rulesets --jq '.[] | select(.name=="gerrit-mirror-lock-main") | .id')
# Add a repo-admin (RepositoryRole id 5) bypass alongside the existing DeployKey bypass:
gh api repos/navapbc/rebar/rulesets/$RID \
  | jq '{name,target,enforcement,conditions,rules,bypass_actors:(.bypass_actors + [{actor_type:"RepositoryRole",actor_id:5,bypass_mode:"always"}])}' \
  | gh api -X PUT repos/navapbc/rebar/rulesets/$RID --input -
# … land the emergency change via a normal push/PR … then REMOVE the temporary bypass:
gh api repos/navapbc/rebar/rulesets/$RID \
  | jq '{name,target,enforcement,conditions,rules,bypass_actors:[.bypass_actors[] | select(.actor_type=="DeployKey")]}' \
  | gh api -X PUT repos/navapbc/rebar/rulesets/$RID --input -
```
This is faster and less disruptive than the full delete + restore below, and keeps the lock in place
(just with a temporary human exception you remove afterward).

> **Note on the current live state (WS7 cutover, epic b744):** only `gerrit-mirror-lock-main` was
> applied (tags left open); the legacy ruleset removed was **id 18306946** (not the older snapshot
> id `18048287` the script references). If restoring legacy protection, verify the snapshot matches
> the protection you actually want. Reconciling the snapshot/Terraform to the live ids is tracked as
> a follow-up.

### Full delete + restore (<15 minutes) — `infra/github/rollback-mirror-lock.sh`

```bash
gh auth status
./infra/github/rollback-mirror-lock.sh              # delete the two locks + restore main-protection
# variants:
./infra/github/rollback-mirror-lock.sh --disable           # disable (keep) the locks instead of deleting
./infra/github/rollback-mirror-lock.sh --reenable-features # also re-enable PRs/Issues/Actions
```

The script: (1) removes (or disables) `gerrit-mirror-lock-main` and
`gerrit-mirror-lock-tags` by name; (2) recreates `main-protection` from
`infra/github/main-protection.snapshot.json` (stripping read-only fields
`id`/`created_at`/`updated_at`/`_links`/`node_id` before POSTing); (3) optionally
re-enables features. **Verify** afterward that a normal PR-merge into `main`
works again.

If you applied via Terraform (Path A), `terraform destroy` (or removing the two
resources and `apply`) also drops the locks — but still run the rollback script
(or recreate from the snapshot) to restore `main-protection`, which Terraform
does not manage.

## Mirror-mode cutover playbook (the permanent-cutover steps)

> These steps make the lock a PERMANENT cutover. They are DELIBERATELY NOT applied
> by the d251 PoC, which ran an **apply-prove-rollback** (the lock was proven on the
> live repo, then `main-protection` was restored). Apply these only at a real cutover.

### Feature toggles (mirror hygiene)
Once `main` is locked to the deploy key, turn off the now-unused GitHub collaboration
surfaces so contributors are routed to Gerrit (all reversible via the same API):

```bash
# Disable Pull Requests is not a single repo flag; restrict to the mirror posture by
# disabling Issues, Projects, Wiki, and Actions, and removing merge methods so the UI
# offers no PR-merge path. (Re-enable by setting each back to true / restoring Actions.)
gh api -X PATCH repos/navapbc/rebar \
  -F has_issues=false -F has_projects=false -F has_wiki=false
gh api -X PUT  repos/navapbc/rebar/actions/permissions -F enabled=false
# Do NOT archive the repo — archiving freezes the replication bot's pushes too.
```

### Mirror banner (About + README)
Set the repo description/About and add a README banner at the top:

```bash
gh repo edit navapbc/rebar \
  --description "Read-only mirror of Gerrit (rebar.solutions.navateam.com). Contribute via Gerrit — see CONTRIBUTING.md."
```

README banner text (prepend to README.md at cutover):

```markdown
> **This GitHub repo is a read-only mirror.** `main` only advances via the
> Gerrit server at `rebar.solutions.navateam.com` after the automated `LLM-Review`
> gate passes. **Do not open GitHub PRs** — see [CONTRIBUTING.md](CONTRIBUTING.md)
> for the Gerrit contribution workflow.
```

### Contributor workflow under mirror mode (the CONTRIBUTING.md content)
At cutover, add a root `CONTRIBUTING.md` (and remove GitHub-PR guidance) describing the
Gerrit-only flow:

1. **Register your SSH key on Gerrit** — `https://rebar.solutions.navateam.com/settings/#SSHKeys`.
2. **Install the Change-Id hook** — `curl -Lo .git/hooks/commit-msg https://rebar.solutions.navateam.com/tools/hooks/commit-msg && chmod +x .git/hooks/commit-msg` (or use `git-review`).
3. **Add the Gerrit remote** — `git remote add gerrit ssh://<you>@rebar.solutions.navateam.com:29418/rebar`.
4. **Push for review** — `git push gerrit HEAD:refs/for/main` (optionally `%topic=rebar-<feature>`).
5. **The `LLM-Review` gate** — the review-bot reviews your patchset and votes
   `LLM-Review` (the single submit requirement: `label:LLM-Review=MAX AND -has:unresolved`).
   A stuck change can be re-reviewed via the receiver's `/rerun` (see ADR-0009 / S7 runbook).
6. **Submit** in Gerrit once the gate is satisfied; Gerrit replicates the merge to
   GitHub `main` (the only writer).
