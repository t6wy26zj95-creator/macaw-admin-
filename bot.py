#!/usr/bin/env python3
"""Macaw admin bot: one-button migration of all Macaw services to a new VPS.

Runs as root from /opt/macaw-admin (systemd unit macaw-admin.service).
Talks only to OWNER_ID. No emoji anywhere in messages or buttons.
"""
import asyncio
import html
import ipaddress
import logging
import os
import re
import signal
import smtplib
import ssl
from email.message import EmailMessage
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router

from cloud import OPENRC_KEYS, Cloud, CloudError, cloud_config, find_auth_url, looks_like_example, parse_openrc
from monitor import DEFAULT_REFERENCE, Prober, Round, Tracker, country_name
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

BASE = Path("/opt/macaw-admin")
ENV_FILE = BASE / ".env"
LOG_DIR = BASE / "logs"
LOCK = "/run/macaw-migrate.lock"
HANDOFF_FAILED = BASE / "handoff-failed"
OLD_SERVERS = BASE / "old-servers"  # written on the new server by migrate.sh
SCRIPT_TIMEOUT = 30 * 60
LOCKED_RC = 75  # flock exit code when another run holds the lock

log = logging.getLogger("macaw-admin")


def load_env() -> dict[str, str]:
    """Read .env exactly the way the shell scripts do (bash `source`), so quoting matches."""
    out = subprocess.run(
        ["bash", "-c", 'set -a; source "$1"; env -0', "_", str(ENV_FILE)],
        capture_output=True, check=True, env={"PATH": "/usr/bin:/bin"},
    ).stdout
    env = {}
    for item in out.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode()] = v.decode()
    return env


CFG = load_env()
BOT_TOKEN = CFG["BOT_TOKEN"]
OWNER_ID = int(CFG["OWNER_ID"])
DOMAIN = CFG.get("DUCKDNS_DOMAIN", "macawp3") + ".duckdns.org"


def save_password(password: str) -> None:
    set_env("ROOT_PASSWORD", password)


def set_env(key: str, value: str) -> None:
    """Set KEY in .env (single-quoted so any characters survive `source`)."""
    quoted = "'" + value.replace("'", "'\\''") + "'"
    lines = ENV_FILE.read_text().splitlines()
    newline = f"{key}={quoted}"
    CFG[key] = value
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = newline
            break
    else:
        lines.append(newline)
    tmp = ENV_FILE.with_name(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_FILE)


# ---------- keyboards ----------

def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


MENU = kb([("New server at HostVDS", "newsrv")], [("Migrate to a new server", "migrate")],
          [("Wipe old server", "wipe")], [("Block check", "blockcheck")], [("Email settings", "email")],
          [("Update SRS bot", "srsupdate")], [("Update admin bot", "update")])
CANCEL = kb([("Cancel", "cancel")])
CONFIRM = kb([("Start", "start"), ("Cancel", "cancel")])


def failure_kb() -> InlineKeyboardMarkup:
    row = [("Retry", "retry")]
    if S.switched:
        row.append(("Roll back", "rollback"))
    return kb(row)


# ---------- state ----------

@dataclass
class State:
    # idle | ask_ip | confirm | running | ask_pass | failed | rolling_back | handoff
    # wipe_ask_ip | wipe_confirm | wiping | wipe_ask_pass | wipe_failed
    # email_ask_user | email_ask_pass | email_ask_to
    # cloud_ask_openrc | cloud_ask_pass | cloud_pick_flavor | cloud_pick_net | cloud_pick_image
    # newsrv_confirm | creating | srvdel_confirm
    mode: str = "idle"
    ip: str = ""
    password: str | None = None   # override sent via NEEDPASS
    password_saved: bool = False
    switched: bool = False        # some attempt reached step 6: services here are stopped
    wipe_ip: str = ""
    wipe_password: str | None = None
    email_user: str = ""
    email_password: str = ""
    cloud_tmp: dict = field(default_factory=dict)    # HostVDS settings being set up
    cloud_lists: dict = field(default_factory=dict)  # flavors / networks / images to pick from
    del_ip: str = ""
    pending_file: tuple[str, str] | None = None  # (file_id, file_name) waiting for Send
    task: asyncio.Task | None = None


S = State()
CURRENT_IP = ""
BUSY = {"running", "rolling_back", "handoff", "wiping", "creating"}


# ---------- progress message ----------

class Progress:
    """One Telegram message that is edited as ##STEP lines arrive."""

    def __init__(self, bot: Bot, title: str):
        self.bot, self.title = bot, title
        self.steps: list[list[str]] = []  # [status, text]
        self.msg_id: int | None = None

    def render(self) -> str:
        lines = [self.title, ""]
        lines += [f"[{st}] {html.escape(text)}" for st, text in self.steps]
        if not self.steps:
            lines.append("Starting...")
        return "\n".join(lines)

    async def show(self) -> None:
        try:
            if self.msg_id is None:
                self.msg_id = (await self.bot.send_message(OWNER_ID, self.render())).message_id
            else:
                await self.bot.edit_message_text(self.render(), chat_id=OWNER_ID, message_id=self.msg_id)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("progress update failed: %s", e)
        except TelegramAPIError as e:
            log.warning("progress update failed: %s", e)

    async def step(self, text: str) -> None:
        self.finish_running("done")
        self.steps.append(["running", text])
        await self.show()

    def finish_running(self, status: str) -> None:
        for s in self.steps:
            if s[0] == "running":
                s[0] = status


# ---------- running the scripts ----------

@dataclass
class RunResult:
    log: Path
    rc: int | None = None
    last_step: int = 0
    needpass: bool = False
    fail: str = ""
    result: dict = field(default_factory=dict)
    done: bool = False
    timed_out: bool = False


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


async def run_script(name: str, args: list[str], progress: Progress,
                     extra_env: dict | None = None, on_step=None) -> RunResult:
    LOG_DIR.mkdir(exist_ok=True)
    res = RunResult(log=LOG_DIR / f"{name}-{datetime.now():%Y%m%d-%H%M}.log")
    env = dict(os.environ)
    env.pop("ROOT_PASSWORD_OVERRIDE", None)
    env.update(PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               LC_ALL="C.UTF-8", DEBIAN_FRONTEND="noninteractive", HOME="/root")
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        "flock", "-n", "-E", str(LOCKED_RC), LOCK, str(BASE / f"{name}.sh"), *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True, limit=1 << 20,
    )

    async def pump():
        with res.log.open("a", encoding="utf-8") as f:
            f.write(f"=== {datetime.now():%F %T} {name}.sh {' '.join(args)}\n")
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:  # absurdly long line
                    f.write("[line too long, skipped]\n")
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                f.write(line + "\n")
                f.flush()
                if line.startswith("##STEP "):
                    text = line[7:].strip()
                    m = re.match(r"(\d+)/", text)
                    res.last_step = int(m.group(1)) if m else res.last_step
                    if on_step:
                        await on_step(res.last_step)
                    await progress.step(text)
                elif line.startswith("##NEEDPASS"):
                    res.needpass = True
                elif line.startswith("##FAIL"):
                    res.fail = line[6:].strip()
                elif line.startswith("##RESULT"):
                    res.result = dict(kv.split("=", 1) for kv in line[8:].split() if "=" in kv)
                elif line.startswith("##DONE"):
                    res.done = True
            res.rc = await proc.wait()
            f.write(f"=== exit code {res.rc}\n")

    try:
        await asyncio.wait_for(pump(), SCRIPT_TIMEOUT)
    except TimeoutError:
        res.timed_out = True
        await kill_group(proc)
        with res.log.open("a", encoding="utf-8") as f:
            f.write(f"=== killed after {SCRIPT_TIMEOUT // 60} minutes\n")
    return res


async def send_failure(bot: Bot, res: RunResult, what: str, total: int, markup=None) -> None:
    where = f" at step {res.last_step}/{total}" if res.last_step else ""
    if res.timed_out:
        text = f"{what} timed out after {SCRIPT_TIMEOUT // 60} minutes{where}."
    elif res.fail:
        text = f"{what} failed{where}: {html.escape(res.fail)}"
    else:
        text = f"{what} stopped unexpectedly{where} (exit code {res.rc})."
    if S.switched and markup is None:
        text += ("\n\nServices on this server are stopped. Retry to continue the migration, "
                 "or Roll back to bring them back here.")
    text += "\n\nFull log attached."
    if len(text) > 1000:
        text = text[:990] + "..."
    markup = markup or failure_kb()
    try:
        await bot.send_document(OWNER_ID, FSInputFile(res.log), caption=text, reply_markup=markup)
    except TelegramAPIError as e:
        log.warning("sending log failed: %s", e)
        await bot.send_message(OWNER_ID, text + f"\nLog: {res.log}", reply_markup=markup)


# ---------- migration ----------

async def migration(bot: Bot) -> None:
    ip = S.ip
    S.mode = "running"
    progress = Progress(bot, f"Migration to <code>{ip}</code>")
    await progress.show()

    async def on_step(num: int):
        if num >= 6:
            S.switched = True
        # Step 3 means step 2 (login) worked: the new password is right, keep it.
        # Saved before step 5 so the copy of .env on the new server has it too.
        if num >= 3 and S.password and not S.password_saved:
            save_password(S.password)
            S.password_saved = True

    extra = {"MIGRATE_FROM": CURRENT_IP}
    if S.password:
        extra["ROOT_PASSWORD_OVERRIDE"] = S.password
    res = await run_script("migrate", [ip], progress, extra, on_step)

    if res.rc == LOCKED_RC and not res.last_step:
        S.mode = "failed" if S.switched else "idle"
        await bot.send_message(OWNER_ID, "Another migration or rollback is already running on this server.",
                               reply_markup=failure_kb() if S.switched else MENU)
        return

    if res.needpass and not res.done:
        progress.finish_running("failed")
        await progress.show()
        S.mode = "ask_pass"
        text = (f"The new server <code>{ip}</code> rejected the root password. Log attached.\n\n"
                "Send the password. I will delete your message right away.")
        try:
            await bot.send_document(OWNER_ID, FSInputFile(res.log), caption=text, reply_markup=CANCEL)
        except TelegramAPIError:
            await bot.send_message(OWNER_ID, text, reply_markup=CANCEL)
        return

    if not res.done:
        progress.finish_running("failed")
        await progress.show()
        S.mode = "failed"
        await send_failure(bot, res, "Migration", 8)
        return

    progress.finish_running("done")
    await progress.show()
    r = res.result
    tun = r.get("tunnel", "?")
    tun_text = f"https://{html.escape(tun)}" if tun and tun != "?" else "address not reported yet"
    await bot.send_message(OWNER_ID, "\n".join([
        f"Migration to <code>{ip}</code> finished.",
        "",
        "SRS bot: OK",
        f"Tunnel: OK ({tun_text})",
        f"Player: OK (HTTP {html.escape(r.get('player', '?'))})",
        f"DuckDNS: OK ({DOMAIN} -> {ip})",
        "",
        f"The old server <code>{CURRENT_IP}</code> still holds the data with tokens. Once everything "
        "works, tap \"Wipe old server\" in the admin bot on the new server.",
        "",
        f"Handing the admin bot over to <code>{ip}</code>. The next message will come from there.",
    ]), disable_web_page_preview=True)
    await handoff(bot, ip)


async def handoff(bot: Bot, ip: str) -> None:
    """Stop this bot before starting the new one (two pollers on one token = 409 Conflict).
    Runs in its own transient unit so stopping macaw-admin doesn't kill it.
    If the new admin bot can't be started, this one comes back and says so."""
    S.mode = "handoff"
    script = f"""
systemctl disable macaw macaw-tunnel
(cd /opt/macaw-player && docker compose down)
systemctl disable --now macaw-admin
for i in 1 2 3 4 5 6 7 8 9 10; do
  ssh -n -o BatchMode=yes -o ConnectTimeout=30 root@{ip} systemctl start macaw-admin && exit 0
  sleep 15
done
echo {ip} > {HANDOFF_FAILED}
systemctl enable --now macaw-admin
"""
    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--quiet", "--collect", "--on-active=5",
        f"--unit=macaw-handoff-{int(time.time())}", "/bin/sh", "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        S.mode = "idle"
        await bot.send_message(
            OWNER_ID,
            "Could not start the handoff: " + html.escape(out.decode(errors="replace").strip()[:500])
            + f"\n\nServices already run on <code>{ip}</code>. The admin bot stays here for now.",
            reply_markup=MENU)


# ---------- rollback ----------

async def rollback(bot: Bot) -> None:
    ip = S.ip
    S.mode = "rolling_back"
    progress = Progress(bot, f"Rolling back from <code>{ip}</code>")
    await progress.show()
    res = await run_script("rollback", [ip], progress)

    if res.rc == LOCKED_RC and not res.last_step:
        S.mode = "failed"
        await bot.send_message(OWNER_ID, "Another migration or rollback is already running on this server.",
                               reply_markup=failure_kb())
        return

    r = res.result
    warn = ""
    if r.get("newstopped") != "yes":
        warn = (f"\n\nWarning: could not reach <code>{ip}</code> to stop the services there. "
                "If they are still running there, both SRS bots fight over the same token.")
    if not res.done:
        progress.finish_running("failed")
        await progress.show()
        S.mode = "failed"
        await send_failure(bot, res, "Rollback", 3)
        if warn:
            await bot.send_message(OWNER_ID, warn.strip())
        return

    progress.finish_running("done")
    await progress.show()
    S.switched = False
    S.mode = "idle"
    await bot.send_message(OWNER_ID, "\n".join([
        f"Rollback finished. Everything runs on <code>{CURRENT_IP}</code> again.",
        "",
        "SRS bot and tunnel: OK",
        f"Player: OK ({html.escape(r.get('player', '?'))} containers)",
        f"DuckDNS: OK ({DOMAIN} -> {CURRENT_IP})",
    ]) + warn, reply_markup=MENU)


# ---------- wipe old server ----------

def known_old_servers() -> list[str]:
    try:
        ips = OLD_SERVERS.read_text().split()
    except FileNotFoundError:
        return []
    out = []
    for ip in ips:
        if ip not in out and ip != CURRENT_IP:
            out.append(ip)
    return out


def forget_old_server(ip: str) -> None:
    rest = [x for x in known_old_servers() if x != ip]
    OLD_SERVERS.write_text("".join(x + "\n" for x in rest))


WIPE_RETRY = kb([("Retry", "wipe_retry"), ("Cancel", "cancel")])


async def wipe(bot: Bot) -> None:
    ip = S.wipe_ip
    S.mode = "wiping"
    progress = Progress(bot, f"Wiping old server <code>{ip}</code>")
    await progress.show()
    extra = {"ROOT_PASSWORD_OVERRIDE": S.wipe_password} if S.wipe_password else None
    res = await run_script("wipe", [ip], progress, extra)

    if res.rc == LOCKED_RC and not res.last_step:
        S.mode = "idle"
        await bot.send_message(OWNER_ID, "A migration or rollback is running on this server. Try later.",
                               reply_markup=MENU)
        return
    if res.needpass and not res.done:
        progress.finish_running("failed")
        await progress.show()
        S.mode = "wipe_ask_pass"
        text = (f"The old server <code>{ip}</code> rejected the root password. Log attached.\n\n"
                "Send its password. I will delete your message right away.")
        try:
            await bot.send_document(OWNER_ID, FSInputFile(res.log), caption=text, reply_markup=CANCEL)
        except TelegramAPIError:
            await bot.send_message(OWNER_ID, text, reply_markup=CANCEL)
        return
    if not res.done:
        progress.finish_running("failed")
        await progress.show()
        S.mode = "wipe_failed"
        await send_failure(bot, res, "Wiping", 4, WIPE_RETRY)
        return

    progress.finish_running("done")
    await progress.show()
    forget_old_server(ip)
    S.mode, S.wipe_password = "idle", None
    await bot.send_message(OWNER_ID, "\n".join([
        f"Old server <code>{ip}</code> wiped.",
        "",
        "Removed: SRS bot, tunnel, player and admin bot with their services, data, Docker volumes, "
        ".env files with tokens, install leftovers, cloudflared and the SSH keys between the two servers.",
        "Amnezia and Docker were not touched.",
    ]), reply_markup=kb([(f"Delete {ip} at HostVDS", f"srvdel:{ip}")], [("Menu", "menu")])
        if cloud_creds_ready() else MENU)


# ---------- HostVDS servers ----------

CLOUD_SETTINGS = ("HOSTVDS_FLAVOR", "HOSTVDS_NETWORK", "HOSTVDS_IMAGE")


def cloud_creds_ready() -> bool:
    return all(CFG.get(k) for k in ("OS_AUTH_URL", "OS_USERNAME", "OS_PASSWORD"))


def cloud_ready() -> bool:
    return cloud_creds_ready() and all(CFG.get(k) for k in CLOUD_SETTINGS)


def flavor_label(f: dict) -> str:
    return f"{f.get('name')}: {f.get('vcpus')} CPU, {f.get('ram', 0) / 1024:g} GB RAM, {f.get('disk')} GB"


async def ensure_ssh_key() -> str:
    key = Path("/root/.ssh/id_ed25519")
    if not key.exists():
        key.parent.mkdir(mode=0o700, exist_ok=True)
        proc = await asyncio.create_subprocess_exec("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q")
        await proc.wait()
    return key.with_suffix(".pub").read_text().strip()


async def wait_port_22(ip: str, timeout: int = 600) -> bool:
    for _ in range(timeout // 10):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(ip, 22), 10)
            w.close()
            return True
        except Exception:
            await asyncio.sleep(10)
    return False


async def create_server(bot: Bot) -> None:
    S.mode = "creating"
    progress = Progress(bot, "New server at HostVDS")
    await progress.show()
    c = Cloud(CFG)
    ip = None
    try:
        await progress.step("1/3 Ordering the server")
        flavors = {f["id"]: f for f in await c.flavors()}
        flavor = flavors.get(CFG["HOSTVDS_FLAVOR"])
        if not flavor:
            raise CloudError("The saved plan no longer exists. Change the server settings.")
        pubkey = await ensure_ssh_key()
        name = f"macaw-{datetime.now():%Y%m%d-%H%M}"
        # Some images can't run on some plans ("No valid host was found"): then try the next
        # Ubuntu 24.04 image (plain ones first) and remember the one that worked.
        images = Cloud.ubuntu_2404_candidates(await c.images())
        saved = [i for i in images if i["id"] == CFG["HOSTVDS_IMAGE"]]
        if not images:
            images = [{"id": CFG["HOSTVDS_IMAGE"], "name": CFG.get("HOSTVDS_IMAGE_LABEL", "")}]
        elif saved and not Cloud.is_prebuilt(saved[0]):
            images = saved + [i for i in images if i is not saved[0]]
        srv = None
        for n, image in enumerate(images):
            if n:
                progress.finish_running("failed")
                await progress.step(f"1/3 That system did not start on this plan, trying {image.get('name', image['id'])}")
            sid = await c.create_server(name, flavor, image["id"], CFG["HOSTVDS_NETWORK"],
                                        cloud_config(CFG["ROOT_PASSWORD"], pubkey))
            await progress.step("2/3 Waiting for it to start")
            try:
                srv = await c.wait_active(sid)
                break
            except CloudError as e:
                if "no valid host" not in str(e).lower():
                    raise
                log.warning("image %s failed: %s", image.get("name"), e)
                try:  # a server in ERROR state is useless: don't leave it in the account
                    await c.delete_server(sid)
                except Exception:
                    log.exception("could not delete failed server")
                if n == len(images) - 1:
                    raise
        if image["id"] != CFG["HOSTVDS_IMAGE"]:
            set_env("HOSTVDS_IMAGE", image["id"])
            set_env("HOSTVDS_IMAGE_LABEL", image.get("name", image["id"]))
        # The project's default firewall lets nothing in: allow SSH and the VPN.
        await c.open_ports(sid)
        ip = Cloud.ipv4(srv)
        await progress.step(f"3/3 Waiting for SSH on {ip}")
        if not await wait_port_22(ip):
            raise CloudError(f"Server {ip} started, but SSH did not come up in 10 minutes")
    except Exception as e:
        log.exception("create server failed")
        progress.finish_running("failed")
        await progress.show()
        S.mode = "idle"
        markup = kb([(f"Delete {ip} at HostVDS", f"srvdel:{ip}")], [("Menu", "menu")]) if ip else MENU
        await bot.send_message(OWNER_ID, f"Creating the server failed: {html.escape(str(e))[:700]}",
                               reply_markup=markup)
        return
    progress.finish_running("done")
    await progress.show()
    S.mode = "idle"
    await bot.send_message(
        OWNER_ID,
        f"New server <code>{ip}</code> is ready ({html.escape(flavor.get('name', ''))}, {html.escape(name)}). "
        "The root password is your standard one.\n\n"
        "Next: set up the VPN on it with the Amnezia app, then tap the button below.",
        reply_markup=kb([(f"Migrate to {ip}", f"migrate_to:{ip}")], [("Menu", "menu")]))


async def accept_cloud_settings(message: Message, values: dict, text: str = "") -> None:
    if not values.get("OS_USERNAME"):
        if looks_like_example(text):
            hint = ("That's the example commands from the bottom of the HostVDS API page. "
                    "They don't contain your login.\n\n")
        else:
            hint = "I couldn't find the login in that.\n\n"
        await message.answer(
            hint + "On the HostVDS API page, copy the text in the Login box (just above Password). "
            "It starts with <code>hostvds-</code>. Send just that here. You don't need the openrc.sh file.",
            reply_markup=CANCEL)
        return
    S.cloud_tmp, S.mode = values, "cloud_ask_pass"
    await message.answer(f"Got it, login <code>{html.escape(values['OS_USERNAME'])}</code>.\n\n"
                         "Now send the API password: on the same HostVDS API page, tap Show next to the "
                         "password and copy it. I will delete your message right away.", reply_markup=CANCEL)


async def show_cloud_setup_start(message: Message) -> None:
    S.mode, S.cloud_tmp, S.cloud_lists = "cloud_ask_openrc", {}, {}
    await message.answer(
        "HostVDS setup (one time).\n\n"
        "1. Open the HostVDS API page.\n"
        "2. Copy the text in the Login box (just above Password). It starts with <code>hostvds-</code>. "
        "Send just that here, not the OpenStack example commands below it.\n"
        "3. Then I will ask for the password from the same page.\n\n"
        "You don't need the openrc.sh file.",
        reply_markup=CANCEL)


def start_task(coro) -> None:
    async def guarded():
        try:
            await coro
        except Exception as e:  # never leave the owner without an answer
            log.exception("task crashed")
            S.mode = "failed" if S.switched else "idle"
            try:
                await BOT.send_message(OWNER_ID, f"Internal error: {html.escape(repr(e))[:500]}",
                                       reply_markup=failure_kb() if S.switched else MENU)
            except TelegramAPIError:
                pass
    S.task = asyncio.create_task(guarded())


# ---------- emailing files ----------
# Telegram is blocked for some people, so files (VPN configs) can be forwarded by email.
# Sender, app password and recipient are set in the bot (Email settings) and kept in .env.

SMTP_HOSTS = {
    "gmail.com": "smtp.gmail.com", "googlemail.com": "smtp.gmail.com",
    "yandex.ru": "smtp.yandex.ru", "ya.ru": "smtp.yandex.ru", "yandex.com": "smtp.yandex.ru",
    "mail.ru": "smtp.mail.ru", "inbox.ru": "smtp.mail.ru", "list.ru": "smtp.mail.ru", "bk.ru": "smtp.mail.ru",
}
APP_PASSWORD_HELP = {
    "smtp.gmail.com": "a Google app password: turn on 2-Step Verification, then create one at "
                      "myaccount.google.com/apppasswords (16 letters).",
    "smtp.yandex.ru": "a Yandex app password for Mail: id.yandex.ru, Security, App passwords.",
    "smtp.mail.ru": "a Mail.ru app password: account settings, Security, Passwords for external apps.",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def smtp_send(user: str, password: str, to: str | None = None,
              filename: str | None = None, data: bytes | None = None) -> None:
    """Log in over SSL; send the file if one is given (otherwise only check the login)."""
    host = SMTP_HOSTS[user.rsplit("@", 1)[1].lower()]
    with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, password)
        if data is None:
            return
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, to, f"Config: {filename}"
        msg.set_content(f"File attached: {filename}\n")
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
        smtp.send_message(msg)


def email_ready() -> bool:
    return all(CFG.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO"))


# ---------- block monitor ----------
# Test points inside the country (check-host.net) try to reach this server every few minutes.
# Settings in .env (all optional): MONITOR_ENABLED (1/0), MONITOR_COUNTRY (ru),
# MONITOR_PORTS (22; add the XRay port, e.g. "22 443"), MONITOR_INTERVAL (minutes, 10),
# MONITOR_REFERENCE (a site that works in that country, ya.ru:443 for Russia),
# GLOBALPING_TOKEN (free token from dash.globalping.io for a higher hourly limit; not needed).

def mon_country() -> str:
    return (CFG.get("MONITOR_COUNTRY") or "ru").strip().lower()


def mon_ports() -> list[int]:
    ports = [int(p) for p in re.findall(r"\d+", CFG.get("MONITOR_PORTS") or "22") if 0 < int(p) < 65536]
    return ports or [22]


def mon_interval() -> int:
    try:
        return max(5, int(CFG.get("MONITOR_INTERVAL") or 10))
    except ValueError:
        return 10


def mon_reference() -> str:
    return CFG.get("MONITOR_REFERENCE") or DEFAULT_REFERENCE.get(mon_country(), "www.google.com:443")


def mon_enabled() -> bool:
    return CFG.get("MONITOR_ENABLED", "1") != "0"


CHECKER = Prober(mon_country(), CFG.get("GLOBALPING_TOKEN", ""))
TRACKER = Tracker(BASE / "monitor-state.json")


async def run_block_check() -> Round:
    # A round that finished in the last 2 minutes is reused, so a tap right after the timed
    # check (or the other way round) doesn't send check-host.net a second batch of requests.
    return await CHECKER.run(CURRENT_IP, mon_ports(), mon_reference(), max_age=120)


def places(nodes, ok: bool) -> str:
    return ", ".join(html.escape(n.place) for n in nodes if bool(n.target_ok) == ok) or "none"


def round_details(r: Round) -> str:
    if r.error:
        return f"Could not check: {html.escape(r.error[:300])}"
    local, abroad = r.local(), r.abroad()
    if not local and not abroad:
        return "No test point gave a usable answer."
    lines = [f"{country_name(mon_country())}: reach it: {places(local, True)}; can't: {places(local, False)}",
             f"Abroad: reach it: {places(abroad, True)}; can't: {places(abroad, False)}"]
    broken = [n for n in r.nodes if not n.usable]
    if broken:
        lines.append("Ignored (test point not working): " + ", ".join(html.escape(n.place) for n in broken))
    if r.source:
        lines.append(f"Test points from {r.source}.")
    return "\n".join(lines)


def duration(sec: float) -> str:
    m = int(sec // 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} days"


VERDICT_TEXT = {
    "ok": "reachable from {c}",
    "partial": "partly blocked in {c}: some test points there can't reach it",
    "blocked": "blocked in {c}: no test point there can reach it, while it works from abroad",
    "down": "unreachable from everywhere, abroad included. That looks like a server or provider "
            "problem, not a block",
    "unknown": "not checked right now",
}


def alert_text(old: str, new: str, held: float, r: Round) -> str:
    c, ip = country_name(mon_country()), f"<code>{CURRENT_IP}</code>"
    if new == "unknown":
        return (f"Block monitor: I haven't been able to check {ip} for a while "
                f"(check-host.net or its test points aren't answering).\n\n{round_details(r)}")
    if new == "ok":
        head = f"Server {ip} is reachable from {c} again"
        if old in ("blocked", "partial", "down") and held:
            head += f" (it was {old} for {duration(held)})"
        return head + f".\n\n{round_details(r)}"
    text = f"Server {ip} is {VERDICT_TEXT[new].format(c=c)}.\n\n{round_details(r)}"
    if new in ("blocked", "partial"):
        text += ("\n\nThis shows the IP can't be reached from inside the country. It can't show who "
                 "blocked it. Moving to a new server gives you a new IP.")
    return text


BLOCK_ALERT_KB = kb([("Check again", "blockcheck")], [("New server at HostVDS", "newsrv")], [("Menu", "menu")])


async def monitor_loop(bot: Bot) -> None:
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            # Skip while services are moving or this server isn't the live one any more.
            if mon_enabled() and CURRENT_IP not in ("", "unknown") and S.mode not in BUSY and not S.switched:
                r = await run_block_check()
                log.info("block check %s: %s %s", CURRENT_IP, r.verdict, r.error)
                change = TRACKER.feed(CURRENT_IP, r.verdict)
                if change:
                    old, new, held = change
                    markup = BLOCK_ALERT_KB if new in ("blocked", "partial") else None
                    await bot.send_message(OWNER_ID, alert_text(old, new, held, r), reply_markup=markup)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("block monitor round failed")
        # Back off while checks fail (usually check-host.net's rate limit): 10, 20, 30, 30... min.
        await asyncio.sleep(mon_interval() * 60 * min(TRACKER.unknown_streak + 1, 3))


# ---------- handlers ----------

router = Router()
router.message.filter(F.from_user.id == OWNER_ID, F.chat.type == "private")
router.callback_query.filter(F.from_user.id == OWNER_ID)


async def drop_buttons(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


@router.message(Command("delete"))
async def cmd_delete(message: Message):
    """/delete <ip>: delete a HostVDS server (asks first; never the live one)."""
    parts = (message.text or "").split()
    if len(parts) != 2 or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", parts[1]) or not cloud_creds_ready():
        await message.answer("Send it like this: <code>/delete 1.2.3.4</code>", reply_markup=MENU)
        return
    if S.mode in BUSY:
        return
    S.del_ip, S.mode = parts[1], "srvdel_confirm"
    await message.answer(f"Delete server <code>{S.del_ip}</code> at HostVDS? It is destroyed for good "
                         "and billing for it stops.",
                         reply_markup=kb([("Delete", "srvdel_go"), ("Cancel", "cancel")]))


@router.message(Command("openports"))
async def cmd_openports(message: Message):
    """Servers created before the firewall fix: let SSH and the VPN in."""
    if not cloud_creds_ready():
        await message.answer("HostVDS isn't set up yet.", reply_markup=MENU)
        return
    try:
        c = Cloud(CFG)
        servers = (await c.call("GET", "compute", "/servers/detail")).get("servers", [])
        done = []
        for srv in servers:
            if srv.get("name", "").startswith("macaw-"):
                await c.open_ports(srv["id"])
                done.append(Cloud.ipv4(srv) or srv["name"])
    except Exception as e:
        log.exception("openports failed")
        await message.answer(f"Could not open the ports: {html.escape(str(e))[:500]}", reply_markup=MENU)
        return
    if not done:
        await message.answer("No servers made by this bot in your HostVDS account.", reply_markup=MENU)
        return
    await message.answer("Ports opened on " + ", ".join(f"<code>{html.escape(x)}</code>" for x in done)
                         + ". Try connecting again in a minute.", reply_markup=MENU)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if S.mode in BUSY:
        await message.answer("A migration or rollback is running. Progress is in the message above.")
        return
    if S.switched:
        S.mode = "failed"
        await message.answer(f"The last migration to <code>{S.ip}</code> stopped after the switchover. "
                             "Services on this server are stopped.", reply_markup=failure_kb())
        return
    S.mode = "idle"
    await message.answer(f"Macaw admin bot. Current server: <code>{CURRENT_IP}</code>.", reply_markup=MENU)


@router.callback_query(F.data == "migrate")
async def cb_migrate(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY:
        await cb.message.answer("A migration or rollback is already running.")
        return
    if S.switched:
        await cb.message.answer("The last migration stopped after the switchover. Retry it or roll it back first.",
                                reply_markup=failure_kb())
        return
    S.mode = "ask_ip"
    S.password, S.password_saved = None, False
    await cb.message.answer("Send the new server's IP.", reply_markup=CANCEL)


@router.callback_query(F.data == "wipe")
async def cb_wipe(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY:
        await cb.message.answer("A migration, rollback or wipe is already running.")
        return
    if S.switched:
        await cb.message.answer("The last migration stopped after the switchover. Retry it or roll it back first.",
                                reply_markup=failure_kb())
        return
    S.mode, S.wipe_password = "wipe_ask_ip", None
    known = known_old_servers()
    rows = [[(f"Wipe {ip}", f"wipeip:{ip}")] for ip in known] + [[("Cancel", "cancel")]]
    text = "Send the old server's IP."
    if known:
        text = "Which old server should I wipe? Tap it, or send another IP."
    await cb.message.answer(text, reply_markup=kb(*rows))


async def ask_wipe_confirm(message: Message, ip: str) -> None:
    S.wipe_ip, S.mode = ip, "wipe_confirm"
    await message.answer(
        f"Wipe <code>{ip}</code>?\n\n"
        "This deletes the SRS bot, tunnel, player and admin bot there, with all their data, Docker volumes "
        "and tokens. Amnezia stays. It can't be undone.\n\n"
        "I will refuse if that server is still the live one.",
        reply_markup=kb([("Wipe", "wipe_go"), ("Cancel", "cancel")]))


@router.callback_query(F.data.startswith("wipeip:"))
async def cb_wipe_ip(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "wipe_ask_ip":
        return
    await ask_wipe_confirm(cb.message, cb.data.split(":", 1)[1])


@router.callback_query(F.data == "wipe_go")
async def cb_wipe_go(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "wipe_confirm":
        return
    start_task(wipe(cb.bot))


@router.callback_query(F.data == "wipe_retry")
async def cb_wipe_retry(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "wipe_failed":
        return
    start_task(wipe(cb.bot))


@router.callback_query(F.data == "email")
async def cb_email(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY:
        await cb.message.answer("Not now: a migration, rollback or wipe is running.")
        return
    S.mode = "email_ask_user"
    text = ""
    if email_ready():
        text = (f"Now: files go from {html.escape(CFG['SMTP_USER'])} to {html.escape(CFG['MAIL_TO'])}.\n"
                "To change it, go through the steps again.\n\n")
    await cb.message.answer(text + "Send the address to send from (Gmail, Yandex or Mail.ru).", reply_markup=CANCEL)


@router.message(F.document)
async def on_document(message: Message):
    if S.mode == "cloud_ask_openrc":
        try:
            buf = await message.bot.download(message.document)
            values = parse_openrc(buf.read().decode("utf-8", "replace"))
        except Exception as e:
            await message.answer(f"Could not read that file: {html.escape(str(e))[:200]}", reply_markup=CANCEL)
            return
        await accept_cloud_settings(message, values)
        return
    if S.mode in ("ask_pass", "wipe_ask_pass", "email_ask_pass", "cloud_ask_pass"):
        await message.answer("I'm waiting for a password, not a file. Tap Cancel first.", reply_markup=CANCEL)
        return
    if not email_ready():
        await message.answer("Email isn't set up yet. Tap Email settings, then send the file again.",
                             reply_markup=MENU)
        return
    doc = message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.answer("That file is over 20 MB, too big for a Telegram bot to download.")
        return
    S.pending_file = (doc.file_id, doc.file_name or "config.conf")
    await message.answer(f"Email <code>{html.escape(S.pending_file[1])}</code> to "
                         f"{html.escape(CFG['MAIL_TO'])}?",
                         reply_markup=kb([("Send", "email_send"), ("Cancel", "email_drop")]))


@router.callback_query(F.data == "email_drop")
async def cb_email_drop(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    S.pending_file = None
    await cb.message.answer("Not sent.")


@router.callback_query(F.data == "email_send")
async def cb_email_send(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if not S.pending_file or not email_ready():
        return
    file_id, name = S.pending_file
    S.pending_file = None
    try:
        buf = await cb.bot.download(file_id)
        await asyncio.to_thread(smtp_send, CFG["SMTP_USER"], CFG["SMTP_PASSWORD"], CFG["MAIL_TO"],
                                name, buf.read())
    except Exception as e:
        log.warning("email failed: %r", e)
        await cb.message.answer(f"Could not send <code>{html.escape(name)}</code>: {html.escape(str(e))[:300]}")
        return
    await cb.message.answer(f"Sent <code>{html.escape(name)}</code> to {html.escape(CFG['MAIL_TO'])}.")


# raw.githubusercontent.com caches "main" for up to 5 minutes, which once paired a stale installer
# with fresh bot code. So the installer is fetched from the latest commit, like the files it installs.
INSTALL_URL = ("https://raw.githubusercontent.com/t6wy26zj95-creator/macaw-admin-/"
               "$(sha=$(curl -fsSL -H 'Accept: application/vnd.github.sha' "
               "https://api.github.com/repos/t6wy26zj95-creator/macaw-admin-/commits/main 2>/dev/null); "
               "case $sha in *[!0-9a-f]*|'') echo main;; *) echo $sha;; esac)/install.sh")


@router.callback_query(F.data == "update")
async def cb_update(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY or S.switched:
        await cb.message.answer("Not now: a migration, rollback or wipe is running or unfinished.")
        return
    # The installer restarts macaw-admin, so it runs in its own unit.
    LOG_DIR.mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--quiet", "--collect", f"--unit=macaw-update-{int(time.time())}",
        "--setenv=HOME=/root", "/bin/bash", "-c",
        f"curl -fsSL {INSTALL_URL} | bash > {LOG_DIR}/update.log 2>&1 || "
        f"{{ set -a; . {ENV_FILE}; set +a; "
        f"curl -s https://api.telegram.org/bot$BOT_TOKEN/sendMessage -d chat_id={OWNER_ID} "
        f"--data-urlencode 'text=Update failed, see {LOG_DIR}/update.log' >/dev/null; }}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        await cb.message.answer("Could not start the update: " + html.escape(out.decode(errors="replace")[:500]))
        return
    await cb.message.answer("Updating from GitHub. The bot restarts and says hello when it's done.")


# ---------- SRS bot update ----------
# Runs the SRS bot's own updater (/opt/macaw/app/deploy/update.sh) and reports its output.
# It runs in its own unit, so it survives an admin bot restart; the exit code goes to a
# file, and a result the bot didn't get to report is sent on the next start.

SRS_UPDATE_SCRIPT = "/opt/macaw/app/deploy/update.sh"
SRS_UPDATE_UNIT = "macaw-srs-update"
SRS_UPDATE_LOG = LOG_DIR / "srs-update.log"
SRS_UPDATE_RC = LOG_DIR / "srs-update.rc"           # exit code, written when the script ends
SRS_UPDATE_PENDING = LOG_DIR / "srs-update.pending"  # exists until the result is reported
SRS_UPDATE_TIMEOUT = 30 * 60


async def srs_update_active() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "is-active", "--quiet", SRS_UPDATE_UNIT,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await proc.wait() == 0


async def report_srs_update(bot: Bot) -> None:
    """Send the update result (tail in the message, full log attached) once."""
    if not SRS_UPDATE_PENDING.exists():
        return
    SRS_UPDATE_PENDING.unlink(missing_ok=True)
    try:
        rc = SRS_UPDATE_RC.read_text().strip()
    except FileNotFoundError:
        rc = ""
    try:
        lines = SRS_UPDATE_LOG.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    if rc == "0":
        head = "SRS bot updated."
    elif rc:
        head = f"SRS bot update failed (exit code {html.escape(rc)})."
    else:
        head = "SRS bot update stopped without finishing (killed or timed out)."
    tail = "\n".join(lines[-25:])[-3000:]
    text = head + (f"\n\n<pre>{html.escape(tail)}</pre>" if tail else "\n\nThe script printed nothing.")
    try:
        await bot.send_message(OWNER_ID, text, reply_markup=MENU)
        if len(lines) > 25:
            await bot.send_document(OWNER_ID, FSInputFile(SRS_UPDATE_LOG), caption="Full log")
    except TelegramAPIError as e:
        log.warning("sending SRS update result failed: %s", e)


async def watch_srs_update(bot: Bot) -> None:
    deadline = time.monotonic() + SRS_UPDATE_TIMEOUT + 60
    await asyncio.sleep(2)
    while time.monotonic() < deadline and not SRS_UPDATE_RC.exists() and await srs_update_active():
        await asyncio.sleep(3)
    await report_srs_update(bot)


@router.callback_query(F.data == "srsupdate")
async def cb_srsupdate(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY or S.switched:
        await cb.message.answer("Not now: a migration, rollback or wipe is running or unfinished.")
        return
    if not Path(SRS_UPDATE_SCRIPT).is_file():
        await cb.message.answer(f"<code>{SRS_UPDATE_SCRIPT}</code> is not on this server.")
        return
    if await srs_update_active():
        await cb.message.answer("An SRS bot update is already running. I'll send the result when it ends.")
        return
    LOG_DIR.mkdir(exist_ok=True)
    SRS_UPDATE_RC.unlink(missing_ok=True)
    SRS_UPDATE_LOG.write_text("")
    SRS_UPDATE_PENDING.touch()
    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--quiet", "--collect", f"--unit={SRS_UPDATE_UNIT}",
        f"--property=RuntimeMaxSec={SRS_UPDATE_TIMEOUT}", "--setenv=HOME=/root",
        "--setenv=DEBIAN_FRONTEND=noninteractive", "/bin/bash", "-c",
        f'sudo bash {SRS_UPDATE_SCRIPT} </dev/null > {SRS_UPDATE_LOG} 2>&1; echo $? > {SRS_UPDATE_RC}',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        SRS_UPDATE_PENDING.unlink(missing_ok=True)
        await cb.message.answer("Could not start the update: " + html.escape(out.decode(errors="replace")[:500]))
        return
    await cb.message.answer("Updating the SRS bot. I'll send the output when it's done.")
    asyncio.create_task(watch_srs_update(BOT))


def block_kb() -> InlineKeyboardMarkup:
    toggle = ("Turn alerts off", "blockalerts:0") if mon_enabled() else ("Turn alerts on", "blockalerts:1")
    return kb([("Check again", "blockcheck")], [toggle], [("Menu", "menu")])


def block_status_line() -> str:
    if not mon_enabled():
        return "Alerts are off."
    return (f"Alerts are on: I check every {mon_interval()} min and message you when this changes "
            f"(after two checks in a row agree).")


BLOCK_CHECK_BUSY = asyncio.Lock()


@router.callback_query(F.data == "blockcheck")
async def cb_blockcheck(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if BLOCK_CHECK_BUSY.locked():
        await cb.message.answer("A check is already running.")
        return
    async with BLOCK_CHECK_BUSY:
        note = await cb.message.answer(f"Asking test points in {country_name(mon_country())} and abroad to reach "
                                       f"<code>{CURRENT_IP}</code>. This takes a minute or two...")
        r = await run_block_check()
        try:
            await note.delete()
        except TelegramAPIError:
            pass
    ports = ", ".join(str(p) for p in mon_ports())
    await cb.message.answer(
        f"Server <code>{CURRENT_IP}</code> is {VERDICT_TEXT[r.verdict].format(c=country_name(mon_country()))}.\n\n"
        f"{round_details(r)}\n\nChecked: connecting to port {ports}. {block_status_line()}",
        reply_markup=block_kb())


@router.callback_query(F.data.startswith("blockalerts:"))
async def cb_blockalerts(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    set_env("MONITOR_ENABLED", cb.data.split(":", 1)[1])
    await cb.message.answer(block_status_line(), reply_markup=MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode not in BUSY and not S.switched:
        S.mode = "idle"
    await cb.message.answer(f"Macaw admin bot. Current server: <code>{CURRENT_IP}</code>.", reply_markup=MENU)


@router.callback_query(F.data.startswith("migrate_to:"))
async def cb_migrate_to(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY or S.switched:
        await cb.message.answer("Not now: a migration, rollback or wipe is running or unfinished.")
        return
    await drop_buttons(cb)
    S.ip, S.mode = cb.data.split(":", 1)[1], "confirm"
    S.password, S.password_saved = None, False
    await cb.message.answer(f"Migrating to <code>{S.ip}</code>. Is the VPN already set up on the new server?",
                            reply_markup=CONFIRM)


@router.callback_query(F.data == "newsrv")
async def cb_newsrv(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY:
        await cb.message.answer("Not now: something is already running.")
        return
    if not cloud_ready():
        await show_cloud_setup_start(cb.message)
        return
    S.mode = "newsrv_confirm"
    await cb.message.answer(
        "Create a new server at HostVDS?\n\n"
        f"Plan: {html.escape(CFG.get('HOSTVDS_FLAVOR_LABEL', CFG['HOSTVDS_FLAVOR']))}\n"
        f"Network: {html.escape(CFG.get('HOSTVDS_NETWORK_LABEL', CFG['HOSTVDS_NETWORK']))}\n"
        f"System: {html.escape(CFG.get('HOSTVDS_IMAGE_LABEL', CFG['HOSTVDS_IMAGE']))}\n\n"
        "HostVDS starts billing it right away.",
        reply_markup=kb([("Create", "newsrv_go"), ("Cancel", "cancel")], [("Change server settings", "cloud_setup")]))


@router.callback_query(F.data == "cloud_setup")
async def cb_cloud_setup(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode in BUSY:
        return
    await show_cloud_setup_start(cb.message)


@router.callback_query(F.data == "newsrv_go")
async def cb_newsrv_go(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "newsrv_confirm":
        return
    start_task(create_server(cb.bot))


@router.callback_query(F.data.startswith("cflav:"))
async def cb_cloud_flavor(cb: CallbackQuery):
    await cb.answer()
    if S.mode != "cloud_pick_flavor":
        return
    await drop_buttons(cb)
    f = S.cloud_lists["flavors"][int(cb.data.split(":")[1])]
    S.cloud_tmp["HOSTVDS_FLAVOR"], S.cloud_tmp["HOSTVDS_FLAVOR_LABEL"] = f["id"], flavor_label(f)
    S.mode = "cloud_pick_net"
    nets = S.cloud_lists["networks"]
    await cb.message.answer(f"Plan: {html.escape(flavor_label(f))}.\n\nNow pick the network (you want IPv4+IPv6):",
                            reply_markup=kb(*[[(f"{n.get('name') or n['id'][:8]} ({n['_label']})", f"cnet:{i}")]
                                              for i, n in enumerate(nets)], [("Cancel", "cancel")]))


async def finish_cloud_setup(message: Message, image: dict) -> None:
    S.cloud_tmp["HOSTVDS_IMAGE"], S.cloud_tmp["HOSTVDS_IMAGE_LABEL"] = image["id"], image.get("name", image["id"])
    for k, v in S.cloud_tmp.items():
        set_env(k, v)
    S.mode, S.cloud_tmp, S.cloud_lists = "idle", {}, {}
    await message.answer(
        "HostVDS is set up.\n\n"
        f"Plan: {html.escape(CFG['HOSTVDS_FLAVOR_LABEL'])}\n"
        f"Network: {html.escape(CFG['HOSTVDS_NETWORK_LABEL'])}\n"
        f"System: {html.escape(CFG['HOSTVDS_IMAGE_LABEL'])}",
        reply_markup=kb([("Create a server now", "newsrv")], [("Menu", "menu")]))


@router.callback_query(F.data.startswith("cnet:"))
async def cb_cloud_net(cb: CallbackQuery):
    await cb.answer()
    if S.mode != "cloud_pick_net":
        return
    await drop_buttons(cb)
    n = S.cloud_lists["networks"][int(cb.data.split(":")[1])]
    S.cloud_tmp["HOSTVDS_NETWORK"] = n["id"]
    S.cloud_tmp["HOSTVDS_NETWORK_LABEL"] = f"{n.get('name') or n['id'][:8]} ({n['_label']})"
    image = Cloud.pick_ubuntu_2404(S.cloud_lists["images"])
    if image:
        await finish_cloud_setup(cb.message, image)
        return
    S.mode = "cloud_pick_image"
    imgs = S.cloud_lists["images"][:30]
    await cb.message.answer("I couldn't find Ubuntu 24.04 by name. Pick the Ubuntu 24.04 image:",
                            reply_markup=kb(*[[(i.get("name", i["id"])[:60], f"cimg:{n}")] for n, i in enumerate(imgs)],
                                            [("Cancel", "cancel")]))


@router.callback_query(F.data.startswith("cimg:"))
async def cb_cloud_image(cb: CallbackQuery):
    await cb.answer()
    if S.mode != "cloud_pick_image":
        return
    await drop_buttons(cb)
    await finish_cloud_setup(cb.message, S.cloud_lists["images"][int(cb.data.split(":")[1])])


@router.callback_query(F.data.startswith("srvdel:"))
async def cb_srvdel(cb: CallbackQuery):
    await cb.answer()
    if S.mode in BUSY:
        return
    await drop_buttons(cb)
    S.del_ip, S.mode = cb.data.split(":", 1)[1], "srvdel_confirm"
    await cb.message.answer(f"Delete server <code>{S.del_ip}</code> at HostVDS? It is destroyed for good "
                            "and billing for it stops.",
                            reply_markup=kb([("Delete", "srvdel_go"), ("Cancel", "cancel")]))


@router.callback_query(F.data == "srvdel_go")
async def cb_srvdel_go(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "srvdel_confirm":
        return
    ip, S.mode = S.del_ip, "idle"
    try:
        live = await asyncio.to_thread(socket.gethostbyname, DOMAIN)
    except OSError:
        live = ""
    if ip in (CURRENT_IP, live):
        await cb.message.answer(f"<code>{ip}</code> is the live server. Not deleting it.", reply_markup=MENU)
        return
    try:
        c = Cloud(CFG)
        srv = await c.find_by_ip(ip)
        if not srv:
            await cb.message.answer(f"No server with IP <code>{ip}</code> in your HostVDS account. "
                                    "Delete it in the HostVDS panel if it's elsewhere.", reply_markup=MENU)
            return
        await c.delete_server(srv["id"])
    except Exception as e:
        await cb.message.answer(f"Deleting failed: {html.escape(str(e))[:500]}", reply_markup=MENU)
        return
    await cb.message.answer(f"Server <code>{ip}</code> ({html.escape(srv.get('name', ''))}) is being deleted "
                            "at HostVDS.", reply_markup=MENU)


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode in ("wipe_ask_ip", "wipe_confirm", "wipe_ask_pass", "wipe_failed",
                  "email_ask_user", "email_ask_pass", "email_ask_to",
                  "cloud_ask_openrc", "cloud_ask_pass", "cloud_pick_flavor", "cloud_pick_net", "cloud_pick_image",
                  "newsrv_confirm", "srvdel_confirm"):
        S.mode, S.wipe_password, S.email_password = "idle", None, ""
        S.cloud_tmp, S.cloud_lists = {}, {}
        await cb.message.answer("Cancelled.", reply_markup=MENU)
        return
    if S.mode not in ("ask_ip", "confirm", "ask_pass"):
        return
    if S.switched:
        S.mode = "failed"
        await cb.message.answer("Services on this server are still stopped.", reply_markup=failure_kb())
        return
    S.mode = "idle"
    await cb.message.answer("Cancelled.", reply_markup=MENU)


@router.callback_query(F.data == "start")
async def cb_start(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "confirm":
        return
    start_task(migration(cb.bot))


@router.callback_query(F.data == "retry")
async def cb_retry(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "failed" or not S.ip:
        return
    start_task(migration(cb.bot))


@router.callback_query(F.data == "rollback")
async def cb_rollback(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
    if S.mode != "failed" or not S.switched:
        return
    start_task(rollback(cb.bot))


@router.message()
async def on_message(message: Message):
    if S.mode == "cloud_ask_openrc":
        await accept_cloud_settings(message, parse_openrc(message.text or ""), message.text or "")
        return

    if S.mode == "cloud_ask_pass":
        try:
            await message.delete()
        except TelegramAPIError:
            await message.answer("I could not delete your message with the password. Please delete it yourself.")
        password = (message.text or "").strip()
        if not password:
            await message.answer("Send the API password as text.", reply_markup=CANCEL)
            return
        try:
            if not S.cloud_tmp.get("OS_AUTH_URL"):
                note = await message.answer("Looking up the HostVDS API address...")
                found = await find_auth_url(S.cloud_tmp["OS_USERNAME"], password, log.info)
                found.pop("OS_USERNAME", None)
                S.cloud_tmp.update(found)
                try:
                    await note.delete()
                except TelegramAPIError:
                    pass
            c = Cloud({**S.cloud_tmp, "OS_PASSWORD": password})
            await c.auth()
            if c.cfg.get("OS_PROJECT_ID"):
                S.cloud_tmp["OS_PROJECT_ID"] = c.cfg["OS_PROJECT_ID"]
            flavors, nets, images = await c.flavors(), await c.networks(), await c.images()
        except Exception as e:
            await message.answer(f"{html.escape(str(e))[:500]}\n\nSend the API password again to retry.",
                                 reply_markup=CANCEL)
            return
        if not flavors or not nets:
            await message.answer("Logged in, but HostVDS returned no plans or networks.", reply_markup=MENU)
            S.mode = "idle"
            return
        S.cloud_tmp["OS_PASSWORD"] = password
        S.cloud_lists = {"flavors": flavors[:40], "networks": nets, "images": images}
        S.mode = "cloud_pick_flavor"
        rows = []
        for i, f in enumerate(S.cloud_lists["flavors"]):
            mark = " <- 3 euro plan?" if (f.get("vcpus"), f.get("ram"), f.get("disk")) == (1, 2048, 20) else ""
            rows.append([(flavor_label(f) + mark, f"cflav:{i}")])
        await message.answer("Logged in to HostVDS. Pick the plan for new servers "
                             "(HostVDS-2 is 1 CPU, 2 GB RAM, 20 GB):",
                             reply_markup=kb(*rows, [("Cancel", "cancel")]))
        return

    if S.mode == "email_ask_pass":
        try:
            await message.delete()
        except TelegramAPIError:
            await message.answer("I could not delete your message with the password. Please delete it yourself.")
        password = (message.text or "").strip()
        if not password:
            await message.answer("Send the app password as text.", reply_markup=CANCEL)
            return
        try:
            await asyncio.to_thread(smtp_send, S.email_user, password)
        except smtplib.SMTPAuthenticationError:
            await message.answer("The mail server rejected that password. It must be an app password, "
                                 "not your normal one. Send it again.", reply_markup=CANCEL)
            return
        except Exception as e:
            await message.answer(f"Could not reach the mail server: {html.escape(str(e))[:300]}\n\n"
                                 "Send the app password again to retry.", reply_markup=CANCEL)
            return
        S.email_password, S.mode = password, "email_ask_to"
        await message.answer("Login works. Now send the address the files should go to.", reply_markup=CANCEL)
        return

    if S.mode == "email_ask_to":
        to = (message.text or "").strip()
        if not EMAIL_RE.match(to):
            await message.answer("That doesn't look like an email address. Send it again.", reply_markup=CANCEL)
            return
        set_env("SMTP_USER", S.email_user)
        set_env("SMTP_PASSWORD", S.email_password)
        set_env("MAIL_TO", to)
        S.email_password, S.mode = "", "idle"
        await message.answer(f"Email is set up. Send me a file and I'll offer to email it from "
                             f"{html.escape(S.email_user)} to {html.escape(to)}.", reply_markup=MENU)
        return

    if S.mode == "email_ask_user":
        addr = (message.text or "").strip().lower()
        if not EMAIL_RE.match(addr) or addr.rsplit("@", 1)[1] not in SMTP_HOSTS:
            await message.answer("Send a Gmail, Yandex or Mail.ru address, like name@gmail.com.", reply_markup=CANCEL)
            return
        S.email_user, S.mode = addr, "email_ask_pass"
        host = SMTP_HOSTS[addr.rsplit("@", 1)[1]]
        await message.answer(f"Now send {APP_PASSWORD_HELP[host]}\n\nI will delete your message right away.",
                             reply_markup=CANCEL)
        return

    if S.mode in ("ask_pass", "wipe_ask_pass"):
        # Delete the password first, before anything else can go wrong.
        try:
            await message.delete()
        except TelegramAPIError:
            await message.answer("I could not delete your message with the password. Please delete it yourself.")
        if not message.text or "\n" in message.text:
            await message.answer("Send the password as one line of text.", reply_markup=CANCEL)
            return
        if S.mode == "wipe_ask_pass":
            S.wipe_password = message.text
            start_task(wipe(message.bot))
            return
        S.password, S.password_saved = message.text, False
        start_task(migration(message.bot))
        return

    if S.mode == "wipe_ask_ip":
        try:
            addr = ipaddress.IPv4Address((message.text or "").strip())
        except ValueError:
            await message.answer("That is not a valid IPv4 address. Send it like 203.0.113.10.", reply_markup=CANCEL)
            return
        if str(addr) == CURRENT_IP:
            await message.answer("That is this server, the live one. Send the old server's IP.", reply_markup=CANCEL)
            return
        await ask_wipe_confirm(message, str(addr))
        return

    if S.mode == "ask_ip":
        text = (message.text or "").strip()
        try:
            addr = ipaddress.IPv4Address(text)
        except ValueError:
            await message.answer("That is not a valid IPv4 address. Send it like 203.0.113.10.", reply_markup=CANCEL)
            return
        if str(addr) == CURRENT_IP:
            await message.answer("That is this server's IP. Send the new server's IP.", reply_markup=CANCEL)
            return
        if not addr.is_global:
            await message.answer("That is a private or reserved address. Send the server's public IP.",
                                 reply_markup=CANCEL)
            return
        S.ip, S.mode = str(addr), "confirm"
        await message.answer(f"Migrating to <code>{S.ip}</code>. Is the VPN already set up on the new server?",
                             reply_markup=CONFIRM)
        return

    if S.mode in BUSY:
        await message.answer("A migration, rollback or wipe is running. Progress is in the message above.")
    elif S.mode == "failed":
        await message.answer("The last run failed.", reply_markup=failure_kb())
    else:
        await message.answer("Use the button below.", reply_markup=MENU)


# ---------- startup ----------

async def detect_ip() -> str:
    conn = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=10)) as s:
        for url in ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"):
            try:
                async with s.get(url) as r:
                    text = (await r.text()).strip()
                    ipaddress.IPv4Address(text)
                    return text
            except Exception:
                continue
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()[0]
    except Exception:
        return "unknown"


async def on_startup(bot: Bot):
    global CURRENT_IP
    CURRENT_IP = await detect_ip()
    try:
        version = (BASE / "VERSION").read_text().strip()
    except FileNotFoundError:
        version = "unknown"
    text = f"Admin bot is running on <code>{CURRENT_IP}</code> (version {html.escape(version)})"
    if HANDOFF_FAILED.exists():
        failed_ip = HANDOFF_FAILED.read_text().strip()
        HANDOFF_FAILED.unlink(missing_ok=True)
        text += (f"\n\nHandoff failed: could not start the admin bot on <code>{html.escape(failed_ip)}</code> "
                 "over SSH, so this one came back. The services themselves run on "
                 f"<code>{html.escape(failed_ip)}</code> and are stopped here.")
    try:
        await bot.send_message(OWNER_ID, text, reply_markup=MENU)
    except TelegramAPIError as e:
        log.warning("startup message failed (send /start to the bot once): %s", e)
    global MONITOR_TASK
    MONITOR_TASK = asyncio.create_task(monitor_loop(bot))
    if SRS_UPDATE_PENDING.exists():  # an SRS update was running when the bot restarted
        asyncio.create_task(watch_srs_update(bot))


MONITOR_TASK: asyncio.Task | None = None


BOT: Bot


async def main():
    global BOT
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(BOT, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
