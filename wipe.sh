#!/usr/bin/env bash
# /opt/macaw-admin/wipe.sh <OLD_IP>
# Deletes every Macaw service, its data and tokens from an old server.
# Never touches Amnezia (/opt/amnezia, amnezia-* containers) or Docker itself.
# Run by bot.py as root. Same stdout protocol as migrate.sh.
set -euo pipefail
export HOME=/root
OLD="${1:?usage: wipe.sh OLD_IP}"
set -a; source /opt/macaw-admin/.env; set +a
[ -n "${ROOT_PASSWORD_OVERRIDE:-}" ] && ROOT_PASSWORD="$ROOT_PASSWORD_OVERRIDE"
DOMAIN="$DUCKDNS_DOMAIN.duckdns.org"
step(){ echo "##STEP $*"; }
fail(){ echo "##FAIL $*"; exit 1; }
trap 'rc=$?; echo "##FAIL Unexpected error on line $LINENO (code $rc), see the log"' ERR
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new"
old(){ local rc i; for i in 1 2 3; do ssh -n $SSH_OPTS root@"$OLD" "$@" && return 0; rc=$?; [ $rc -ne 255 ] && return $rc; sleep 10; done; return 255; }

step "1/4 Connecting to $OLD"
for i in $(seq 6); do
  timeout 10 bash -c "</dev/tcp/$OLD/22" 2>/dev/null && break
  [ "$i" = 6 ] && fail "Port 22 on $OLD is unreachable"; sleep 10
done

step "2/4 Logging in"
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -q
ssh-keygen -R "$OLD" >/dev/null 2>&1 || true
if ! ssh -n $SSH_OPTS root@"$OLD" true 2>/dev/null; then
  command -v sshpass >/dev/null || { apt-get -y -qq update >/dev/null; apt-get -y -qq install sshpass >/dev/null; }
  rc=0
  out=$(SSHPASS="$ROOT_PASSWORD" timeout 120 sshpass -e ssh-copy-id -i /root/.ssh/id_ed25519.pub \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 root@"$OLD" 2>&1) || rc=$?
  printf '%s\n' "$out" | sed 's/^/  ssh-copy-id: /'
  echo "ssh-copy-id exit code: $rc"
  [ $rc -eq 124 ] && fail "Password login to $OLD timed out"
  [ $rc -eq 5 ] && { echo "##NEEDPASS"; exit 3; }
  [ $rc -ne 0 ] && fail "Login to $OLD failed (code $rc), see the log"
  old true || fail "Key installed, but key login doesn't work"
fi

step "3/4 Safety checks"
[ "$(old 'cat /etc/machine-id')" != "$(cat /etc/machine-id)" ] || fail "$OLD is this server. Refusing to wipe it"
[ "$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')" != "$OLD" ] || fail "$DOMAIN still points to $OLD, so it looks like the live server. Refusing to wipe it"
if old 'systemctl is-active --quiet macaw || systemctl is-active --quiet macaw-admin'; then
  fail "The SRS bot or admin bot is running on $OLD, so it looks like the live server. Refusing to wipe it"
fi
# The old server's key is authorized here (it pushed the migration). Remove that access too.
OLDPUB=$(old 'cat /root/.ssh/id_ed25519.pub 2>/dev/null' || true)

step "4/4 Wiping $OLD"
MYPUB=$(cat /root/.ssh/id_ed25519.pub)
old "set +e
  TUSER=\$(grep -Po '^User=\\K.*' /etc/systemd/system/macaw-tunnel.service 2>/dev/null)
  systemctl disable --now macaw macaw-tunnel macaw-admin 2>/dev/null
  if [ -f /opt/macaw-player/docker-compose.yml ] && docker compose version >/dev/null 2>&1; then
    (cd /opt/macaw-player && docker compose down -v --rmi local --remove-orphans)
  fi
  if command -v docker >/dev/null; then
    for v in \$(docker volume ls -q | grep '^macaw_'); do docker volume rm -f \"\$v\"; done
    docker network rm macaw_default 2>/dev/null
  fi
  rm -f /etc/systemd/system/macaw.service /etc/systemd/system/macaw-tunnel.service /etc/systemd/system/macaw-admin.service
  systemctl daemon-reload
  rm -rf /opt/macaw /opt/macaw-player /opt/macaw-admin
  rm -f /root/macaw.tgz /root/install.sh* /usr/bin/cloudflared
  rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519.pub
  id macaw >/dev/null 2>&1 && userdel macaw
  getent group macaw >/dev/null && groupdel macaw
  if [ -n \"\$TUSER\" ] && [ \"\$TUSER\" != root ] && [ \"\$TUSER\" != macaw ] && [ \"\$TUSER\" != nobody ] \\
     && [ \"\$(id -u \"\$TUSER\" 2>/dev/null || echo 0)\" -ge 100 ] && [ \"\$(id -u \"\$TUSER\")\" -lt 1000 ]; then userdel \"\$TUSER\"; fi
  echo 'Left over:'; ls -d /opt/macaw* /etc/systemd/system/macaw*.service 2>/dev/null; docker volume ls -q 2>/dev/null | grep '^macaw_'
  # Last: remove this server's key from the old one.
  if [ -f /root/.ssh/authorized_keys ]; then
    grep -vF '$MYPUB' /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp
    cat /root/.ssh/authorized_keys.tmp > /root/.ssh/authorized_keys; rm -f /root/.ssh/authorized_keys.tmp
  fi
  true" || fail "Wiping failed, see the log"
left=$(ssh -n $SSH_OPTS -o PasswordAuthentication=no root@"$OLD" true 2>/dev/null && echo yes || echo no)
echo "our key still accepted there: $left"

if [ -n "$OLDPUB" ] && [ "$OLDPUB" != "$MYPUB" ] && [ -f /root/.ssh/authorized_keys ]; then
  grep -vF "$OLDPUB" /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp || true
  cat /root/.ssh/authorized_keys.tmp > /root/.ssh/authorized_keys; rm -f /root/.ssh/authorized_keys.tmp
  echo "removed the old server's key from this server"
fi
ssh-keygen -R "$OLD" >/dev/null 2>&1 || true
echo "##RESULT wiped=$OLD"
echo "##DONE"
