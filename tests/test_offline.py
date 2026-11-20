"""Offline smoke tests (no network, no Telegram). Run:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import yaml

for name in ("feedparser", "yfinance"):          # allow running without the heavy deps installed
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = types.ModuleType(name)

import bot, market, sources   # noqa: E402
from state import State        # noqa: E402

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))


def fake_history(period="6mo", interval="1d"):
    n = {"1d": 78, "5d": 130, "1mo": 22, "6mo": 126, "1y": 252, "5y": 260}[period]
    idx = pd.date_range(end=pd.Timestamp.now(tz="America/New_York"), periods=n, freq="15min" if n in (78, 130) else "D")
    close = 100 + np.cumsum(np.random.randn(n))
    return pd.DataFrame({"Close": close, "Volume": np.random.randint(1e6, 5e6, n)}, index=idx)


class FakeTicker:
    def __init__(self, sym): self.sym = sym
    def history(self, period="5d", interval="1d"): return fake_history(period, interval)


class FakeTG:
    chat_id = "1"
    def __init__(self): self.sent = []
    def send_message(self, text, markup=None, **k): self.sent.append(("msg", text)); return {"ok": 1}
    def send_photo(self, png, caption="", markup=None, **k): self.sent.append(("photo", caption)); return {"ok": 1}
    def edit_photo(self, *a, **k): self.sent.append(("edit", a[2] if len(a) > 2 else "")); return {"ok": 1}
    def answer_callback(self, *a, **k): pass


def item(title, mins_old=5, src="Reuters", ticks=()):
    return sources.Item(uid=title[:12] + str(mins_old), title=title, link="https://x.test/" + title[:5], source=src,
                        published=datetime.now(timezone.utc) - timedelta(minutes=mins_old), feed_tickers=list(ticks))


class Tests(unittest.TestCase):
    def setUp(self):
        self.watch = CFG["watchlist"]
        self.st = State(os.path.join(tempfile.mkdtemp(), "s.json")).d

    def test_scoring_and_dedupe(self):
        items = [item("Nvidia beats earnings estimates, raises guidance"),
                 item("NVIDIA beats earnings estimates raises guidance", src="CNBC"),     # same story
                 item("Local bakery opens new store", ticks=()),                         # irrelevant
                 item("Nvidia stock edges higher", mins_old=600)]                        # too old
        out = sources.process(items, self.watch, CFG, self.st)
        self.assertEqual(len(out), 1)
        self.assertIn("NVDA", out[0].tickers)
        self.assertGreaterEqual(out[0].score, CFG["news"]["breaking_score"])
        self.assertEqual(sources.process(items, self.watch, CFG, self.st), [])           # already seen

    def test_seed_returns_nothing_but_marks_seen(self):
        its = [item("Fed signals rate cut as CPI cools")]
        self.assertEqual(sources.process(its, self.watch, CFG, self.st, seed=True), [])
        self.assertTrue(self.st["seen"])

    def test_charts(self):
        with mock.patch.object(market.yf, "Ticker", FakeTicker, create=True):
            for k in market.PERIODS:
                png, s = market.chart_png("NVDA", k)
                self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(market.snapshot_png([("A", 1.2), ("B", -2.0)]).startswith(b"\x89PNG"))

    def test_commands(self):
        tg = FakeTG()
        ctx = bot.Ctx(CFG, State(os.path.join(tempfile.mkdtemp(), "s.json")), tg)
        msg = lambda t: {"chat": {"id": 1}, "text": t}
        with mock.patch.object(market.yf, "Ticker", FakeTicker, create=True):
            for c in ("/start", "/chart nvda 5d", "/chart 1y amd", "/price nvda amd", "/watchlist", "/market",
                      "/add shop", "/remove nvda", "/status"):
                bot.on_message(ctx, msg(c))
            bot.on_message(ctx, {"chat": {"id": 999}, "text": "/start"})                    # stranger: ignored
        self.assertNotIn("NVDA", ctx.watch())
        self.assertIn("SHOP", ctx.watch())
        self.assertEqual(len(tg.sent), 9)

    def test_full_cycle(self):
        tg = FakeTG()
        ctx = bot.Ctx(CFG, State(os.path.join(tempfile.mkdtemp(), "s.json")), tg)
        ctx.state.d["seeded"] = True
        feed = [item("Nvidia unveils new GPU, analysts raise price target", ticks=["NVDA"])]
        with mock.patch.object(market.yf, "Ticker", FakeTicker, create=True), \
             mock.patch.object(sources, "fetch_all", return_value=feed):
            bot.cycle(ctx)
        self.assertTrue(any("NVDA" in t for k, t in tg.sent if k == "msg"))


if __name__ == "__main__":
    unittest.main()
