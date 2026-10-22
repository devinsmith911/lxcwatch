#!/usr/bin/env bash
# Install or upgrade lxcwatch in a Debian/Ubuntu LXC (run as root, from a checkout).
# Re-running it upgrades the code and keeps the config, key and database.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/lxcwatch
DATA=/var/lib/lxcwatch
ENVFILE=/etc/lxcwatch.env

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv openssh-client >/dev/null

id lxcwatch >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin lxcwatch
install -d -o lxcwatch -g lxcwatch -m 700 "$DATA"

rm -rf "$APP/src"
install -d "$APP/src"
cp -r "$SRC/lxcwatch" "$SRC/pyproject.toml" "$APP/src/"
[[ -x "$APP/venv/bin/pip" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade "$APP/src"

if [[ ! -f "$DATA/id_ed25519" ]]; then
  runuser -u lxcwatch -- ssh-keygen -q -t ed25519 -N '' -C "lxcwatch@$(hostname)" -f "$DATA/id_ed25519"
fi

if [[ ! -f "$ENVFILE" ]]; then
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  install -m 600 /dev/null "$ENVFILE"
  sed "s|^LXCWATCH_PASSWORD=.*|LXCWATCH_PASSWORD=${password}|" "$SRC/.env.example" >"$ENVFILE"
  echo "Wrote $ENVFILE (web login: admin / ${password})"
fi

install -m 644 "$SRC/deploy/lxcwatch.service" /etc/systemd/system/lxcwatch.service
systemctl daemon-reload

if grep -qE '^LXCWATCH_PVE_HOST=.+' "$ENVFILE"; then
  systemctl enable --now lxcwatch
  systemctl restart lxcwatch
  echo "lxcwatch is running on port 8080."
else
  cat <<EOF

Next steps:
  1. Allow this key to log in as root on your Proxmox nodes:

     $(cat "$DATA/id_ed25519.pub")

  2. Set LXCWATCH_PVE_HOST in $ENVFILE to any node's address.
  3. Run: systemctl enable --now lxcwatch
EOF
fi
