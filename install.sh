#!/usr/bin/env bash
# Macaw admin bot installer / updater. Run as root on the current server:
#   curl -fsSL https://raw.githubusercontent.com/t6wy26zj95-creator/macaw-admin-/main/install.sh | bash
# First run asks for the secrets and writes /opt/macaw-admin/.env.
# Later runs keep .env and just update the code and restart the bot.
# The whole script sits in { } so bash reads it fully before running anything
# (with curl | bash, a command that reads stdin would otherwise eat the rest).
{
set -euo pipefail
REPO_RAW="https://raw.githubusercontent.com/t6wy26zj95-creator/macaw-admin-/main"
DIR=/opt/macaw-admin

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
# stdin is the piped script, so questions are asked on the terminal directly.
[ -r /dev/tty ] || { echo "No terminal to ask questions on. Run it from an SSH session."; exit 1; }

echo "== Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=300 update -qq
apt-get -o DPkg::Lock::Timeout=300 install -y -qq python3-venv sshpass rsync curl >/dev/null

echo "== Downloading the bot"
mkdir -p "$DIR/logs"
tmp=$(mktemp -d)
for f in bot.py migrate.sh rollback.sh; do
  curl -fsSL "$REPO_RAW/$f" -o "$tmp/$f"
done
install -m 644 "$tmp/bot.py" "$DIR/bot.py"
install -m 700 "$tmp/migrate.sh" "$DIR/migrate.sh"
install -m 700 "$tmp/rollback.sh" "$DIR/rollback.sh"
rm -rf "$tmp"

# Single-quote a value so `source .env` keeps $, spaces and quotes as they are.
sq(){ local s=${1//\'/\'\\\'\'}; printf "'%s'" "$s"; }
ask(){ local v=""; while [ -z "$v" ]; do read -r -p "$1: " v </dev/tty; done; printf %s "$v"; }

env_ok(){
  [ -f "$DIR/.env" ] || return 1
  ( set +u; set -a; source "$DIR/.env"
    for v in BOT_TOKEN OWNER_ID ROOT_PASSWORD DUCKDNS_DOMAIN DUCKDNS_TOKEN; do [ -n "${!v:-}" ] || exit 1; done
    [[ "$OWNER_ID" =~ ^[0-9]+$ ]] ) 2>/dev/null
}

if env_ok; then
  echo "== Keeping the existing $DIR/.env"
else
  echo
  echo "== Secrets (stored only in $DIR/.env, readable by root only)"
  BOT_TOKEN=$(ask "Admin bot token from @BotFather")
  OWNER_ID=""
  until [[ "$OWNER_ID" =~ ^[0-9]+$ ]]; do OWNER_ID=$(ask "Your numeric Telegram ID (from @userinfobot)"); done
  while :; do
    read -r -s -p "Root password for all servers: " P1 </dev/tty; echo
    read -r -s -p "Same password again: " P2 </dev/tty; echo
    [ -n "$P1" ] && [ "$P1" = "$P2" ] && break
    echo "Empty or different, try again."
  done
  DUCKDNS_TOKEN=$(ask "DuckDNS token (top of duckdns.org after login)")
  read -r -p "DuckDNS subdomain [macawp3]: " DOMAIN </dev/tty
  umask 077
  cat > "$DIR/.env" <<EOF
BOT_TOKEN=$(sq "$BOT_TOKEN")
OWNER_ID=$OWNER_ID
ROOT_PASSWORD=$(sq "$P1")
DUCKDNS_DOMAIN=$(sq "${DOMAIN:-macawp3}")
DUCKDNS_TOKEN=$(sq "$DUCKDNS_TOKEN")
EOF
  unset P1 P2
fi
chmod 600 "$DIR/.env"

echo "== Python environment"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --disable-pip-version-check 'aiogram>=3.13,<4'

echo "== systemd unit"
cat > /etc/systemd/system/macaw-admin.service <<'UNIT'
[Unit]
Description=Macaw admin bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/macaw-admin
ExecStart=/opt/macaw-admin/venv/bin/python /opt/macaw-admin/bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable macaw-admin >/dev/null 2>&1
systemctl restart macaw-admin
sleep 5
if systemctl is-active --quiet macaw-admin; then
  echo
  echo "Done. The admin bot is running and should message you in Telegram now."
  echo "No message? Open the bot, send /start, and check: journalctl -u macaw-admin -n 50"
else
  echo "The admin bot failed to start. Last log lines:"
  journalctl -u macaw-admin -n 30 --no-pager
  echo "To re-enter the secrets: rm $DIR/.env and run the command again."
  exit 1
fi
exit 0
}
