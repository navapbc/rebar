# DCO rollout — live-validation evidence

Durable, in-repo record that the Developer Certificate of Origin (DCO) enforcement
was rolled out and verified against the **running** Gerrit server, for the epic
`breaded-ammonitic-elephant` (story `sepia-cardiac-hawk`). This is the artifact the
story's acceptance criteria ask for (server rejection message + migration-order proof
+ settings confirmation captured in-repo, not only in the ticket tracker).

## 1. Strict migration order (provable from git history)

The three changes landed on `main` in the required order — docs, then the e2e `-s`
fixes, then the `project.config` flag — so the config commit is a **descendant** of
both prerequisites (no window where a push could fail before the scripts were signed):

| Step | Path (representative) | Merged commit |
| --- | --- | --- |
| 1. docs | `CONTRIBUTING.md` (DCO section) | `4657b8926d6f97930ee009d2c636b33801785520` |
| 2. e2e `-s` | `infra/gerrit/feature-branch-e2e-scenarios.sh` | `7a1220d3a9965931db9e11df3310044bcb32e507` |
| 3. config flag | `infra/gerrit/project.config` (`[receive]`) | `dbaf0584b06608b48d7e483e54f7f21a6a9d4324` |

Verification (both return true):

```console
$ git merge-base --is-ancestor 7a1220d3a dbaf0584b && echo "config ⊇ e2e"
config ⊇ e2e
$ git merge-base --is-ancestor 4657b8926 dbaf0584b && echo "config ⊇ docs"
config ⊇ docs
```

## 2. Server flip

`receive.requireSignedOffBy = true` was rolled out to the live project by pushing
the merged `infra/gerrit/project.config` to `refs/meta/config` (as an administrator).
The push was surgical — it added only the `[receive]` block; all groups, labels,
ACLs, and webhooks were preserved (12 insertions, 0 deletions).

```console
$ git push … HEAD:refs/meta/config
   98e7cb3..71ee442  HEAD -> refs/meta/config
```

Rollback is documented inline in `infra/gerrit/project.config` (set the flag `false`
and re-run `setup-project.sh`).

## 3. Live verification (unsigned rejected, signed accepted)

After the flip, an **unsigned** commit pushed to `refs/for/main` was **rejected** at
push time:

```console
$ git push gerrit HEAD:refs/for/main        # commit WITHOUT a Signed-off-by trailer
 ! [remote rejected]     HEAD -> refs/for/main
       (commit 705361a: not Signed-off-by author/committer/uploader in message footer)
error: failed to push some refs
```

The **same** commit, amended with `git commit --amend -s` to add the sign-off, was
**accepted** (it created a Gerrit change, which was then abandoned as a throwaway
probe):

```console
$ git commit --amend -s --no-edit
$ git push gerrit HEAD:refs/for/main
remote: SUCCESS
remote:   https://rebar.solutions.navateam.com/c/rebar/+/463 … [NEW]
```

This is a direct end-to-end exercise of the exact signed-push path the e2e scripts
use (`git commit -s` → push to `refs/for/*`). The three e2e scripts were updated to
sign every commit (`reviewbot-e2e.sh` and `feature-branch-e2e-scenarios.sh` use
`git commit -s`; merges use `git merge --signoff`; `feature-branch-e2e.sh` also
installs a `prepare-commit-msg` sign-off hook as a belt-and-braces net).

## 4. GitHub web-signoff (belt-and-braces)

`web_commit_signoff_required` was set to `true` on `navapbc/rebar`, so commits made
through GitHub's web UI on the mirror also require a sign-off. The Gerrit
`requireSignedOffBy` flag above is the load-bearing gate; this is defense in depth.

```console
$ gh api repos/navapbc/rebar --jq .web_commit_signoff_required
true
```
