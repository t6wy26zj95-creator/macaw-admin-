"""Run: python3 -m unittest discover tests  (needs aiohttp)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402
from monitor import CheckHost, NodeResult, Tracker, decide, ping_ok, tcp_ok  # noqa: E402

IP = "203.0.113.10"
NODES = {
    "ru1.node.check-host.net": {"location": ["ru", "Russia", "Moscow"]},
    "ru2.node.check-host.net": {"location": ["ru", "Russia", "Saint Petersburg"]},
    "ru3.node.check-host.net": {"location": ["ru", "Russia", "Ekaterinburg"]},
    "de1.node.check-host.net": {"location": ["de", "Germany", "Frankfurt"]},
    "nl1.node.check-host.net": {"location": ["nl", "Netherlands", "Amsterdam"]},
    "us1.node.check-host.net": {"location": ["us", "USA", "Los Angeles"]},
}
PING_OK = [[["OK", 0.05, IP], ["OK", 0.05], ["OK", 0.05], ["OK", 0.05]]]
PING_FAIL = [[["TIMEOUT", 3.0], ["TIMEOUT", 3.0], ["TIMEOUT", 3.0], ["TIMEOUT", 3.0]]]
TCP_OK = [{"time": 0.04, "address": IP}]
TCP_FAIL = [{"error": "Connection timed out"}]


def node(local, target, ref=True):
    return NodeResult("x", "ru" if local else "de", "", local, target, ref)


class Parsing(unittest.TestCase):
    def test_ping(self):
        self.assertTrue(ping_ok(PING_OK))
        self.assertFalse(ping_ok(PING_FAIL))
        self.assertFalse(ping_ok([None]))
        self.assertIsNone(ping_ok(None))
        self.assertTrue(ping_ok([[["TIMEOUT", 3], ["OK", 0.1, IP]]]))

    def test_tcp(self):
        self.assertTrue(tcp_ok(TCP_OK))
        self.assertFalse(tcp_ok(TCP_FAIL))
        self.assertFalse(tcp_ok([]))
        self.assertIsNone(tcp_ok(None))


class Decide(unittest.TestCase):
    def test_verdicts(self):
        abroad = [node(False, True)]
        self.assertEqual(decide([node(True, True)] * 3 + abroad), "ok")
        self.assertEqual(decide([node(True, True)] * 3 + [node(True, False)] + abroad), "ok")
        self.assertEqual(decide([node(True, True), node(True, False)] + abroad), "partial")
        self.assertEqual(decide([node(True, False)] * 3 + abroad), "blocked")
        self.assertEqual(decide([node(True, False)] * 3 + [node(False, False)]), "down")
        self.assertEqual(decide([node(True, False)] * 3), "unknown")

    def test_broken_test_points_ignored(self):
        # A Russian test point that can't reach ya.ru either says nothing about our server.
        nodes = [node(True, False, ref=False), node(True, True), node(False, True)]
        self.assertEqual(decide(nodes), "ok")
        self.assertEqual(decide([node(True, False, ref=False), node(False, True)]), "unknown")


class FakeCheckHost(CheckHost):
    """Answers like check-host.net; `blocked` lists test points that can't reach IP."""

    def __init__(self, blocked=(), no_ref=(), limited=0):
        super().__init__("ru")
        self.blocked, self.no_ref, self.reqs = set(blocked), set(no_ref), {}
        self.limited, self.starts = limited, 0  # first `limited` check requests get limit_exceeded
        self.poll_limited, self.silent = 0, False  # result polls refused / test points never report

    async def _get(self, s, path, params=None):
        if path == "/nodes/hosts":
            return {"nodes": NODES}
        if path.startswith("/check-result/"):
            if self.poll_limited:
                self.poll_limited -= 1
                return {"error": "limit_exceeded"}
            if self.silent:
                return {n: None for n in self.reqs[path.rsplit("/", 1)[1]][2]}
            kind, host, nodes = self.reqs[path.rsplit("/", 1)[1]]
            out = {}
            for n in nodes:
                if host.startswith("ya.ru"):
                    out[n] = TCP_FAIL if n in self.no_ref else TCP_OK
                elif kind == "ping":
                    out[n] = PING_FAIL if n in self.blocked else PING_OK
                else:
                    out[n] = TCP_FAIL if n in self.blocked else TCP_OK
            return out
        self.starts += 1
        if self.starts <= self.limited:
            return {"error": "limit_exceeded"}
        kind = path.removeprefix("/check-")
        host = dict(params)["host"]
        nodes = [v for k, v in params if k == "node"]
        rid = f"r{len(self.reqs)}"
        self.reqs[rid] = (kind, host, nodes)
        return {"ok": 1, "request_id": rid, "nodes": {}}


class Client(unittest.TestCase):
    def setUp(self):
        self._sleep = monitor.asyncio.sleep

        async def no_sleep(_):
            return None
        monitor.asyncio.sleep = no_sleep

    def tearDown(self):
        monitor.asyncio.sleep = self._sleep

    def run_check(self, fake):
        return asyncio.run(fake.run(IP, [22], "ya.ru:443"))

    def test_picks_local_and_control_nodes(self):
        fake = FakeCheckHost()
        r = self.run_check(fake)
        self.assertEqual(r.verdict, "ok")
        self.assertEqual([n.place for n in r.local()], ["Moscow", "Saint Petersburg", "Ekaterinburg"])
        self.assertEqual([n.place for n in r.abroad()], ["Frankfurt", "Amsterdam"])  # no US: not a control country

    def test_blocked(self):
        r = self.run_check(FakeCheckHost(blocked={n for n in NODES if n.startswith("ru")}))
        self.assertEqual(r.verdict, "blocked")

    def test_partial_and_broken(self):
        r = self.run_check(FakeCheckHost(blocked={"ru1.node.check-host.net", "ru3.node.check-host.net"},
                                         no_ref={"ru3.node.check-host.net"}))
        self.assertEqual(r.verdict, "partial")  # ru3 ignored, ru1 down, ru2 up
        self.assertEqual(len(r.local()), 2)

    def test_retries_after_rate_limit(self):
        fake = FakeCheckHost(limited=2)
        self.assertEqual(self.run_check(fake).verdict, "ok")
        self.assertEqual(fake.starts, 5)  # 2 refused + ping, reference, port 22

    def test_rate_limited_result_polls_are_retried(self):
        fake = FakeCheckHost()
        fake.poll_limited = 2
        self.assertEqual(self.run_check(fake).verdict, "ok")

    def test_no_results_is_an_error_not_ignored_points(self):
        fake = FakeCheckHost()
        fake.silent = True
        old, monitor.RESULT_WAIT = monitor.RESULT_WAIT, 0
        try:
            r = self.run_check(fake)
        finally:
            monitor.RESULT_WAIT = old
        self.assertEqual(r.verdict, "unknown")
        self.assertIn("no test point returned", r.error)

    def test_gives_up_when_still_rate_limited(self):
        r = self.run_check(FakeCheckHost(limited=99))
        self.assertEqual(r.verdict, "unknown")
        self.assertIn("rate limiting", r.error)

    def test_recent_round_is_reused(self):
        fake = FakeCheckHost()

        async def twice():
            a = await fake.run(IP, [22], "ya.ru:443", max_age=120)
            b = await fake.run(IP, [22], "ya.ru:443", max_age=120)
            return a, b
        a, b = asyncio.run(twice())
        self.assertIs(a, b)
        self.assertEqual(fake.starts, 3)

    def test_api_failure_is_unknown(self):
        class Broken(CheckHost):
            async def _get(self, s, path, params=None):
                raise monitor.CheckError("check-host.net answered HTTP 503")
        r = asyncio.run(Broken("ru").run(IP, [22], "ya.ru:443"))
        self.assertEqual(r.verdict, "unknown")
        self.assertTrue(r.error)  # fallback nodes are used, then the check request fails


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "state.json"

    def test_alerts_only_on_confirmed_change(self):
        t = Tracker(self.path)
        self.assertIsNone(t.feed(IP, "ok", 0))
        self.assertIsNone(t.feed(IP, "ok", 600))  # first confirmed ok: silent
        self.assertIsNone(t.feed(IP, "blocked", 1200))  # one bad round: not yet
        self.assertIsNone(t.feed(IP, "ok", 1800))
        self.assertIsNone(t.feed(IP, "blocked", 2400))
        self.assertEqual(t.feed(IP, "blocked", 3000), ("ok", "blocked", 2400))
        self.assertIsNone(t.feed(IP, "blocked", 3600))
        self.assertIsNone(t.feed(IP, "unknown", 4200))  # unknown never flips the state
        self.assertIsNone(t.feed(IP, "ok", 4800))
        self.assertEqual(t.feed(IP, "ok", 5400), ("blocked", "ok", 2400))

    def test_state_survives_restart_and_resets_on_new_ip(self):
        t = Tracker(self.path)
        t.feed(IP, "blocked", 0)
        t.feed(IP, "blocked", 600)  # first confirmed state is blocked: alert
        t2 = Tracker(self.path)
        self.assertEqual(t2.state, "blocked")
        self.assertIsNone(t2.feed(IP, "blocked", 1200))  # no repeat after restart
        t2.feed("198.51.100.7", "ok", 1800)
        self.assertEqual(t2.state, "")

    def test_unknown_notice_once(self):
        t = Tracker(self.path, unknown_notice=3)
        out = [t.feed(IP, "unknown", i) for i in range(1, 6)]
        self.assertEqual(out, [None, None, ("", "unknown", 0), None, None])


# ---------- Globalping ----------

GP_PROBES = {
    "RU": [("RU", "Moscow", 12389, "Rostelecom"), ("RU", "Moscow", 8359, "MTS"),
           ("RU", "Saint Petersburg", 31213, "MegaFon")],
    "DE": [("DE", "Frankfurt", 24940, "Hetzner")], "NL": [("NL", "Amsterdam", 1136, "KPN")],
    "FI": [("FI", "Helsinki", 1759, "Telia")],
}


class FakeGlobalping(monitor.Globalping):
    def __init__(self, blocked=(), no_ref=(), no_ru=False, status=202):
        super().__init__("ru")
        self.blocked, self.no_ref, self.no_ru, self.status = set(blocked), set(no_ref), no_ru, status
        self.ms, self.posts = {}, 0

    async def _req(self, s, method, path, body=None):
        if method == "POST":
            self.posts += 1
            if self.status != 202:
                return self.status, {"error": {"message": "Too many requests"}}
            loc = body["locations"]
            if isinstance(loc, str):
                probes = self.ms[loc][0]
            else:
                probes = [p for l in loc for p in GP_PROBES.get(l["country"], [])[:l["limit"]]]
                if self.no_ru and any(l["country"] == "RU" for l in loc):
                    return 422, {"error": {"message": "No suitable probes found."}}
            mid = f"m{len(self.ms)}"
            self.ms[mid] = (probes, body["target"])
            return 202, {"id": mid, "probesCount": len(probes)}
        probes, target = self.ms[path.rsplit("/", 1)[1]]
        results = []
        for p in probes:
            bad = p[1] + "/" + p[3] in (self.no_ref if target == "ya.ru" else self.blocked)
            results.append({"probe": dict(zip(("country", "city", "asn", "network"), p)),
                            "result": {"status": "finished",
                                       "stats": {"total": 3, "rcv": 0 if bad else 3, "loss": 100 if bad else 0}}})
        return 200, {"id": "x", "status": "finished", "results": results}


class GlobalpingTest(unittest.TestCase):
    def setUp(self):
        self._sleep = monitor.asyncio.sleep

        async def no_sleep(_):
            return None
        monitor.asyncio.sleep = no_sleep

    def tearDown(self):
        monitor.asyncio.sleep = self._sleep

    def test_ok(self):
        gp = FakeGlobalping()
        r = asyncio.run(gp.run(IP, [22], "ya.ru:443"))
        self.assertEqual(r.verdict, "ok")
        self.assertEqual([n.place for n in r.local()],
                         ["Moscow (Rostelecom)", "Moscow (MTS)", "Saint Petersburg (MegaFon)"])
        self.assertEqual(len(r.abroad()), 3)
        self.assertEqual(gp.posts, 4)  # target + reference, for Russia and for abroad

    def test_blocked_and_broken_probe(self):
        gp = FakeGlobalping(blocked={"Moscow/Rostelecom", "Moscow/MTS", "Saint Petersburg/MegaFon"},
                            no_ref={"Moscow/MTS"})
        r = asyncio.run(gp.run(IP, [22], "ya.ru:443"))
        self.assertEqual(r.verdict, "blocked")
        self.assertEqual(len(r.local()), 2)

    def test_extra_port_counts(self):
        gp = FakeGlobalping()
        r = asyncio.run(gp.run(IP, [22, 443], "ya.ru:443"))
        self.assertEqual(r.verdict, "ok")
        self.assertEqual(gp.posts, 6)

    def test_falls_back_to_check_host(self):
        p = monitor.Prober("ru")
        p.gp = FakeGlobalping(no_ru=True)
        p.ch = FakeCheckHost()
        r = asyncio.run(p.run(IP, [22], "ya.ru:443"))
        self.assertEqual((r.verdict, r.source), ("ok", "check-host.net"))

    def test_both_fail_explains_both(self):
        p = monitor.Prober("ru")
        p.gp = FakeGlobalping(status=429)
        p.ch = FakeCheckHost(limited=99)
        r = asyncio.run(p.run(IP, [22], "ya.ru:443"))
        self.assertEqual(r.verdict, "unknown")
        self.assertIn("hourly limit", r.error)
        self.assertIn("rate limiting", r.error)


if __name__ == "__main__":
    unittest.main()
