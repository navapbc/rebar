# Gerrit plugin provenance (Gerrit 3.14.1)

This pins the plugins the rebar review-bot PoC depends on, so installs are
reproducible and auditable. Story S4a (review-bot identity + event plumbing).

The Gerrit host runs the official **`gerritcodereview/gerrit:3.14.1`** image.
Two plugins matter here:

| Plugin       | Version        | Source                                                                                                                                  | sha256                                                             |
|--------------|----------------|-----------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `webhooks`   | 3.14.1 (bundled) | bundled in `gerritcodereview/gerrit:3.14.1` — **no external download** (provenance = the image itself)                                  | n/a (ships inside the image)                                       |
| `events-log` | 3.14.x         | https://gerrit-ci.gerritforge.com/job/plugin-events-log-bazel-master-stable-3.14/lastSuccessfulBuild/artifact/bazel-bin/plugins/events-log/events-log.jar | `46ef4f8741a733251bdbc7ce80fcdc0cb9885aff13e7895e0038c7c52aec565c` |

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

- **Re-pinning.** GerritForge CI publishes the `lastSuccessfulBuild` of the
  `stable-3.14` branch, so the artifact at the URL can advance over time. When it
  does, re-download, recompute the sha256 (`shasum -a 256 events-log.jar`), and
  update the table here in the same commit — the checksum in this file is the
  authority `install-plugins.sh` enforces.
