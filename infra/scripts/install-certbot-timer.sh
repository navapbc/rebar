#!/usr/bin/env bash
# Issue and renew the host-nginx Let's Encrypt certificate (ADR 0007). Install
# certbot from dnf or fall back to /opt/certbot, use the HTTP-01 webroot, and reload
# nginx after issuance and each twice-daily renewal.
#
# Args / env:
#   DOMAIN  (default rebar.solutions.navateam.com)
#   EMAIL   (default joeoakhart@navapbc.com)
# Run as root; issuance and unit installation are idempotent.
set -euo pipefail

DOMAIN="${1:-${DOMAIN:-rebar.solutions.navateam.com}}"
EMAIL="${2:-${EMAIL:-joeoakhart@navapbc.com}}"
# mechanism-ok: env_var WEBROOT — a008-88cf-056e-44fb: render-target seam for certbot
# renewal unit tests; production keeps the host nginx webroot default.
WEBROOT="${WEBROOT:-/var/www/certbot}"
# mechanism-ok: env_var UNIT_DIR — a008-88cf-056e-44fb: render-target seam so tests assert the
# generated certbot-renew.service without writing /etc/systemd/system.
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"

# 1. Ensure nginx and its webroot.
command -v nginx >/dev/null 2>&1 || dnf install -y nginx
systemctl enable --now nginx
mkdir -p "${WEBROOT}"

# 2. Install via dnf or isolated venv.
if ! command -v certbot >/dev/null 2>&1; then
  echo "install-certbot-timer: installing certbot..." >&2
  if dnf install -y certbot python3-certbot-nginx 2>/dev/null; then
    : # dnf path succeeded
  else
    # AL2023 lacks snap; isolate the fallback.
    dnf install -y python3 python3-pip
    python3 -m venv /opt/certbot
    /opt/certbot/bin/pip install --upgrade pip certbot
    ln -sf /opt/certbot/bin/certbot /usr/local/bin/certbot
  fi
fi

# 3. Issue non-interactively through nginx's HTTP-01 webroot.
certbot certonly \
  --webroot -w "${WEBROOT}" \
  -d "${DOMAIN}" \
  --non-interactive --agree-tos -m "${EMAIL}" \
  --keep-until-expiring

# 4. Renew twice daily; reload nginx only after a renewal.
cat >"${UNIT_DIR}/certbot-renew.service" <<'UNIT'
[Unit]
Description=Renew Let's Encrypt certificates (rebar)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env certbot renew --quiet --deploy-hook "systemctl reload nginx"
TimeoutStartSec=600
UNIT

cat >"${UNIT_DIR}/certbot-renew.timer" <<'UNIT'
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

# Serve the newly issued certificate.
nginx -t && systemctl reload nginx

echo "install-certbot-timer: cert issued for ${DOMAIN}; renewal timer active." >&2
