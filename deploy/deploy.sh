#!/bin/bash
# Выкладка закоммиченного HEAD на mvm. /etc/razbor.env и база в /var/lib/razbor не трогаются.
# Использование: deploy/deploy.sh [хост]
set -euo pipefail
HOST=${1:-mvm}
cd "$(dirname "$0")/.."
git archive HEAD razbor static deploy | ssh "$HOST" 'set -e
id razbor >/dev/null 2>&1 || useradd --system --home /opt/razbor --shell /usr/sbin/nologin razbor
rm -rf /opt/razbor.new && mkdir -p /opt/razbor.new && tar -x -C /opt/razbor.new
rm -rf /opt/razbor && mv /opt/razbor.new /opt/razbor && chown -R root:root /opt/razbor
install -m 644 /opt/razbor/deploy/razbor.service /etc/systemd/system/razbor.service
[ -f /etc/nginx/sites-available/razbor ] || install -m 644 /opt/razbor/deploy/razbor.nginx /etc/nginx/sites-available/razbor
ln -sf /etc/nginx/sites-available/razbor /etc/nginx/sites-enabled/razbor
[ -f /etc/razbor.env ] || { echo "нет /etc/razbor.env"; exit 1; }
systemctl daemon-reload && systemctl enable razbor >/dev/null 2>&1 && systemctl restart razbor
nginx -t 2>/dev/null && systemctl reload nginx
sleep 2; systemctl is-active razbor'
