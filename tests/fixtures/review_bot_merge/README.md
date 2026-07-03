# review-bot merge-change fixtures (epic 88ab / S2)

Live-captured from the running Gerrit 3.14.1 (change 183, a real 2-parent merge revision)
on 2026-07-02, XSSI prefix stripped. They prove the "riskiest assumption" (AC#1): that
`GET /changes/{id}/revisions/{rev}/files` with NO `base`/`parent` param returns the
AUTO-MERGE-BASE file map for a merge commit (it does NOT 409 like `/patch`).

Reference: Gerrit REST API — rest-api-changes.html#list-files:
"If the revision is a merge commit and neither base nor parent is set, the list of files is
computed against the auto-merge." A clean merge (no conflict) yields only the magic
pseudo-paths `/COMMIT_MSG` + `/MERGE_LIST` (empty real delta).

- merge_commit_clean.json  — CommitInfo (2 parents => merge detection)
- merge_files_clean.json   — files map (no parent): only magic paths (clean merge)
- mergelist_clean.json     — integrated-commit list
- merge_diff_mergelist.json— per-file DiffInfo shape (the /MERGE_LIST pseudo-file)
