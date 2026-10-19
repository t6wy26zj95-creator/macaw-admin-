#!/usr/bin/env bash
# /opt/macaw-admin/migrate.sh <NEW_IP>
# Pushes every Macaw service from this server to NEW_IP. Run by bot.py as root.
# Protocol on stdout: ##STEP n/8 text, ##NEEDPASS, ##FAIL text, ##RESULT k=v ..., ##DONE
set -euo pipefail
export HOME=/root  # systemd services get no HOME; ssh-copy-id needs ~/.ssh
NEW="${1:?usage: migrate.sh NEW_IP}"
set -a; source /opt/macaw-admin/.env; set +a
[ -n "${ROOT_PASSWORD_OVERRIDE:-}" ] && ROOT_PASSWORD="$ROOT_PASSWORD_OVERRIDE"
DOMAIN="$DUCKDNS_DOMAIN.duckdns.org"
step(){ echo "##STEP $*"; }
fail(){ echo "##FAIL $*"; exit 1; }
# Any command that fails without an explicit fail() still produces a ##FAIL line.
trap 'rc=$?; echo "##FAIL Unexpected error on line $LINENO (code $rc), see the log"' ERR
APT='apt-get -o DPkg::Lock::Timeout=300 -y -qq'
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"
new(){ local rc i; for i in 1 2 3; do ssh -n $SSH_OPTS root@"$NEW" "$@" && return 0; rc=$?; [ $rc -ne 255 ] && return $rc; sleep 10; done; return 255; }
# --numeric-ids: container data must keep its numeric owners, not be remapped by user name.
push(){ local i; for i in 1 2 3; do rsync -aH --numeric-ids --delete -e "ssh $SSH_OPTS" "$1" "root@$NEW:$2" && return 0; sleep 10; done; return 1; }

step "1/8 Connecting to $NEW"
for i in $(seq 6); do
  timeout 10 bash -c "</dev/tcp/$NEW/22" 2>/dev/null && break
  [ "$i" = 6 ] && fail "Port 22 on $NEW is unreachable"; sleep 10
done

step "2/8 SSH key"
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -q
ssh-keygen -R "$NEW" >/dev/null 2>&1 || true
if ! ssh -n $SSH_OPTS root@"$NEW" true 2>/dev/null; then
  command -v sshpass >/dev/null || { $APT update >/dev/null; $APT install sshpass >/dev/null; }
  # Which login methods does the server offer? (no password sent here)
  methods=$(ssh -n -o BatchMode=yes -o PreferredAuthentications=none -o ConnectTimeout=30 \
    -o StrictHostKeyChecking=accept-new root@"$NEW" true 2>&1 | grep -o 'Permission denied (.*)' || true)
  echo "server login methods: ${methods:-unknown}"
  case "$methods" in
    *password*|*keyboard-interactive*|"") ;;
    *) fail "The new server doesn't accept password logins for root ($methods). Enable them in the hosting panel, or reinstall the server with a root password, then tap Retry" ;;
  esac
  rc=0
  out=$(SSHPASS="$ROOT_PASSWORD" timeout 120 sshpass -e ssh-copy-id -i /root/.ssh/id_ed25519.pub \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 root@"$NEW" 2>&1) || rc=$?
  printf '%s\n' "$out" | sed 's/^/  ssh-copy-id: /'
  echo "ssh-copy-id exit code: $rc"
  [ $rc -eq 124 ] && fail "Password login to $NEW timed out"
  [ $rc -eq 6 ] && fail "Host key problem for $NEW"
  if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qiE 'expired|must change|change your password|new password|current password'; then
    fail "The new server demands a password change on first login. Log in once (hosting panel console or SSH), set the standard password, then tap Retry"
  fi
  # sshpass code 5 = the server rejected the password.
  [ $rc -eq 5 ] && { echo "##NEEDPASS"; exit 3; }
  [ $rc -ne 0 ] && fail "Password login to $NEW failed (code $rc), see the log"
  new true || fail "Key installed, but key login doesn't work"
fi

step "3/8 Checking the new server"
new 'test -x /usr/bin/python3.12' || fail "No /usr/bin/python3.12: Ubuntu 24.04 required"
# On a Retry, our own services from the previous attempt may be running there and hold the ports.
new 'systemctl stop macaw macaw-tunnel macaw-admin 2>/dev/null
  if [ -f /opt/macaw-player/docker-compose.yml ] && docker compose version >/dev/null 2>&1; then
    cd /opt/macaw-player && docker compose stop >/dev/null 2>&1
  fi; true' || true
for p in 80 8443; do
  if new "ss -ltnH 'sport = :$p' | grep -q ."; then fail "Port $p is busy on the new server"; fi
done
free=$(new "df --output=avail -BG / | tail -1 | tr -dc 0-9") || fail "Could not check disk space on the new server"
[ "${free:-0}" -ge 5 ] || fail "Not enough disk space on the new server: ${free}G"

step "4/8 Packages"
command -v rsync >/dev/null || $APT install rsync >/dev/null
new "set -e; export DEBIAN_FRONTEND=noninteractive; $APT update; $APT install rsync curl sshpass >/dev/null
  if ! command -v docker >/dev/null; then
    $APT install docker.io docker-compose-v2 >/dev/null && systemctl enable --now docker
  elif ! docker compose version >/dev/null 2>&1; then
    if dpkg -s docker-ce >/dev/null 2>&1; then $APT install docker-compose-plugin >/dev/null
    else $APT install docker-compose-v2 >/dev/null; fi
  fi
  docker compose version >/dev/null
  if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then ufw allow 80/tcp; ufw allow 8443/tcp; fi" \
  || fail "Failed to install Docker/Compose/rsync"

step "5/8 Pre-copy (services still running here)"
TUSER=$(grep -Po '^User=\K.*' /etc/systemd/system/macaw-tunnel.service || true)
TGROUP=$(grep -Po '^Group=\K.*' /etc/systemd/system/macaw-tunnel.service || true)
new "getent group macaw >/dev/null || groupadd --system macaw
  id macaw >/dev/null 2>&1 || useradd --system --gid macaw --home-dir /opt/macaw --no-create-home --shell /usr/sbin/nologin macaw
  if [ -n '$TGROUP' ] && ! getent group '$TGROUP' >/dev/null; then groupadd --system '$TGROUP'; fi
  if [ -n '$TUSER' ] && ! id '$TUSER' >/dev/null 2>&1; then useradd --system --no-create-home --shell /usr/sbin/nologin '$TUSER'; fi
  mkdir -p /opt/macaw /opt/macaw-player /opt/macaw-admin" || fail "Failed to create users on the new server"
push /opt/macaw/ /opt/macaw/ || fail "Copying /opt/macaw failed"
push /opt/macaw-player/ /opt/macaw-player/ || fail "Copying /opt/macaw-player failed"
push /opt/macaw-admin/ /opt/macaw-admin/ || fail "Copying /opt/macaw-admin failed"
for f in macaw.service macaw-tunnel.service macaw-admin.service; do
  push /etc/systemd/system/$f /etc/systemd/system/$f || fail "Copying $f failed"
done
push /usr/bin/cloudflared /usr/bin/cloudflared || fail "Copying cloudflared failed"
VOLS=$(docker volume ls -q | grep '^macaw_' || true)
new "cd /opt/macaw-player && docker compose build -q && docker compose create >/dev/null" \
  || fail "Player build failed on the new server"
for v in $VOLS; do
  new "docker volume inspect $v >/dev/null 2>&1 || docker volume create $v >/dev/null" || fail "Creating volume $v failed"
  push /var/lib/docker/volumes/$v/_data/ /var/lib/docker/volumes/$v/_data/ || fail "Copying volume $v failed"
done

step "6/8 Switchover (bots offline for 1-3 minutes)"
systemctl stop macaw macaw-tunnel || fail "Could not stop the SRS bot/tunnel here"
(cd /opt/macaw-player && docker compose stop) || fail "Could not stop the player here"
push /opt/macaw/ /opt/macaw/ || fail "Final copy of /opt/macaw failed"
push /opt/macaw-player/ /opt/macaw-player/ || fail "Final copy of /opt/macaw-player failed"
for v in $VOLS; do
  push /var/lib/docker/volumes/$v/_data/ /var/lib/docker/volumes/$v/_data/ || fail "Final copy of volume $v failed"
done
push /opt/macaw-admin/ /opt/macaw-admin/ || fail "Final copy of /opt/macaw-admin failed"
new "chown -R macaw:macaw /opt/macaw && systemctl daemon-reload \
  && systemctl enable --now macaw macaw-tunnel && systemctl enable macaw-admin" \
  || fail "Failed to start the SRS bot/tunnel on the new server"

step "7/8 DuckDNS -> $NEW"
r=$(curl -s --max-time 20 "https://www.duckdns.org/update?domains=$DUCKDNS_DOMAIN&token=$DUCKDNS_TOKEN&ip=$NEW" || true)
[ "$r" = OK ] || fail "DuckDNS replied: ${r:-no answer}"
for i in $(seq 36); do
  [ "$(new "getent ahostsv4 $DOMAIN | awk 'NR==1{print \$1}'" || true)" = "$NEW" ] && break; sleep 5
done
new "cd /opt/macaw-player && docker compose up -d" || fail "Player failed to start"

step "8/8 Checks"
sleep 20
new 'systemctl is-active macaw macaw-tunnel' >/dev/null || fail "SRS bot or tunnel is not active on the new server"
ok=0
for i in $(seq 6); do
  new 'cd /opt/macaw-player && [ "$(docker compose ps --status running -q | wc -l)" -eq 3 ]' && { ok=1; break; }
  sleep 10
done
[ $ok = 1 ] || fail "Not all 3 player containers are running"
code=000
for i in $(seq 6); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
    --resolve "$DOMAIN:8443:$NEW" "https://$DOMAIN:8443" || true)
  [ "$code" != 000 ] && break; sleep 10
done
[ "$code" != 000 ] || fail "Player doesn't respond over HTTPS"
TUN=""
for i in $(seq 12); do
  TUN=$(new "curl -s --max-time 5 127.0.0.1:8091/quicktunnel" | grep -Po '"hostname":"\K[^"]+' || true)
  [ -n "$TUN" ] && break; sleep 5
done
# Remember where we came from, so the new admin bot can offer to wipe it.
[ -n "${MIGRATE_FROM:-}" ] && { new "echo '$MIGRATE_FROM' >> /opt/macaw-admin/old-servers" || true; }
echo "##RESULT player=$code tunnel=${TUN:-?} duckdns=OK"
echo "##DONE"
