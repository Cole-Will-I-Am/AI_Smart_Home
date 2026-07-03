#!/usr/bin/env bash
# Idempotent installer: code -> /opt/homeops, config skeleton -> /etc/homeops,
# state -> /var/lib/homeops, unit -> systemd. Run as root on the target host.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
id -u homeops &>/dev/null || useradd --system --home /var/lib/homeops --shell /usr/sbin/nologin homeops
install -d -o homeops -g homeops -m 750 /var/lib/homeops
install -d -m 755 /opt/homeops /etc/homeops
cp -r "$SRC/homeops" "$SRC/config" /opt/homeops/
[ -f /etc/homeops/deployment.yaml ] || sed 's#../config/#/opt/homeops/config/#' \
    "$SRC/deploy/deployment.example.yaml" > /etc/homeops/deployment.yaml
if [ ! -f /etc/homeops/secrets.env ]; then
    install -o homeops -g homeops -m 600 "$SRC/deploy/secrets.example.env" /etc/homeops/secrets.env
    echo ">> EDIT /etc/homeops/secrets.env (it is 0600 homeops:homeops; keep it that way)"
fi
install -m 644 "$SRC/deploy/homeops.service" /etc/systemd/system/homeops.service
systemctl daemon-reload
echo ">> next: edit /etc/homeops/{deployment.yaml,secrets.env}"
echo ">> then: python3 -m homeops.cli validate /etc/homeops/deployment.yaml"
echo ">>       python3 -m homeops.cli preflight /etc/homeops/deployment.yaml   # read-only"
echo ">>       systemctl enable --now homeops"
