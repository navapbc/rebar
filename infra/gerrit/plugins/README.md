# Gerrit plugin provenance (Gerrit 3.14.1)

This pins the plugins the rebar review-bot PoC depends on, so installs are
reproducible and auditable. Story S4a (review-bot identity + event plumbing).

The Gerrit host runs the official **`gerritcodereview/gerrit:3.14.1`** image.
Two plugins matter here:

| Plugin       | Version        | Source                                                                                                                                  | sha256                                                             |
|--------------|----------------|-----------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `webhooks`   | 3.14.1 (bundled) | bundled in `gerritcodereview/gerrit:3.14.1` — **no external download** (provenance = the image itself)                                  | n/a (ships inside the image)                                       |
| `events-log` | 3.14.x         | https://gerrit-ci.gerritforge.com/job/plugin-events-log-bazel-master-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/events-log/events-log.jar | `46ef4f8741a733251bdbc7ce80fcdc0cb9885aff13e7895e0038c7c52aec565c` |
| `gerrit-oauth-provider` (`oauth.jar`) | 3.14.x (b744/WS8) | https://gerrit-ci.gerritforge.com/job/plugin-oauth-bazel-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/oauth/oauth.jar | `2bcf58a652fe5e513d7a4c73362dfc5d9a3dc697f699a5280416ae6f86d0242f` |

## Notes

- **`webhooks` is bundled and already ENABLED** in the `gerritcodereview/gerrit:3.14.1`
  image — there is nothing to download or install. It reads its remote config
  exclusively from each project's `refs/meta/config` (`webhooks.config`); see
  `infra/gerrit/webhooks.config` and `infra/gerrit/service-user.sh`.

- **`events-log` is NOT bundled** and must be downloaded from the recorded
  GerritForge CI URL above and dropped into the Gerrit site `plugins/` dir by
  `infra/gerrit/install-plugins.sh`, which verifies the jar against the sha256
  recorded here (fail-on-mismatch) before installing. It is a pure-Java plugin
  (architecture-independent — the same jar runs on the arm64/Graviton host).
  Verified jar: 208173 bytes, `Gerrit-ApiVersion: 3.14.1-SNAPSHOT`,
  `Gerrit-PluginName: events-log`.

- **`gerrit-oauth-provider` (auth hardening, b744/WS8) is NOT bundled** and is only
  needed when Gerrit is switched from the PoC `DEVELOPMENT_BECOME_ANY_ACCOUNT` to
  `auth.type = OAUTH` (GitHub backend). `install-plugins.sh` downloads the pinned jar
  from the GerritForge CI URL above, verifies the recorded sha256 (fail-on-mismatch,
  same discipline as `events-log`), and drops it in `plugins/oauth.jar`. Gerrit
  registers it under its MANIFEST `Gerrit-PluginName: gerrit-oauth-provider` (the jar
  filename is cosmetic), so the gerrit.config `[plugin
  "gerrit-oauth-provider-github-oauth"]` section binds correctly. Follow
  `infra/runbooks/gerrit-auth-hardening.md` for the full switch (OAuth App, SSM
  client-id/secret, `secure.config`, bot-credential provisioning). Verified jar:
  4,077,624 bytes, `Gerrit-ApiVersion: 3.14.2-SNAPSHOT` (built against the tip of
  `stable-3.14`, API-compatible with the 3.14.1 image — the same rolling-artifact
  posture as `events-log`). Actively maintained (tracks the Gerrit release train);
  GitHub-identity precedent: GerritHub.

  > **CI job name gotcha:** the oauth job is `plugin-oauth-bazel-stable-3.14` — it
  > OMITS the `master` segment that `events-log` uses (`plugin-events-log-bazel-master-stable-3.14`).
  > The `-master-` form 404s for oauth. Job *pages* 403 for everyone; the jar
  > artifact path (200 vs 404) is the real existence signal.

- **Re-pinning.** GerritForge CI publishes the `lastSuccessfulBuild` of the
  `stable-3.14` branch, so the artifact at the URL can advance over time. When it
  does, re-download, recompute the sha256 (`shasum -a 256 events-log.jar`), and
  update the table here in the same commit — the checksum in this file is the
  authority `install-plugins.sh` enforces.
