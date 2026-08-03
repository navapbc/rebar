#!/usr/bin/env bash
# Transitional exec shim (ticket f1eb): the probe is now scripts/probe_rebar.py.
# The Gerrit Verified gate runs gerrit-verify.yaml from MAIN's workflow definition
# against the patchset tree, so main's golden-path step still invokes this path
# until the re-pointed workflow (in this same change) lands on main. Ticket 51b2
# tracks deleting this shim afterwards.
exec python "$(dirname "$0")/probe_rebar.py" "$@"
