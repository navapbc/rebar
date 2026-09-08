# Vendored Jira Data Center test harness wrapper.
#
# Upstream repository: https://github.com/pycontribs/jira
# Pinned revision: e3c7b167b065660605645cef7f101cbb324772e4
# Upstream source path: docker/jira-test-image/Dockerfile
# Upstream license identifier: BSD-2-Clause
# Stored upstream license: pycontribs-jira-LICENSE.txt
#
# This copy adds `-Dproduct.start.timeout=2700000` and expands the operational
# annotations for base-image provenance, runtime downloads, startup behavior,
# architecture constraints, and ineffective timeout properties.
#
# The upstream `pycontribs/jira-test-image:8.17.1` name is a local build name.
# It is not published to a container registry, so this repository builds the
# vendored wrapper instead of pulling that name or cloning the upstream repository.
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

# Jira is downloaded at runtime (~917 Maven artifacts); the pinned base digest does not pin it.
# Both entrypoint flags are required for a cold start:
#
# - ``skipAllPrompts`` bypasses AMPS's retired Marketplace-v1 SDK-update endpoint.
# - ``product.start.timeout=2700000`` raises Cargo's effective 600000 ms deploy watchdog to
#   cover the download and JVM boot on contended or emulated hosts.
#
# ``startupTimeout`` and ``cargo.timeout`` were tested and do not change that 600000 ms ceiling.
ENTRYPOINT ["atlas-run", "-DskipAllPrompts=true", "-Dproduct.start.timeout=2700000"]
