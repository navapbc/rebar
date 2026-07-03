# Epic 88ab — feature-branch flow: live validation evidence

Durable run-log evidence for the epic-88ab feature-branch flow, captured from live
Gerrit changes on `rebar.solutions.navateam.com` + GitHub Actions on `navapbc/rebar`.
This file is the committed companion to the per-ticket comment trails (the tickets hold
the same evidence; this file makes it durable in-repo). See ADR-0020 (two-vote gate),
ADR-0021 (replication change refs), ADR-0025 (feature-branch merge-carry).

## S3 (will-tile-plum) — CI coverage for the feature-branch flow

### AC1 — g2p dispatch is branch-agnostic (verified finding)
`gerrit-to-platform` does **not** branch-filter dispatch: its event→workflow mapping is
filename-substring based with no branch keys in `gerrit_to_platform.ini` (source:
`lfit/releng-gerrit_to_platform@44d5d46` — `patchset_created.py` / `github.py` /
`helpers.py`). BUT g2p dispatches via GitHub `workflow_dispatch(ref=refs/heads/{GERRIT_BRANCH})`,
which requires that ref to exist on the GitHub mirror. This drove the AC2 remediation.

### AC2 — replication.config replicates feature/* (committed + deployed live)
- Committed: `infra/gerrit/replication.config` — `push = +refs/heads/feature/*:refs/heads/feature/*`
  on `[remote "github"]`, `autoReload = true` (no Gerrit restart on config change).
- Live proof (pre-fix vs post-fix):
  - **Change 198** (feature/s3-ci, PRE-fix): got LLM-Review+1 and **zero CI** — the
    `workflow_dispatch` targeted a non-existent mirror ref (the exact failure the story exists to catch).
  - **Change 200** (feature/s3-ci2, POST-fix, no rebar-ticket trailer): CI dispatched
    (run 28644216042), Verified=-1 (the gate correctly failing a bad commit).
  - **Change 201** (feature/s3-ci2, POST-fix, well-formed): CI dispatched (run 28645046830),
    **LLM-Review=MAX AND Verified=MAX** — a feature/* change earns BOTH gate votes end-to-end.

### AC3 — merge change gets CI on the actual merge tree
- **Change 215** — a real 2-parent merge (merge feature/s3-ci2 → main): first parent 374fadc
  (main tip), second parent 3e20d44 (feature head). Merge revision = merge commit 2533a353
  (2 parents confirmed via the GitHub API).
- g2p dispatched gerrit-verify (**run 28654010839**, event=`workflow_dispatch`); the CI job
  log confirms it fetched **`refs/changes/15/215/1`** (= the merge commit 2533a353, the merge
  *tree*). All 3 CI matrix jobs passed (ubuntu 3.11/3.12, macos 3.12); `require resolvable
  rebar ticket` passed → **Verified=MAX**. So a merge change gets CI on the actual merge tree
  via the merge refspec.

### AC4 — Verified re-runs on a re-merge; LLM-Review carries (ADR-0025 copyCondition)
Mechanism finding: `MERGE_FIRST_PARENT_UPDATE` requires the feature diff to be **isolated**
from main's churn. A re-merge of a feature branch whose file main also changed is classified
`REWORK` (both votes wiped, full re-review).

- **Change 234** (Change-Id I5b66bf8a, isolated feature file `docs/s3/ac4-marker.txt`):
  - PS1 (merge into main~4): `kind=REWORK` (baseline).
  - PS2 (re-merge into main=463739cd, second parent 312cb95 unchanged): `kind=MERGE_FIRST_PARENT_UPDATE`;
    earned LLM-Review+1 AND Verified+1 (**run 28681531597**).
  - Landed change 236 to advance main → 58a068ca (does not touch the marker file).
  - PS3 (re-merge into main=58a068ca, same second parent 312cb95): `kind=MERGE_FIRST_PARENT_UPDATE`.
    **RESULT: Gerrit "Copied Votes: * LLM-Review+1" — LLM-Review CARRIED**; **Verified was
    REMOVED and CI RE-DISPATCHED (run 28682539519)**. Post-push labels: LLM-Review={+1}, Verified={}.
- REWORK counter-case (**change 215 PS2**, feature file overlapping main churn): BOTH
  LLM-Review+1 AND Verified+1 removed (Gerrit: "approvals got outdated and were removed").

Conclusion: on a `MERGE_FIRST_PARENT_UPDATE` re-merge (first parent moved, reviewed feature
tip unchanged) **LLM-Review carries** (copyCondition includes `MERGE_FIRST_PARENT_UPDATE`)
while **Verified re-runs** (its copyCondition is `NO_CODE_CHANGE` only — a new merge tree must
be re-built). Changing the feature tip → REWORK → both wiped. Exactly the ADR-0025 divergence.

### AC5 — concurrency (cancel-in-progress) with a run example
`gerrit-verify.yaml` sets `concurrency.group = gerrit-verify-${GERRIT_CHANGE_ID}`,
`cancel-in-progress: true` (keyed per Change-Id → all patchsets of one change share the group).
- **Change 234**: PS1 CI dispatched (**run 28681518772**, STARTED). Pushing PS2 while it was
  in-flight → Gerrit "New merge patch set was added with a new first parent relative to Patch
  Set 1"; the PS2 dispatch started **run 28681531597**, and the in-flight PS1 run was
  **CANCELLED** (Gerrit: "Patch Set 1: … CANCELLED: …/runs/28681518772").
- Behavior: a superseded patchset does not keep burning a runner or race a stale Verified;
  only the latest patchset's run proceeds. Because the group key is the Change-Id, it does
  **not** cross-cancel between different changes / a concurrent branch change.
