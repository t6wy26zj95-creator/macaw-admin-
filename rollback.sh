#!/usr/bin/env bash
# /opt/macaw-admin/rollback.sh <NEW_IP>
# Undoes a failed switchover: stops services on NEW_IP, points DuckDNS back here,
# starts everything here again. Run by bot.py as root.
set -uo pipefail
NEW="${1:?usage: rollback.sh NEW_IP}"
set -a; source /opt/macaw-admin/.env; set +a
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new"

echo "##STEP 1/3 Stopping services on $NEW"
stopped=no
for _ in 1 2 3; do
  if ssh -n $SSH_OPTS root@"$NEW" 'systemctl disable --now macaw macaw-tunnel macaw-admin 2>/dev/null
       if [ -f /opt/macaw-player/docker-compose.yml ]; then cd /opt/macaw-player && docker compose stop; fi; true'; then
    stopped=yes; break
  fi
  sleep 10
done

echo "##STEP 2/3 DuckDNS -> this server"
# With an empty ip= DuckDNS uses the caller's address, which is this server anyway.
OLD=$(curl -4 -s --max-time 10 https://api.ipify.org || true)
r=$(curl -4 -s --max-time 20 "https://www.duckdns.org/update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip=$OLD" || true)
echo "DuckDNS: ${r:-no answer}"

echo "##STEP 3/3 Starting services here"
systemctl enable --now macaw macaw-tunnel
(cd /opt/macaw-player && docker compose up -d)
sleep 15
srs=$(systemctl is-active macaw macaw-tunnel | paste -sd, -)
players=$(cd /opt/macaw-player && docker compose ps --status running -q | wc -l)
echo "##RESULT newstopped=$stopped duckdns=${r:-none} srs=$srs player=$players"

problems=""
[ "$r" = OK ] || problems="$problems DuckDNS replied '${r:-nothing}';"
[ "$srs" = "active,active" ] || problems="$problems SRS bot/tunnel: $srs;"
[ "$players" -eq 3 ] || problems="$problems $players of 3 player containers running;"
[ -z "$problems" ] || { echo "##FAIL$problems"; exit 1; }
echo "##DONE"
