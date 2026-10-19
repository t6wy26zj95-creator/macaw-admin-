# Macaw admin bot

Telegram bot that moves all Macaw services (SRS bot, mini app tunnel, music player, and itself) to a new VPS with one button. It does not touch Amnezia/VPN.

## Install (once, on the current server, as root)

```bash
curl -fsSL https://raw.githubusercontent.com/t6wy26zj95-creator/macaw-admin-/main/install.sh | bash
```

It asks for the admin bot token, your Telegram ID, the root password (hidden), and the DuckDNS token. These are saved to `/opt/macaw-admin/.env` (root-only) and never leave the server except when the bot copies itself to the next server.
After that the bot messages you "Admin bot is running on <IP>".

To update later, tap "Update admin bot" in Telegram (or run the same command again). Both keep `.env`. To re-enter the secrets: `rm /opt/macaw-admin/.env` and run it again.

## Migrating

1. Tap "New server at HostVDS" (or buy a VPS with Ubuntu 24.04 and the standard root password by hand).
2. Set up AmneziaWG on it with the Amnezia app.
3. In Telegram: "Migrate to a new server", send the IP, tap "Start".

## Wiping an old server

After a migration, tap "Wipe old server" in the admin bot (now on the new server) and pick the old IP, or send any old IP.
It removes the SRS bot, tunnel, player and admin bot with their data, Docker volumes, `.env` files with tokens, install leftovers, cloudflared, and the SSH keys between the two servers. Amnezia and Docker stay.
It refuses to wipe the server it runs on, a server that DuckDNS still points to, or a server where the SRS bot or admin bot is still running.

## HostVDS

The first tap on "New server at HostVDS" runs a one-time setup: send the login from the HostVDS API page (hostvds-...), then the API password (deleted from the chat). The bot logs in to horizon.hostvds.com and finds the API address itself; sending the openrc file also works, then pick the plan (HostVDS-2 is the 3 euro one) and the IPv4+IPv6 network. The bot finds Ubuntu 24.04 itself.
New servers get the standard root password and this server's SSH key through cloud-init.
After "Wipe old server" the bot offers to delete that server at HostVDS. It refuses to delete the live server.

## Emailing a file

For people who can't use Telegram: tap "Email settings" once and set your mailbox (Gmail, Yandex or Mail.ru, with an app password) and the recipient. After that, send the bot a file, tap Send, and it is emailed from your mailbox. The settings live in `.env` and move with the bot.

## Files

- `bot.py`: the bot (Python 3.12, aiogram 3, long polling). Only answers `OWNER_ID`.
- `migrate.sh <NEW_IP>`: 8 steps, pushes everything from this server to the new one.
- `rollback.sh <NEW_IP>`: undoes a failed switchover.
- `cloud.py`: small OpenStack client for HostVDS.
- `wipe.sh <OLD_IP>`: wipes Macaw from an old server.
- `install.sh`: installer/updater.

Logs: `/opt/macaw-admin/logs/`, and `journalctl -u macaw-admin`.
