"""Block monitor: checks from test points inside a country (Russia by default) whether this
server's IP can still be reached. Test points come from Globalping (api.globalping.io, community
probes on home and office connections, 250 tests an hour for free), with check-host.net as a
fallback when Globalping can't run a round.

Each round asks the in-country test points and a few test points abroad to open a TCP
connection to the IP (port 22 by default; check-host.net also pings it). The same points also connect to a reference site
that is known to work in that country, so a broken test point is ignored instead of read as a block.

Verdicts:
  ok       in-country test points reach the server
  partial  at least half of them can't, the rest can
  blocked  none of them can, while test points abroad can
  down     nobody can, abroad included (server or provider problem, not a block)
  unknown  not enough working test points, or neither service could run the check

This tells whether the IP is reachable from inside the country. It can't tell who blocked it,
and it can't see blocking aimed only at the VPN protocol while the IP stays reachable.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

API = "https://check-host.net"
HEADERS = {"Accept": "application/json", "User-Agent": "macaw-admin-monitor"}
CONTROL_COUNTRIES = ("de", "nl", "fi", "fr", "pl", "se", "gb", "ch", "at", "cz")
MAX_LOCAL = 6
MAX_CONTROL = 3
RESULT_WAIT = 90  # seconds to wait for test points to report
RESULT_FIRST_POLL = 8  # most test points report within a few seconds
RESULT_POLL = 10
REQUEST_GAP = 3  # seconds between check requests
LIMIT_RETRY_WAITS = (20, 40)  # seconds to wait before retrying after "limit_exceeded"
COUNTRY_NAMES = {"ru": "Russia", "by": "Belarus", "ir": "Iran", "cn": "China", "tr": "Turkey",
                 "kz": "Kazakhstan", "uz": "Uzbekistan", "tm": "Turkmenistan"}
DEFAULT_REFERENCE = {"ru": "ya.ru:443", "by": "ya.ru:443"}
FALLBACK_NODES = {
    "ru": ["ru1.node.check-host.net", "ru2.node.check-host.net", "ru3.node.check-host.net",
           "ru4.node.check-host.net"],
    "control": ["de1.node.check-host.net", "nl1.node.check-host.net", "fi1.node.check-host.net"],
}


class CheckError(Exception):
    pass


@dataclass
class NodeResult:
    node: str
    country: str
    city: str
    local: bool               # inside the watched country
    target_ok: bool | None    # None: the test point didn't answer
    ref_ok: bool | None

    @property
    def usable(self) -> bool:
        # A test point that can't reach the reference site is broken, so its answer means nothing.
        return self.target_ok is not None and self.ref_ok is True

    @property
    def place(self) -> str:
        return self.city or self.country.upper() or self.node.split(".")[0]


@dataclass
class Round:
    verdict: str
    nodes: list[NodeResult] = field(default_factory=list)
    error: str = ""
    source: str = ""

    def local(self) -> list[NodeResult]:
        return [n for n in self.nodes if n.local and n.usable]

    def abroad(self) -> list[NodeResult]:
        return [n for n in self.nodes if not n.local and n.usable]


# ---------- parsing check-host.net answers ----------

def ping_ok(result) -> bool | None:
    """check-ping result for one node: [[["OK", 0.04, "1.2.3.4"], ["TIMEOUT", 3.0], ...]].
    [null] means the node couldn't resolve or send; None means no answer yet."""
    if result is None:
        return None
    if not isinstance(result, list) or not result:
        return False
    for per_addr in result:
        if isinstance(per_addr, list):
            for probe in per_addr:
                if isinstance(probe, list) and probe and probe[0] == "OK":
                    return True
    return False


def tcp_ok(result) -> bool | None:
    """check-tcp result for one node: [{"time": 0.03, "address": "1.2.3.4"}] or [{"error": "..."}]."""
    if result is None:
        return None
    if not isinstance(result, list) or not result:
        return False
    return any(isinstance(r, dict) and "time" in r and not r.get("error") for r in result)


def combine(*vals: bool | None) -> bool | None:
    """Reachable if any check got through; unknown only if no check answered at all."""
    if any(v is True for v in vals):
        return True
    if all(v is None for v in vals):
        return None
    return False


def decide(nodes: list[NodeResult]) -> str:
    local = [n for n in nodes if n.local and n.usable]
    abroad = [n for n in nodes if not n.local and n.usable]
    if not local or not abroad:
        return "unknown"
    if not any(n.target_ok for n in abroad):
        return "down"
    failed = sum(1 for n in local if not n.target_ok)
    if failed == len(local):
        return "blocked"
    if failed * 2 >= len(local):
        return "partial"
    return "ok"


# ---------- check-host.net client ----------

class CheckHost:
    def __init__(self, country: str):
        self.country = country.lower()
        self._nodes: dict[str, tuple[str, str]] = {}  # node -> (country code, city)
        self._nodes_at = 0.0
        self._lock = asyncio.Lock()  # one round at a time: the free API has a rate limit
        self._last: Round | None = None
        self._last_at = 0.0

    async def _get(self, s: aiohttp.ClientSession, path: str, params=None):
        async with s.get(API + path, params=params, headers=HEADERS) as r:
            if r.status != 200:
                raise CheckError(f"check-host.net answered HTTP {r.status}")
            return await r.json(content_type=None)

    async def pick_nodes(self, s: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
        if time.time() - self._nodes_at > 3600 or not self._nodes:
            try:
                data = await self._get(s, "/nodes/hosts")
                nodes = {}
                for name, info in (data.get("nodes") or {}).items():
                    loc = info.get("location") or []
                    cc = str(loc[0]).lower() if loc else name.split(".")[0].rstrip("0123456789")
                    city = str(loc[2]) if len(loc) > 2 else ""
                    nodes[name] = (cc, city)
                if nodes:
                    self._nodes, self._nodes_at = nodes, time.time()
            except Exception:
                pass  # keep the old list or fall back below
        local = sorted(n for n, (cc, _) in self._nodes.items() if cc == self.country)[:MAX_LOCAL]
        control = []
        for want in CONTROL_COUNTRIES:
            if want == self.country:
                continue
            control += sorted(n for n, (cc, _) in self._nodes.items() if cc == want)[:1]
        control = control[:MAX_CONTROL]
        if not local and self.country in FALLBACK_NODES:
            local = FALLBACK_NODES[self.country]
        if not control:
            control = FALLBACK_NODES["control"]
        return local, control

    async def _check(self, s: aiohttp.ClientSession, kind: str, host: str, nodes: list[str]) -> dict:
        params = [("host", host)] + [("node", n) for n in nodes]
        for wait in LIMIT_RETRY_WAITS + (None,):
            start = await self._get(s, f"/check-{kind}", params)
            if start.get("error") != "limit_exceeded" or wait is None:
                break
            await asyncio.sleep(wait)
        rid = start.get("request_id")
        if start.get("error") == "limit_exceeded":
            raise CheckError("check-host.net is rate limiting this server, try again in a few minutes")
        if not rid:
            raise CheckError(f"check-host.net refused the {kind} check: {str(start)[:200]}")
        for name, info in (start.get("nodes") or {}).items():
            if name not in self._nodes and isinstance(info, list) and info:
                self._nodes[name] = (str(info[0]).lower(), str(info[2]) if len(info) > 2 else "")
        deadline = time.time() + RESULT_WAIT
        res: dict = {}
        await asyncio.sleep(RESULT_FIRST_POLL)
        while True:
            got = await self._get(s, f"/check-result/{rid}") or {}
            # Polling is rate limited too: then the answer is {"error": ...}, not results.
            # Earlier this was read as "no test point answered" and every point got ignored.
            if isinstance(got.get("error"), str):
                if got["error"] != "limit_exceeded":
                    raise CheckError(f"check-host.net: {got['error'][:100]}")
                if time.time() + LIMIT_RETRY_WAITS[0] > deadline:
                    raise CheckError("check-host.net is rate limiting this server")
                await asyncio.sleep(LIMIT_RETRY_WAITS[0])
                continue
            res = got
            if all(res.get(n) is not None for n in nodes) or time.time() + RESULT_POLL > deadline:
                break
            await asyncio.sleep(RESULT_POLL)
        if all(res.get(n) is None for n in nodes):
            raise CheckError(f"no test point returned the {kind} result within {RESULT_WAIT} s")
        return res

    async def run(self, ip: str, ports: list[int], reference: str, max_age: float = 0) -> Round:
        """Run one round, or return the last one if it finished less than max_age seconds ago."""
        async with self._lock:
            if self._last and time.time() - self._last_at < max_age:
                return self._last
            r = await self._run(ip, ports, reference)
            self._last, self._last_at = r, time.time()
            return r

    async def _run(self, ip: str, ports: list[int], reference: str) -> Round:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                local, control = await self.pick_nodes(s)
                if not local:
                    return Round("unknown", error=f"check-host.net has no test points in "
                                                  f"{country_name(self.country)}")
                nodes = local + control
                # One request at a time, a little apart: parallel ones trip check-host.net's rate limit.
                ping = await self._check(s, "ping", ip, nodes)
                await asyncio.sleep(REQUEST_GAP)
                ref = await self._check(s, "tcp", reference, nodes)
                tcps = []
                for p in ports:
                    await asyncio.sleep(REQUEST_GAP)
                    tcps.append(await self._check(s, "tcp", f"{ip}:{p}", nodes))
        except Exception as e:
            return Round("unknown", error=str(e) or type(e).__name__)
        results = []
        for n in nodes:
            cc, city = self._nodes.get(n, (n.split(".")[0].rstrip("0123456789"), ""))
            results.append(NodeResult(
                node=n, country=cc, city=city, local=n in local,
                target_ok=combine(ping_ok(ping.get(n)), *(tcp_ok(t.get(n)) for t in tcps)),
                ref_ok=tcp_ok(ref.get(n)),
            ))
        return Round(decide(results), results, source="check-host.net")


# ---------- Globalping ----------

GP_API = "https://api.globalping.io/v1"
GP_LOCAL_PROBES = 4
GP_CONTROL = ("DE", "NL", "FI")
GP_WAIT = 60


def gp_ok(result: dict) -> bool | None:
    """One probe's TCP ping result. None if the probe didn't run it (offline, still running)."""
    res = (result or {}).get("result") or {}
    status = res.get("status")
    if status == "finished":
        stats = res.get("stats") or {}
        if stats.get("rcv") is not None:
            return stats["rcv"] > 0
        if stats.get("loss") is not None:
            return stats["loss"] < 100
        return bool(res.get("timings"))
    if status == "failed":
        return False
    return None


def gp_key(result: dict) -> tuple:
    p = (result or {}).get("probe") or {}
    return (p.get("country"), p.get("city"), p.get("asn"), p.get("network"))


def split_host_port(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return (host, int(port)) if host and port.isdigit() else (target, 443)


class Globalping:
    def __init__(self, country: str, token: str = ""):
        self.country, self.token = country.lower(), token

    async def _req(self, s: aiohttp.ClientSession, method: str, path: str, body=None) -> tuple[int, dict]:
        headers = {"User-Agent": "macaw-admin-monitor", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with s.request(method, GP_API + path, json=body, headers=headers) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = {}
            return r.status, data if isinstance(data, dict) else {}

    async def _measure(self, s, target: str, port: int, locations) -> tuple[str, list[dict]]:
        body = {"type": "ping", "target": target, "locations": locations,
                "measurementOptions": {"packets": 3, "protocol": "TCP", "port": port}}
        status, data = await self._req(s, "POST", "/measurements", body)
        if status == 429:
            raise CheckError("Globalping's hourly limit is used up")
        if status not in (200, 201, 202) or not data.get("id"):
            err = data.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise CheckError(f"Globalping answered HTTP {status}: {(msg or '')[:150]}")
        mid = data["id"]
        deadline = time.time() + GP_WAIT
        m: dict = {}
        while time.time() < deadline:
            await asyncio.sleep(2)
            status, m = await self._req(s, "GET", f"/measurements/{mid}")
            if status == 200 and m.get("status") != "in-progress":
                break
        return mid, m.get("results") or []

    async def _group(self, s, ip: str, ports: list[int], ref: tuple[str, int], locations,
                     local: bool) -> list[NodeResult]:
        mid, first = await self._measure(s, ip, ports[0], locations)
        others = [(await self._measure(s, ip, p, mid))[1] for p in ports[1:]]  # same probes
        _, refs = await self._measure(s, ref[0], ref[1], mid)
        by_key: dict[tuple, list] = {}
        for r in refs:
            by_key.setdefault(gp_key(r), []).append(gp_ok(r))
        out = []
        for i, r in enumerate(first):
            key = gp_key(r)
            port_oks = [gp_ok(r)] + [gp_ok(o[i]) if i < len(o) and gp_key(o[i]) == key else None
                                     for o in others]
            ref_ok = by_key[key].pop(0) if by_key.get(key) else None
            p = r.get("probe") or {}
            city, net = p.get("city") or "", (p.get("network") or "")[:25]
            out.append(NodeResult(node=f"gp-{local}-{i}", country=str(p.get("country", "")).lower(),
                                  city=f"{city} ({net})" if net else city, local=local,
                                  target_ok=combine(*port_oks), ref_ok=ref_ok))
        return out

    async def run(self, ip: str, ports: list[int], reference: str) -> Round:
        ref = split_host_port(reference)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                local = await self._group(s, ip, ports, ref,
                                          [{"country": self.country.upper(), "limit": GP_LOCAL_PROBES}], True)
                abroad = await self._group(s, ip, ports, ref,
                                           [{"country": c, "limit": 1} for c in GP_CONTROL
                                            if c.lower() != self.country], False)
        except Exception as e:
            return Round("unknown", error=str(e) or type(e).__name__, source="Globalping")
        nodes = local + abroad
        return Round(decide(nodes), nodes, source="Globalping")


class Prober:
    """Globalping first, check-host.net if Globalping can't give an answer. One round at a time,
    and a round from the last max_age seconds is reused."""

    def __init__(self, country: str, token: str = ""):
        self.gp, self.ch = Globalping(country, token), CheckHost(country)
        self._lock = asyncio.Lock()
        self._last: Round | None = None
        self._last_at = 0.0

    async def run(self, ip: str, ports: list[int], reference: str, max_age: float = 0) -> Round:
        async with self._lock:
            if self._last and time.time() - self._last_at < max_age:
                return self._last
            r = await self.gp.run(ip, ports, reference)
            if r.verdict == "unknown":
                r2 = await self.ch._run(ip, ports, reference)
                if r2.verdict != "unknown":
                    r = r2
                else:
                    why_gp = r.error or "not enough working test points"
                    why_ch = r2.error or "not enough working test points"
                    r = Round("unknown", r.nodes + r2.nodes,
                              error=f"Globalping: {why_gp}. check-host.net: {why_ch}.")
            self._last, self._last_at = r, time.time()
            return r


def country_name(cc: str) -> str:
    return COUNTRY_NAMES.get(cc.lower(), cc.upper())


# ---------- turning rounds into alerts ----------

class Tracker:
    """Keeps the confirmed state across restarts and says when to alert.
    A new verdict must repeat `confirm` rounds in a row before it counts, so one bad round
    (a flaky test point, a short outage) doesn't page anyone."""

    def __init__(self, path: Path, confirm: int = 2, unknown_notice: int = 6):
        self.path, self.confirm, self.unknown_notice = path, confirm, unknown_notice
        self.ip, self.state, self.since = "", "", 0.0
        self.candidate, self.count, self.unknown_streak, self.unknown_sent = "", 0, 0, False
        try:
            d = json.loads(path.read_text())
            self.ip, self.state, self.since = d.get("ip", ""), d.get("state", ""), d.get("since", 0.0)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps({"ip": self.ip, "state": self.state, "since": self.since}))
        except OSError:
            pass

    def feed(self, ip: str, verdict: str, now: float | None = None) -> tuple[str, str, float] | None:
        """Returns (old_state, new_state, seconds_in_old_state) when an alert is due, else None.
        Returns ("", "unknown", 0) once when checks keep failing."""
        now = time.time() if now is None else now
        if ip != self.ip:  # new server (after a migration the state file comes along)
            self.ip, self.state, self.since = ip, "", now
            self.candidate, self.count = "", 0
            self.save()
        if verdict == "unknown":
            self.unknown_streak += 1
            if self.unknown_streak >= self.unknown_notice and not self.unknown_sent:
                self.unknown_sent = True
                return "", "unknown", 0
            return None
        self.unknown_streak, self.unknown_sent = 0, False
        if verdict == self.state:
            self.candidate, self.count = "", 0
            return None
        if verdict == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = verdict, 1
        if self.count < self.confirm:
            return None
        old, held = self.state, now - self.since if self.since else 0
        self.state, self.since, self.candidate, self.count = verdict, now, "", 0
        self.save()
        if not old and verdict == "ok":
            return None  # first look after start or a new server: all fine, nothing to say
        return old, verdict, held
