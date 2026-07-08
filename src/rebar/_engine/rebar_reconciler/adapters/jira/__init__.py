# rebar_reconciler.adapters.jira — the Jira/Atlassian vendor adapter.
#
# Jira-specific reconciler machinery (field sanitization + local↔Jira value maps
# today; the ACLI transport and ADF field mapping in a later phase). This is the
# concrete backend behind the vendor-adapter seam introduced by ticket 44be
# (ambery-tweed-grosbeak) — see ``docs/adr/0035-reconciler-vendor-adapter-seam.md``.
