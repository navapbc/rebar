# Runbook — Gerrit outbound mail (currently DISABLED)

Outbound email on the rebar Gerrit box is **deliberately off**:
`infra/compose/gerrit.config` carries `[sendemail] enable = false`. This runbook
records why, what that costs, how to apply a change to it, and exactly what to do
if we ever need mail back.

Decision owner: bug `1630-0279-85ba-4e15` (wetproof-bronzy-woodpecker).

## The decision, and why

An **absent** `[sendemail]` section is not the same as "no mail". Gerrit's
`SmtpEmailSender` defaults `smtpServer` to `127.0.0.1`, and (for
`smtpEncryption` `none`/`tls`) the port to `25`. With no section at all Gerrit
dialled `127.0.0.1:25` on every notification. The official Gerrit image ships no
MTA, so nothing listened there, and every review comment, merge, new patchset and
abandon logged a multi-line stack trace:

```
com.google.gerrit.exceptions.EmailException: Mail Error: Connection refused
    at com.google.gerrit.server.mail.send.SmtpEmailSender.open(SmtpEmailSender.java:457)
```

That produced **4,119** such errors between 2026-06-30 and 2026-07-30 — the
largest signature in `error_log` by an order of magnitude — which actively buried
real errors and measurably slowed a log audit.

`enable = false` is the upstream-documented remedy for exactly this situation:

> "If false Gerrit will not send email messages, for any reason, and all other
> properties of section sendemail are ignored."
> — <https://gerrit-review.googlesource.com/Documentation/config-gerrit.html#sendemail>

The flag was introduced for installations that "might be unable to connect to a
SMTP relay, but are still useful through the web page UI, provided that reviewers
check their dashboard periodically" — which describes this deployment, whose
review flow is bot-driven (`LLM-Review` + `Verified`) and where no human relies on
Gerrit email.

The mainstream community image behaves the same way: `openfrontier/docker-gerrit`
sets `sendemail.enable false` automatically when no SMTP server is configured.

## What this costs

Lost:

- **All notifications** — review/comment/merge mail, `user-notify` watches,
  project watches, abandon/restore, and the review-bot's own mail. Reviewers must
  use the dashboard.
- **Self-service email registration/verification.** Adding a new or secondary
  address sends a verification link, so that flow fails. (It already failed
  before this change, with the same `EmailException`.)
  - Admin workaround: `PUT /accounts/{id}/emails/{email}` with
    `no_confirmation: true`. Only Gerrit administrators may add an address
    without confirmation.
    <https://gerrit-review.googlesource.com/Documentation/rest-api-accounts.html#create-email>

**Not** affected: git push/fetch, the review UI, SSH/HTTP API, CI integration,
label voting, submit, replication, or *inbound* mail (`[receiveemail]` is a
separate section and is untouched).

## Applying a change to gerrit.config (this or any other)

Two things make this non-obvious, and both bit us:

1. **autodeploy does not restart Gerrit.** The loop's `BOT_SERVICE` is
   documented "NEVER 'gerrit'". `infra/compose/gerrit.config` is therefore listed
   in `CONFIG_PATHS` in `infra/scripts/autodeploy.sh` — **detect-only**: landing a
   change emits `AUTODEPLOY_ERROR reason=config_manual` so the pending manual
   apply is visible, and does not apply it.
2. **`sendemail` is not hot-reloadable.** Gerrit reads it at injector-creation
   time, so the container must restart.

Operator apply (supervised; this briefly interrupts the review gate, so do it
when no change is mid-submit):

```sh
# On the box (SSM), as root:
cd /opt/rebar
infra/scripts/compose-up.sh          # re-seeds site etc/gerrit.config from the repo
docker compose -f infra/compose/docker-compose.yml restart gerrit
```

Verify — the count must not grow after the restart:

```sh
grep -c 'Cannot email' /var/gerrit/site/logs/error_log
# then exercise it: comment on any open change, wait ~30s, re-run. Count unchanged = fixed.
```

## If we ever need mail back

Two credible paths. Neither is a one-line change; budget accordingly.

### Option A — direct to Amazon SES

```ini
[sendemail]
    enable = true
    smtpServer = email-smtp.us-east-1.amazonaws.com
    smtpServerPort = 587
    smtpEncryption = tls
    smtpUser = AKIA................
    sslVerify = true
    from = MIXED
```

`smtpPass` goes in `etc/secure.config` (mode 0600), **never** in `gerrit.config`.

Hard prerequisites, each a blocker:

- **SES sandbox** is per-Region: verified recipients only, 200 msgs/24h, 1/sec,
  until production access is granted (AWS Support round-trip).
- **Verify the sender identity in us-east-1** (domain identity + Easy DKIM, up to
  72h DNS propagation). Identities are per-Region.
- **SES SMTP credentials are NOT IAM access keys** and, critically,
  <https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html> states the SES
  SMTP interface **does not support credentials derived from temporary security
  credentials**. Our EC2 instance profile yields exactly those, so SES SMTP
  requires a **long-lived IAM user access key** — it cannot reuse the
  `rebar-gerrit-instance-role` pattern that `ci-gerrit-ssh-key` and
  `g2p-github-pat` use. Store the password in SSM SecureString and materialize
  `secure.config` at boot; the credential itself is still static.
- **EC2 blocks outbound port 25** to public addresses. Use 587.

### Option B — local relay to SES

Run a Postfix/msmtp satellite (host-side with `network_mode: host`, or a compose
sidecar) and point Gerrit at `smtpServer = localhost`. This is what both OpenDev
and Wikimedia actually do for their large Gerrit instances (with direct-delivery
exim rather than SES). Buys queueing and retry, and keeps the SES credential out
of Gerrit's config; costs another moving part. The stock Gerrit container has no
MTA, so a relay must be added explicitly — do **not** expect `localhost:25` to
work on its own.

## Observability

There is no CloudWatch alarm on Gerrit mail failures, and deliberately so while
mail is disabled: with `enable = false` the failure mode cannot occur. If mail is
ever re-enabled, add an `email_errors` counter to
`infra/scripts/observability.sh` (namespace `rebar/host`, alongside
`voter_errors` / `deploy_errors` / `g2p_dispatch_errors`) and an alarm, or this
class of failure will again be invisible for a month.
