#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-certbot-timer.sh — issue + auto-renew the Let's Encrypt cert (story S2).
#
# APPROACH (decided, per ADR-0007): nginx runs as a HOST PACKAGE (dnf nginx), NOT a
# container, so certbot can manage the cert in place and reload nginx with a simple
# `systemctl reload nginx`. The compose stack therefore runs ONLY gerrit + review-bot;
# nginx proxies to their loopback-published ports (8080 / REVIEW_BOT_PORT).
#
# certbot install: AL2023 does not have snap. We install certbot via dnf (it is
# available in the AL2023 repos / EPEL-equivalent); if the dnf package is absent we
# fall back to a pip venv at /opt/certbot. Either way `certbot` ends up on PATH.
#
# Issuance: HTTP-01 via the nginx webroot /var/www/certbot (the port-80 server block
# in infra/nginx/rebar.conf.template serves /.well-known/acme-challenge/ from there).
# Renewal: a systemd timer runs `certbot renew` twice daily with a deploy hook that
# reloads nginx, so a renewed cert is picked up without manual intervention.
#
# Args / env:
#   DOMAIN  (default rebar.solutions.navateam.com)
#   EMAIL   (default joeoakhart@navapbc.com)
# Run from anywhere as root. Idempotent (certbot is a no-op if the cert is current;
# the timer install overwrites its unit files).
# ---------------------------------------------------------------------------
set -euo pipefail

DOMAIN="${1:-${DOMAIN:-rebar.solutions.navateam.com}}"
EMAIL="${2:-${EMAIL:-joeoakhart@navapbc.com}}"
WEBROOT="/var/www/certbot"

# --- 1. Ensure host nginx + the ACME webroot exist -------------------------
command -v nginx >/dev/null 2>&1 || dnf install -y nginx
systemctl enable --now nginx
mkdir -p "${WEBROOT}"

# --- 2. Install certbot (dnf, else a pip venv) -----------------------------
if ! command -v certbot >/dev/null 2>&1; then
  echo "install-certbot-timer: installing certbot..." >&2
  if dnf install -y certbot python3-certbot-nginx 2>/dev/null; then
    : # dnf path succeeded
  else
    # Fallback: isolated pip venv (no snap on AL2023).
    dnf install -y python3 python3-pip
    python3 -m venv /opt/certbot
    /opt/certbot/bin/pip install --upgrade pip certbot
    ln -sf /opt/certbot/bin/certbot /usr/local/bin/certbot
  fi
fi

# --- 3. Initial issuance via the webroot -----------------------------------
# --webroot writes the challenge token under ${WEBROOT}; nginx serves it on :80.
# Non-interactive + agree-tos for an unattended boot. No-op if a valid cert exists.
certbot certonly \
  --webroot -w "${WEBROOT}" \
  -d "${DOMAIN}" \
  --non-interactive --agree-tos -m "${EMAIL}" \
  --keep-until-expiring

# --- 4. systemd timer: renew twice daily, reload nginx on renewal ----------
# A oneshot service + a timer firing 00:00 and 12:00 (with randomized delay).
# --deploy-hook fires ONLY when a cert is actually renewed, reloading host nginx.
cat >/etc/systemd/system/certbot-renew.service <<'UNIT'
[Unit]
Description=Renew Let's Encrypt certificates (rebar)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env certbot renew --quiet --deploy-hook "systemctl reload nginx"
UNIT

cat >/etc/systemd/system/certbot-renew.timer <<'UNIT'
[Unit]
Description=Run certbot renew twice daily (rebar)

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=3600
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now certbot-renew.timer

# Reload nginx now so it serves the freshly-issued cert.
nginx -t && systemctl reload nginx

echo "install-certbot-timer: cert issued for ${DOMAIN}; renewal timer active." >&2
