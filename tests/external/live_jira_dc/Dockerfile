# Vendored Jira Data Center test harness (story J5 / epic e369).
#
# WHY VENDORED, NOT PULLED: `pycontribs/jira-test-image:8.17.1` is NOT a pullable
# image — it is a LOCAL BUILD NAME. `docker pull pycontribs/jira-test-image:8.17.1`
# fails with "pull access denied ... repository does not exist", and the Docker Hub
# API for that repo returns `{"message": "object not found"}`. The `pycontribs`
# namespace holds only OS base images; there is no jira-test-image there. That tag
# is the name the pycontribs/jira project's own README tells YOU to build locally
# (`docker build -t pycontribs/jira-test-image:8.17.1 docker/jira-test-image`). So
# this file is a vendored reproduction of that upstream Dockerfile (attribution:
# pycontribs/jira, docker/jira-test-image/Dockerfile), built here rather than
# pulled from anywhere or cloned from another repo at test time.
#
# The base IS pullable and is pinned by DIGEST (not tag), so the base layer never
# drifts under us even if the `addono/jira-software-standalone` tag moves.
FROM addono/jira-software-standalone@sha256:04326628dc4ac36b2bfc1d0f2ebe5ba3807c1ec9cf9b18307d3c2ad7222537e9

# Overridable across 8.x patch versions ONLY. 9+/11+ fail Maven resolution because
# the upstream pom this image runs (`atlas-run`) is anchored to the 8.x dependency
# graph — see README.md "Version constraints" for the detail. Do not bump this past
# 8.x expecting it to track a client's newer Jira.
ARG JIRA_VERSION=8.17.1
ENV JIRA_VERSION=${JIRA_VERSION}

EXPOSE 2990

# NOTE: this image does NOT contain Jira. `atlas-run` downloads Jira ${JIRA_VERSION}
# (~917 artifacts) from maven.atlassian.com on first container start — see README.md
# "Runtime dependency on maven.atlassian.com". Pinning this Dockerfile's base digest
# does NOT pin Jira itself.
#
# Every flag below is here because removing it was independently proven to break a
# cold start — see rebar ticket c97f-90ae-4976-47c7 comments for the exact failure
# logs this was verified against.
#
#   -DskipAllPrompts=true
#       MANDATORY. Atlassian retired the Marketplace v1 REST endpoint that AMPS's
#       SDK-update check hardcodes, so WITHOUT this flag every cold container start
#       fails with FileNotFoundException on
#       https://marketplace.atlassian.com/rest/1.0/plugins/... . The failure looks
#       like a transient network fault, which is exactly why a maintainer might be
#       tempted to "clean up" this flag — don't.
#
#   -Dproduct.start.timeout=2700000
#       MANDATORY. Cargo's own deploy watchdog (jira-maven-plugin's `run` goal) has
#       a HARDCODED DEFAULT CEILING OF 600000 ms (10 minutes). Without this
#       override, the container dies ~25 minutes into a cold start with:
#         DeployerWatchdog: Deployable [http://localhost:2990/cargocpc/index.html]
#         failed to finish deploying within the timeout period [600000]
#       Jira has, at that point, genuinely booted (Tomcat + H2 + plugin system
#       starting) — it is Cargo's own watchdog that kills it, not a real hang. This
#       property name is the one that actually works; it was found in Atlassian's
#       own docs after two other names were tried and verified to have NO EFFECT
#       (see below). 2700000 ms (45 min) covers the ~900-artifact Maven download
#       plus JVM boot even on an emulated/contended host.
#
#   -DstartupTimeout / -Dcargo.timeout — DO NOT USE.
#       These look like the obvious property names and are NOT. Both were tried and
#       BOTH failed identically to no override at all — the container still died at
#       the same "timeout period [600000]" line. Recorded here so nobody re-derives
#       this by burning another ~25-minute run.
ENTRYPOINT ["atlas-run", "-DskipAllPrompts=true", "-Dproduct.start.timeout=2700000"]
