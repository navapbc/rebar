# The commit-ticket trailer — every commit references a rebar ticket

**Audience: anyone committing code that lands on `main` through this project's review gate,
including consumers who use the `rebar` CLI without a checkout of the rebar repo.** This is the
canonical, install-independent statement of the trailer format. `rebar explain commit-trailer`
prints this file; it is packaged in the wheel, so it resolves from any installation.

## The expected format

Every commit to `main` must reference a rebar ticket that resolves in the ticket store. Add a
`rebar-ticket:` trailer (preferred) or start the subject with the ticket id:

```
Every commit to `main` must reference a rebar ticket. Add a trailer to the commit
message (preferred), or start the subject with the ticket id:

    rebar-ticket: <id>        e.g.  rebar-ticket: blank-guild-koi
  - or a subject prefix -
    <id>: <summary>           e.g.  blank-guild-koi: fix the widget

Accepted <id> forms (resolved against the ticket store):
    alias       blank-guild-koi
    full id     fc9e-8c2e-cb2f-465f
    short id    fc9e-8c2e
    Jira key    REB-310            (project prefix from jira.project)
```

The `rebar-ticket:` trailer is canonical; a leading `<id>:` subject token is accepted as an
alternative. The id can sit anywhere in the message — first-element-*only* is deliberately not
required, since a single store lookup costs the same wherever the id sits, and the trailer keeps
the 50-char subject line free.

## Check your commit before you push

`rebar verify-commit-ticket` is the same check the CI **Verified** gate runs — run it locally to
confirm your commit passes:

```bash
rebar verify-commit-ticket                       # checks HEAD
rebar verify-commit-ticket --rev <ref>           # checks a specific revision
rebar verify-commit-ticket --message-file <path> # checks a message file (e.g. a commit hook)
```

Exit `0` means a candidate id resolved (or the commit is a merge, or the gate is disabled); exit
`1` means nothing resolved and prints this format as the diagnostic; exit `2` is an I/O / store
error (kept distinct so CI infra failures aren't mistaken for a ticketless commit).
`rebar verify-commit-ticket --help` prints the accepted forms too.

## How the id resolves

The id is resolved against the ticket store with the same resolver `rebar show` / `rebar resolve`
use, so **alias, full id, short id, and Jira key** all work identically. A Jira key resolves only
once its ticket is bound in the reconciler's binding store. The store is located by the standard
config precedence (`REBAR_TRACKER_DIR` / `tracker.dir`), so it works per environment without
extra flags.

## See also

- `rebar explain review` — how to pass the code-review gate (the trailer is on its commit
  checklist).
- The full CI wiring, the `verify.require_ticket_for_commit` toggle, and the security notes live
  in the repo docs:
  <https://github.com/navapbc/rebar/blob/main/docs/commit-ticket-trailer.md>.
