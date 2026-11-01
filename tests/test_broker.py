import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(FIX, "trader-budget", "config-reel.json")   # the upstream reel's small account
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-tsla.json")
NOW = "2026-10-19 10:00"
TODAY = "2026-10-19"
AAPL = {"symbol": "AAPL", "side": "buy", "qty": "3", "type": "market", "time_in_force": "day", "product": "CNC"}
BATCH = ["AAPL", "AXP", "GOOGL", "KO", "BAC"]
TSLA_ORDER = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day", "product": "MIS",
              "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
# exactly what Claude sends to the Kite MCP server's place_order: the entry only
TSLA_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "TSLA", "transaction_type": "BUY",
             "quantity": 8, "product": "MIS", "order_type": "MARKET", "validity": "DAY"}
TSLA_STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "TSLA", "transaction_type": "SELL",
                  "quantity": 8, "product": "MIS", "order_type": "SL-M", "validity": "DAY", "trigger_price": 358.32}
TSLA_PLAIN = ("BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about "
              "₹46.88 if the stop fills at 358.32 (it can fill worse in a fast market)")


def tsla(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=AAPL, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes AAPL", "yes 3 AAPL",
                  "yes 3"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes AXP", "yes 4", "yes but make it 4", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 340"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "KO", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("yes sell 3 KO", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_bracket_yes(self):
        o = tsla()
        for r in ("yes", "yes 8 TSLA", "yes 358.32", "yes max loss 46.88", "yes target 370.04"):
            self.assertTrue(self.yes(r, o, ["TSLA"]), r)
        for r in ("yes 9", "yes 10 TSLA", "yes but stop at 357", "yes, stop 358.32", "yes AAPL", "sure"):
            self.assertFalse(self.yes(r, o, ["TSLA", "AAPL"]), r)


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_call(self):
        o = dict(AAPL, why="from a list")
        self.assertEqual(ticket.tool_input(o),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "AAPL", "transaction_type": "BUY",
                          "quantity": 3, "product": "CNC", "order_type": "MARKET", "validity": "DAY"})
        self.assertEqual(ticket.tool_input({"symbol": "M&M", "side": "sell", "qty": 3, "type": "limit",
                                            "limit_price": "3089.5", "time_in_force": "ioc", "product": "MIS"}),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "M&M", "transaction_type": "SELL",
                          "quantity": 3, "product": "MIS", "order_type": "LIMIT", "validity": "IOC", "price": 3089.5})
        stop = ticket.tool_input({"symbol": "KO", "side": "sell", "qty": "2", "type": "stop_limit",
                                  "stop_price": "88.10", "limit_price": "88.00"})
        self.assertEqual((stop["order_type"], stop["trigger_price"], stop["price"], stop["product"]),
                         ("SL", 88.1, 88.0, "MIS"))

    def test_the_bracket_call_is_the_entry_and_leaves_out_why_and_plan(self):
        self.assertEqual(ticket.tool_input(tsla()), TSLA_CALL)
        self.assertEqual(ticket.fingerprint(tsla()), ticket.fingerprint(TSLA_ORDER))
        self.assertEqual(ticket.tool_input(ticket.stop_leg(tsla())), TSLA_STOP_CALL)
        self.assertEqual(ticket.stop_leg(tsla(), qty=5)["qty"], "5")

    def test_plain_english(self):
        self.assertEqual(ticket.plain(AAPL), "BUY 3 shares of AAPL, market order, good for today, delivery (CNC)")
        self.assertEqual(ticket.plain({"symbol": "KO", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "95", "time_in_force": "ioc"}),
                         "SELL 1 share of KO, limit order at ₹95, immediate or cancel")
        self.assertEqual(ticket.plain({"symbol": "KO", "side": "sell", "qty": "2", "type": "stop",
                                       "stop_price": "88.1", "time_in_force": "day", "product": "MIS"}),
                         "SELL 2 shares of KO, stop-loss market (SL-M) order, trigger ₹88.10, good for today, "
                         "intraday (MIS)")
        self.assertEqual(ticket.plain(tsla()), TSLA_PLAIN)
        lim = tsla(type="limit", limit_price="364.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 8 TSLA, limit 364.00, good for today")
        self.assertEqual(ticket.max_loss(lim), (45.44, 358.32))
        no_plan = dict(TSLA_ORDER)
        self.assertEqual(ticket.max_loss(no_plan), (None, 358.32))


class DesktopConfig(unittest.TestCase):
    def test_example_is_zerodhas_kite_mcp_with_no_keys(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            server = json.load(fh)["mcpServers"]["kite"]
        self.assertEqual(server["command"], "npx")
        self.assertIn("https://mcp.kite.trade/mcp", server["args"])
        self.assertNotIn("env", server)                    # Kite's login happens in the browser, never in a file


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def live(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["paper"] = False
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def test_the_full_two_yes_flow_live(self):
        self.live()
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha Kite", text)
        for part in TSLA_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn("Claude will call place_order with exactly:", text)
        self.assertIn('"order_type": "MARKET"', text)
        self.assertIn("ticket.py protect writes the stop-loss (an SL-M sell at 358.32)", text)
        self.assertIn("could both fill and leave you short", text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn("Allow once", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Now call place_order", text)
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1]), TSLA_CALL)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "250101000123456",
                                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf,
                                      now="2026-10-19 10:01"))
        self.assertEqual(code, 0)
        self.assertIn("ticket.py protect --symbol TSLA --qty 8", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"]), ("yes", "entry", 46.88, TODAY))
        self.assertEqual(appr["at"], "2026-10-19T10:00:00+05:30")
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"]), ("250101000123456", 46.88, "entry"))
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 2086.4)
        self.assertEqual(pf["positions"][0]["shares"], 8)

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss ₹46.88", text)

        # trader-risk now counts it: a second TSLA buy today is a duplicate, and ₹46.88 is used
        res = check.check([tsla()], {"budget": 5000}, state=st, now=check.parse_now("2026-10-19 10:15"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 46.88))

        # the stop-loss: its own ticket, its own check, its own yes
        code, text, _ = run(["protect", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", "2026-10-19 10:02"])
        self.assertEqual(code, 0)
        self.assertIn("SELL 8 shares of TSLA, stop-loss market (SL-M) order, trigger ₹358.32", text)
        with open(self.orders) as fh:
            self.assertEqual(ticket.tool_input(json.load(fh)["orders"][0]), TSLA_STOP_CALL)
        self.assertEqual(self.run_risk(now="2026-10-19 10:02"), 0)
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")

    def test_paper_never_calls_kite(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("ORDER 1 of 1, PAPER (nothing goes to Zerodha), Zerodha Kite", text)
        self.assertIn("Paper: Claude does not call Kite", text)
        self.assertNotIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("do not call Kite", text)
        self.assertNotIn("Now call place_order", text)
        code, _, err = run(self.args("result", "--index", "1", "--status", "paper"))
        self.assertEqual(code, 1)
        self.assertIn("needs --filled-qty and --filled-price", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "paper", "--filled-qty", "8",
                                      "--filled-price", "364.20", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "paper", "filled"])
        with open(self.pf) as fh:
            self.assertEqual(json.load(fh)["cash"], 2086.4)

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "abc",
                      "--filled-qty", "8", "--filled-price", "364.20", "--portfolio", self.pf))
        late = "2026-10-19 15:15"
        code, text, _ = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 8 shares of TSLA, market order, good for today, intraday (MIS) (flat by 15:15 IST)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancel it FIRST", text)
        self.assertIn("cancel_order", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(ticket.tool_input(doc["orders"][0]),
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "TSLA", "transaction_type": "SELL",
                          "quantity": 8, "product": "MIS", "order_type": "MARKET", "validity": "DAY"})
        self.assertEqual(self.run_risk(now=late), 0)       # at the 15:15 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 8 TSLA", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "8",
                                   "--filled-price", "366.10", "--portfolio", self.pf, now="2026-10-19 15:16"))
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual((pf["cash"], pf["positions"]), (5015.2, []))
        self.assertEqual(check.day_trades(self.state_doc(), TODAY), (1, ["2026-10-19 TSLA"]))
        code, _, err = run(["flatten", "--symbol", "TSLA", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "tsla", "--qty", "8", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_positions", text)
        self.assertEqual(run(["flatten", "--symbol", "TSLA", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "9"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 10:00")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:25"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:25"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 09:25"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_protect_needs_a_stop_and_whole_shares(self):
        code, _, err = run(["protect", "--symbol", "INFY", "--qty", "5", "--orders", self.orders,
                            "--plan", PLAN_PATH, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no stop for INFY", err)
        code, _, err = run(["protect", "--symbol", "TSLA", "--qty", "2.5", "--orders", self.orders,
                            "--plan", PLAN_PATH, "--config", self.cfg, "--now", NOW])
        self.assertIn("whole shares", err)
        code, text, _ = run(["protect", "--symbol", "INFY", "--qty", "5", "--stop", "1480.20", "--orders",
                             self.orders, "--plan", PLAN_PATH, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertIn("trigger ₹1,480.20", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "KO", "shares": 3, "lots": [
            {"id": "KO-1", "date": "2025-01-02", "shares": 2, "cost": 120.0},
            {"id": "KO-2", "date": "2026-01-02", "shares": 1, "cost": 70.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "2.5", "--price", "90",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 225.0)
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["KO-2"])
        self.assertEqual(lots[0]["shares"], 0.5)
        self.assertEqual(lots[0]["cost"], 35.0)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "KO", "--side", "sell", "--qty", "5", "--price", "90",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
