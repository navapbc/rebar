# Runbook — render the nginx edge config and reload

**Audience:** a human operator with SSM access to the Gerrit host.
**When:** any change to `infra/nginx/rebar.conf.template` has merged to `main`.

## Why this runbook exists

The nginx edge is the ONE deploy surface autodeploy does not apply for you. `infra/scripts/autodeploy.sh`
treats `infra/nginx/rebar.conf.template` as **detect-only** and stops with
`nginx_edge_manual` (auto-apply is a v2 follow-up, epic `6d60-2d0c-6ff7-444b`). So a merge to `main`
changes the file in git and **changes nothing on the box**. Nothing else will tell you.

That gap has already cost real time once: in `renowned-corked-hapuku` a dedicated EBS volume shipped,
was never formatted or mounted, and nobody noticed for weeks because merging looked like landing.
Treat "merged" and "in effect" as two separate facts, and verify the second one.

## Procedure

Run on the Gerrit host (SSM). The template's only substituted variable is `${REVIEW_BOT_PORT}`; the
single-quoted `envsubst` argument is load-bearing — it stops `envsubst` from eating nginx's own
`$host`, `$remote_addr`, `$proxy_add_x_forwarded_for` and friends.

```sh
cd /opt/rebar            # the checkout autodeploy maintains
git fetch origin && git checkout origin/main -- infra/nginx/rebar.conf.template

export REVIEW_BOT_PORT   # the port the review-bot container publishes on loopback
envsubst '${REVIEW_BOT_PORT}' \
  < infra/nginx/rebar.conf.template \
  > /etc/nginx/conf.d/rebar.conf

nginx -t                 # MUST pass before you reload
systemctl reload nginx   # or: nginx -s reload
```

`nginx -t` failing means you have NOT changed the running config — nginx keeps serving the old one.
Fix the template and re-render; do not reload past a failed test.

## Verify it took

Do not trust the reload's exit code alone. Assert on the running config:

```sh
grep -c proxy_read_timeout /etc/nginx/conf.d/rebar.conf     # expect 4 (3x /mcp @3600s, 1x Gerrit @600s)
grep -n proxy_read_timeout /etc/nginx/conf.d/rebar.conf     # confirm the values, not just the count
systemctl is-active nginx                                   # expect: active
curl -sS -o /dev/null -w '%{http_code}\n' https://rebar.solutions.navateam.com/   # expect 200
```

A count of **3** means the render did not happen — the Gerrit `location /` is still on nginx's
compiled-in 60-second read default and bug `5bba-45dd-3bfc-42f1` is still live.

End-to-end check for that bug specifically — a CI-shaped change-ref fetch should no longer 504:

```sh
git init -q --bare /tmp/edgecheck
git --git-dir=/tmp/edgecheck fetch -q https://rebar.solutions.navateam.com/rebar refs/changes/<nn>/<change>/<ps>
echo "rc=$?"          # expect 0; rc=128 with "HTTP 504" means the render did not take
rm -rf /tmp/edgecheck
```

## Rollback

The previous config is whatever the previous commit rendered. To revert, check out the prior revision
of the template, re-render, `nginx -t`, reload. Because the render is a pure function of the template
plus `${REVIEW_BOT_PORT}`, rollback is symmetric with roll-forward — there is no separate undo state.
