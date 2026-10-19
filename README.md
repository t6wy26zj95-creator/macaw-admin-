# Macaw admin bot

Telegram bot that moves all Macaw services (SRS bot, mini app tunnel, music player, and itself) to a new VPS with one button. It does not touch Amnezia/VPN.

## Install (once, on the current server, as root)

```bash
curl -fsSL https://raw.githubusercontent.com/t6wy26zj95-creator/macaw-admin-/main/install.sh | bash
```

It asks for the admin bot token, your Telegram ID, the root password (hidden), and the DuckDNS token. These are saved to `/opt/macaw-admin/.env` (root-only) and never leave the server except when the bot copies itself to the next server.
After that the bot messages you "Admin bot is running on <IP>".

Run the same command again to update the bot. It keeps `.env`. To re-enter the secrets: `rm /opt/macaw-admin/.env` and run it again.

## Migrating

1. Buy a new VPS with Ubuntu 24.04 and the standard root password.
2. Set up AmneziaWG on it with the Amnezia app.
3. In Telegram: "Migrate to a new server", send the IP, tap "Start".

## Files

- `bot.py`: the bot (Python 3.12, aiogram 3, long polling). Only answers `OWNER_ID`.
- `migrate.sh <NEW_IP>`: 8 steps, pushes everything from this server to the new one.
- `rollback.sh <NEW_IP>`: undoes a failed switchover.
- `install.sh`: installer/updater.

Logs: `/opt/macaw-admin/logs/`, and `journalctl -u macaw-admin`.
