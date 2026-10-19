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
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

BASE = Path("/opt/macaw-admin")
ENV_FILE = BASE / ".env"
LOG_DIR = BASE / "logs"
LOCK = "/run/macaw-migrate.lock"
HANDOFF_FAILED = BASE / "handoff-failed"
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
    """Replace ROOT_PASSWORD in .env (single-quoted so any characters survive `source`)."""
    quoted = "'" + password.replace("'", "'\\''") + "'"
    lines = ENV_FILE.read_text().splitlines()
    newline = f"ROOT_PASSWORD={quoted}"
    for i, line in enumerate(lines):
        if line.startswith("ROOT_PASSWORD="):
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


MENU = kb([("Migrate to a new server", "migrate")])
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
    mode: str = "idle"
    ip: str = ""
    password: str | None = None   # override sent via NEEDPASS
    password_saved: bool = False
    switched: bool = False        # some attempt reached step 6: services here are stopped
    task: asyncio.Task | None = None


S = State()
CURRENT_IP = ""
BUSY = {"running", "rolling_back", "handoff"}


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


async def send_failure(bot: Bot, res: RunResult, what: str, total: int) -> None:
    where = f" at step {res.last_step}/{total}" if res.last_step else ""
    if res.timed_out:
        text = f"{what} timed out after {SCRIPT_TIMEOUT // 60} minutes{where}."
    elif res.fail:
        text = f"{what} failed{where}: {html.escape(res.fail)}"
    else:
        text = f"{what} stopped unexpectedly{where} (exit code {res.rc})."
    if S.switched:
        text += ("\n\nServices on this server are stopped. Retry to continue the migration, "
                 "or Roll back to bring them back here.")
    text += "\n\nFull log attached."
    if len(text) > 1000:
        text = text[:990] + "..."
    try:
        await bot.send_document(OWNER_ID, FSInputFile(res.log), caption=text, reply_markup=failure_kb())
    except TelegramAPIError as e:
        log.warning("sending log failed: %s", e)
        await bot.send_message(OWNER_ID, text + f"\nLog: {res.log}", reply_markup=failure_kb())


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

    extra = {"ROOT_PASSWORD_OVERRIDE": S.password} if S.password else None
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
        f"Reminder: the old server <code>{CURRENT_IP}</code> still holds the data with tokens.",
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


# ---------- handlers ----------

router = Router()
router.message.filter(F.from_user.id == OWNER_ID, F.chat.type == "private")
router.callback_query.filter(F.from_user.id == OWNER_ID)


async def drop_buttons(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass


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


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    await cb.answer()
    await drop_buttons(cb)
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
    if S.mode == "ask_pass":
        # Delete the password first, before anything else can go wrong.
        try:
            await message.delete()
        except TelegramAPIError:
            await message.answer("I could not delete your message with the password. Please delete it yourself.")
        if not message.text or "\n" in message.text:
            await message.answer("Send the password as one line of text.", reply_markup=CANCEL)
            return
        S.password, S.password_saved = message.text, False
        start_task(migration(message.bot))
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
        await message.answer("A migration or rollback is running. Progress is in the message above.")
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
    text = f"Admin bot is running on <code>{CURRENT_IP}</code>"
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
