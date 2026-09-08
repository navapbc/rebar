# Gerrit object-store repair

Use this runbook when Gerrit rejects a push with `remote unpack failed: error Missing
blob <sha>` or a full connectivity check reports missing objects in
`/var/gerrit/site/git/rebar.git`.

## Safety boundary

- Do not restart Gerrit unless the incident owner explicitly approves it.
- Do not mutate Gerrit's git data before taking an EBS snapshot of the Gerrit data volume.
- Run host-side git repair commands as Gerrit's uid (`1000`, `ec2-user` on the current host)
  or normalize ownership immediately afterwards. Root-owned packs and repo-root metadata
  (`FETCH_HEAD`, `info/refs`, logs) in the live repository are an outage hazard for the
  Gerrit/JGit process that runs as uid 1000.

## Repair

1. Identify the data volume mounted at `/var/gerrit`.
2. Create the backup snapshot first:

   ```sh
   aws ec2 create-snapshot --volume-id "$GERRIT_DATA_VOLUME" \
     --description "pre-repair Gerrit object-store backup $(date -u +%FT%TZ)"
   ```

3. Run `git fetch` from the GitHub mirror into a remote-tracking ref as uid 1000. Do
   not prune or overwrite all live `refs/heads/*`; the repair only needs to import
   missing objects:

   ```sh
   sudo -u ec2-user git --git-dir=/var/gerrit/site/git/rebar.git \
     fetch https://github.com/navapbc/rebar.git \
       refs/heads/main:refs/remotes/github/main
   ```

4. Normalize ownership if any command may have run as root. Normalize the whole bare
   repository, not just `objects/`, because fetch writes repo-root metadata too:

   ```sh
   chown -R 1000:1000 /var/gerrit/site/git/rebar.git
   ```

5. Verify the named object and the whole repository, including change refs and meta refs:

   ```sh
   sudo -u ec2-user git --git-dir=/var/gerrit/site/git/rebar.git cat-file -t "$MISSING_BLOB"
   sudo -u ec2-user git --git-dir=/var/gerrit/site/git/rebar.git \
     for-each-ref --format='%(refname)' refs/heads refs/tags refs/changes refs/meta >/dev/null
   sudo -u ec2-user git --git-dir=/var/gerrit/site/git/rebar.git \
     fsck --full --connectivity-only --no-dangling --strict
   ```

6. Retry the blocked push once. If it fails with another missing object, stop and record the
   new object id and timestamp on the incident ticket before changing anything else.

## Prevention

`infra/compose/jgit.config` is the committed source of truth for Gerrit's JGit settings and
must keep `receive.autogc=false`. `infra/scripts/compose-up.sh` seeds it into
`/var/gerrit/site/etc/jgit.config` on boot. Autodeploy treats changes to this file as
detect-only and emits a manual-apply signal because applying it requires Gerrit operator
judgement.
