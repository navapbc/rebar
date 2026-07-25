# rebar_reconciler.adapters.jira_datacenter — Jira Server / Data Center backend.
#
# A second vendor adapter alongside adapters/jira/ (Cloud). It targets the Jira
# Data Center REST v2 API (/rest/api/2) with Personal Access Token bearer auth,
# and plain-text / wiki-markup issue+comment bodies rather than Cloud's ADF v3.
# The backend-neutral field maps, sanitizers, identity convention, and link
# vocabulary are shared with the Cloud adapter; only the transport, the
# rich-text seam, and user identity (name vs accountId) differ.
