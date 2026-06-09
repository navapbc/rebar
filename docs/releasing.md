# Releasing rebar — updating the distribution channels

rebar ships from one Python package (`nava-rebar` on PyPI) across three channels.
**PyPI is the base; Homebrew and the MCP Registry reference it.** This is the
step-by-step runbook for cutting a release and updating every channel.

## Channels & where they live
- **PyPI** — `nava-rebar` (import pkg `rebar`; commands `rebar` / `rebar-mcp`).
  Published automatically by `.github/workflows/release.yml` on a `vX.Y.Z` tag,
  via **Trusted Publishing (OIDC)** — no token stored anywhere.
- **Homebrew tap** — repo `navapbc/homebrew-rebar`, `Formula/rebar.rb`. Users:
  `brew install navapbc/rebar/rebar`.
- **MCP Registry** — manifest `server.json` (`io.github.navapbc/rebar`),
  published with the `mcp-publisher` CLI.

## Accounts / prerequisites (one-time — already done)
No separate accounts; everything rides on GitHub.
- PyPI **pending publisher** is registered: project `nava-rebar`, repo
  `navapbc/rebar`, workflow `release.yml`, environment `pypi`.
- GitHub **environment `pypi`** exists, restricted to `v*` tags.
- The Homebrew tap repo exists and is public.
- MCP Registry publishing authenticates via `mcp-publisher login github` (your
  GitHub identity; the `io.github.navapbc/*` namespace needs navapbc org access).

## Hard rules
- **PyPI is immutable.** A version can never be re-uploaded or amended (even after
  deletion). Every change ships as a NEW version (you can only *yank* a bad one).
  → metadata fixes (license, etc.) require a version bump.
- Keep `pyproject.toml` `version`, `server.json` `version` + `packages[].version`
  in lockstep, and the Homebrew formula's `url`/`sha256` pointing at the matching
  PyPI sdist.

---

## Release procedure

### 1. Bump versions (one commit)
```bash
# pyproject.toml:  version = "X.Y.Z"
# server.json:     "version": "X.Y.Z"  AND  packages[0].version: "X.Y.Z"
git add pyproject.toml server.json && git commit -m "Release X.Y.Z" && git push origin main
```
(Local sanity: `python -m build && python -m twine check dist/*`.)

### 2. Tag → PyPI publishes automatically
```bash
git tag -a vX.Y.Z -m "nava-rebar X.Y.Z" && git push origin vX.Y.Z
gh run watch "$(gh run list --workflow=release.yml --event=push -L1 --json databaseId -q '.[0].databaseId')" --exit-status
```
The workflow builds + `twine check`s + publishes via OIDC. (A `workflow_dispatch`
run builds without publishing — use it as a dry run.)

Verify:
```bash
curl -s https://pypi.org/pypi/nava-rebar/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
pipx install "nava-rebar==X.Y.Z" --force
```

### 3. Update the Homebrew tap (after PyPI has the new sdist)
```bash
# fetch the new sdist url + sha256
curl -s https://pypi.org/pypi/nava-rebar/X.Y.Z/json | python3 -c "
import json,sys
for u in json.load(sys.stdin)['urls']:
    if u['packagetype']=='sdist': print(u['url']); print(u['digests']['sha256'])"
```
Edit `Formula/rebar.rb` in `navapbc/homebrew-rebar`: update `url` + `sha256`
(and `license` if it changed), then:
```bash
brew style navapbc/rebar/rebar         # after re-tapping / pulling
git commit -am "rebar X.Y.Z" && git push     # in the tap repo
```
The formula installs the base package (zero pip deps → no `resource` blocks). The
MCP server (`rebar-mcp`) needs the `mcp` extra; brew users get it via
`pipx install 'nava-rebar[mcp]'` or `uvx --from nava-rebar[mcp] rebar-mcp`.

### 4. Update the MCP Registry
Prereqs (both already satisfied for this project — listed so they aren't
rediscovered):
- The PyPI release must carry the `mcp-name: io.github.navapbc/rebar` annotation
  (it lives in `README.md` = the PyPI long description; the registry greps it to
  verify package ownership).
- `server.json` `description` must be **≤ 100 characters** (the registry rejects
  longer with HTTP 422 — note PyPI/pyproject have no such limit, so the two
  descriptions can differ).
- To publish under the **org** namespace `io.github.navapbc/*`, your `navapbc`
  GitHub membership must be **public**:
  `gh api --method PUT orgs/navapbc/public_members/<you>` (revert with `DELETE`).

```bash
mcp-publisher login github      # browser/device auth as a navapbc-org GitHub user
mcp-publisher publish           # publishes ./server.json (validates schema first)
```
**Gotcha:** `mcp-publisher` captures your org namespace claims **at login time**.
If you publicize org membership *after* logging in, re-run `mcp-publisher login
github` before `publish`, or you'll get a 403 listing only `io.github.<you>/*`.
Verify: `curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.navapbc/rebar"`.

---

## Known follow-ups (not release-blocking)
- **GitHub Actions Node 20 deprecation** (`actions/checkout@v4`,
  `setup-python@v5`, `upload/download-artifact@v4`) — bump to Node-24-compatible
  versions before GitHub forces the switch (deadline ~2026-06-16).
- **Lock fallback asymmetry** — when util-linux `flock` is absent, the bash write
  paths use a `mkdir` lock while `ticket_txn.py` uses `fcntl.flock`; they don't
  mutually exclude. Pre-existing; unify on a Python-`fcntl` fallback when touched.
