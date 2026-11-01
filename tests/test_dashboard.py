"""dashboard/server.py: the demo replay end to end, over real HTTP on 127.0.0.1."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

from _load import load

srv = load("dashboard", "server")


class DemoDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cwd = os.getcwd()
        cls.d = tempfile.mkdtemp()
        cls.app = srv.App(os.path.join(cls.d, "demo"), True, 0)
        srv.Handler.app = cls.app
        cls.httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.chdir(cls.cwd)
        shutil.rmtree(cls.d)

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        h = {"Host": "127.0.0.1:%d" % self.port, "Content-Type": "application/json"}
        h.update(headers or {})
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def post(self, path, body=None):
        return self.call("POST", path, body or {})

    def test_1_page_and_status(self):
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"NSE<b>//</b>DAYTRADER", page)
        code, st = self.call("GET", "/api/status")
        self.assertEqual((st["mode"], st["source"], st["now_ist"]), ("demo", "demo", "2026-10-19 09:50:00"))
        self.assertTrue(st["session"]["open"])
        self.assertEqual(st["config"]["flat_by"], "15:00")

    def test_2_only_this_computer(self):
        code, _ = self.call("GET", "/api/status", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.call("POST", "/api/favorites", {"symbols": ["X"]}, headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_3_the_chart_stops_at_the_replay_clock(self):
        self.post("/api/clock", {"time": "09:50"})
        code, c = self.call("GET", "/api/chart?symbol=TSLA")
        self.assertEqual(code, 200)
        self.assertEqual(srv.datetime.datetime.fromtimestamp(c["bars"][-1]["t"], srv.datetime.timezone.utc).strftime("%H:%M"), "04:15")
        self.assertEqual(len(c["bars"]), 7)        # 9:15 to 9:45 closed; the forming bar is not drawn yet
        self.post("/api/clock", {"time": "11:30"})
        _, st = self.call("GET", "/api/status")
        self.assertEqual(st["now_ist"], "2026-10-19 10:45:00")      # the saved data ends there

    def test_4_a_paper_trade_end_to_end(self):
        self.post("/api/demo/reset")
        self.post("/api/clock", {"time": "10:10"})
        code, scan = self.post("/api/scan")
        self.assertEqual(code, 200)
        self.assertIn("TSLA", [h["symbol"] for h in scan["hits"]])
        code, e = self.post("/api/entry", {"symbol": "TSLA"})
        self.assertEqual(e["status"], "triggered")
        code, p = self.post("/api/plan")
        self.assertEqual((p["entry"], p["stop"], p["flat_by"]), (364.18, 358.3, "15:00"))
        code, tm = self.post("/api/timemachine")
        self.assertEqual(tm["mc"]["replays"], 10000)
        code, sz = self.post("/api/size")
        self.assertEqual((sz["sizing"]["qty"], sz["sizing"]["bound_by"]), (85, "max loss"))
        code, r = self.post("/api/risk")
        self.assertEqual(r["risk"]["results"][0]["verdict"], "PASS")
        self.assertTrue(r["tickets"][0]["ready"])

        code, a = self.post("/api/approve", {"index": 1, "reply": "sure"})
        self.assertFalse(a["ok"])                                   # only an explicit yes counts
        code, x = self.post("/api/execute", {"index": 1})
        self.assertFalse(x["ok"])                                   # no yes, no fill
        code, a = self.post("/api/approve", {"index": 1, "reply": "yes"})
        self.assertTrue(a["ok"], a)
        code, x = self.post("/api/execute", {"index": 1})
        self.assertTrue(x["ok"] and x["paper"], x)
        code, book = self.call("GET", "/api/book")
        self.assertEqual(book["positions"][0]["shares"], 85)
        self.assertEqual(book["limits"]["at_risk"], 499.8)

        self.post("/api/clock", {"time": "10:40"})
        code, f = self.post("/api/flatten", {"symbol": "TSLA"})
        self.assertEqual(f["risk"]["results"][0]["kind"], "exit")
        self.post("/api/approve", {"index": 1, "reply": "yes"})
        code, x = self.post("/api/execute", {"index": 1})
        self.assertTrue(x["ok"], x)
        code, book = self.call("GET", "/api/book")
        self.assertEqual(book["positions"], [])
        self.assertEqual(book["stats"]["trades"], 1)
        self.assertEqual(book["journal"][0]["qty"], 85)
        self.assertEqual(book["limits"]["at_risk"], 0)
        self.assertAlmostEqual(book["day_pnl"], book["journal"][0]["pnl"], places=2)

    def test_5_news_results_and_favourites(self):
        code, n = self.call("GET", "/api/news")
        self.assertTrue(n["items"])
        code, r = self.call("GET", "/api/analyst?symbol=DEMO")
        self.assertEqual(r["result"]["rows"][0]["fiscal"], "Q2 FY27")
        code, f = self.post("/api/favorites", {"symbols": ["tsla", "AAPL", "TSLA"]})
        self.assertEqual(f["favorites"], ["TSLA", "AAPL"])
        code, q = self.call("GET", "/api/quotes?symbols=TSLA,AAPL")
        self.assertEqual(set(q), {"TSLA", "AAPL"})

    def test_6_a_live_order_needs_the_symbol_typed(self):
        cfg = os.path.join("trader", "config.json")
        with open(cfg) as fh:
            c = json.load(fh)
        try:
            self.post("/api/demo/reset")
            self.post("/api/clock", {"time": "10:10"})
            for p, b in (("/api/entry", {"symbol": "TSLA"}), ("/api/plan", {}), ("/api/size", {}), ("/api/risk", {})):
                self.post(p, b)
            with open(cfg, "w") as fh:
                json.dump(dict(c, paper=False), fh)
            self.post("/api/risk")
            self.post("/api/approve", {"index": 1, "reply": "yes"})
            code, x = self.post("/api/execute", {"index": 1, "confirm": "AAPL"})
            self.assertEqual(code, 400)
            self.assertIn("type the symbol (TSLA)", x["error"])
        finally:
            with open(cfg, "w") as fh:
                json.dump(c, fh)


if __name__ == "__main__":
    unittest.main()
