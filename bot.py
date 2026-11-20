#!/usr/bin/env python3
"""AI & markets news bot for Telegram.

  python bot.py --get-chat-id   find your chat id (message the bot first)
  python bot.py --test          send a test message
  python bot.py --once          one pass (use this on GitHub Actions)
  python bot.py --loop          run forever (use this on a VPS / Pi / Docker)
"""
import argparse
import html
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import market
import sources
from state import State
from tg import Telegram

log = logging.getLogger("bot")
ET = ZoneInfo("America/New_York")
esc = html.escape
SYM_RE = re.compile(r"^[A-Z0-9.\-^=]{1,12}$")

HELP = """<b>📊 AI &amp; Markets bot</b>
/chart NVDA [1d 5d 1m 6m 1y 5y] – chart with period buttons
/price NVDA AMD – quick quotes
/watchlist – your list with today's move
/news NVDA – latest headlines now
/market – indices snapshot
/digest [premarket|close] – force a briefing
/add TICKER · /remove TICKER – edit watchlist
/status – bot health

Alerts arrive automatically: 🔴 breaking · 🟠 important · 🟢 notable.
Tap <b>📈 TICKER</b> under any alert for its chart."""


class Ctx:
    def __init__(self, cfg, state, tg):
        self.cfg, self.state, self.tg = cfg, state, tg
        self.tz = ZoneInfo(cfg.get("display_timezone", "America/New_York"))

    def watch(self):
        w = {s: dict(v or {}) for s, v in self.cfg["watchlist"].items()}
        for s in self.state.d["watch_add"]:
            w.setdefault(s, {"name": s})
        for s in self.state.d["watch_remove"]:
            w.pop(s, None)
        return w


# ------------------------------------------------------------------ formatting
def ago(dt):
    m = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    return "just now" if m < 1 else f"{m}m ago" if m < 60 else f"{m // 60}h ago"


def dot(pct):
    return "🟢" if pct >= 0 else "🔴"


def qline(label, q):
    return f"{dot(q['chg'])} <b>{esc(label)}</b>  {q['price']:,.2f}  ({q['chg']:+.2f}%)"


def chart_markup(sym):
    return {"inline_keyboard": [[{"text": k.upper(), "callback_data": f"c|{sym}|{k}"} for k in market.PERIODS]]}


def fmt_item(ctx, it, quotes):
    n = ctx.cfg["news"]
    if it.score >= n["breaking_score"]:
        head = "🔴 <b>BREAKING</b>"
    elif it.score >= n["breaking_score"] - 2:
        head = "🟠"
    else:
        head = "🟢"
    tags = " · ".join(f"<b>{t}</b>" for t in it.tickers[:4]) or "<b>MARKET</b>"
    lines = [f"{head} {tags}",
             f'<a href="{esc(it.link, quote=True)}">{esc(it.title)}</a>',
             f"<i>{esc(it.source)} · {ago(it.published)} · {it.published.astimezone(ctx.tz):%H:%M}</i>"]
    px = [qline(t, quotes[t]) for t in it.tickers[:3] if quotes.get(t)]
    if px:
        lines.append("\n".join(px))
    if it.reasons:
        lines.append("🏷 " + ", ".join(it.reasons))
    markup = None
    if it.tickers:
        markup = {"inline_keyboard": [[{"text": f"📈 {t}", "callback_data": f"c|{t}|5d"} for t in it.tickers[:3]]]}
    return "\n".join(lines), markup


# ------------------------------------------------------------------ jobs
def run_news(ctx):
    st, tg = ctx.state.d, ctx.tg
    watch = ctx.watch()
    seed = not st["seeded"]
    items = sources.fetch_all(sources.build_feeds(ctx.cfg, watch), force=seed)
    new = sources.process(items, watch, ctx.cfg, st, seed=seed)
    if seed:
        st["seeded"] = True
        tg.send_message("✅ <b>Bot is live.</b> Watching "
                        f"{len(watch)} tickers. Existing headlines were marked as read, only new ones will arrive.\n\n" + HELP)
        return
    if not new:
        return
    syms = sorted({t for it in new for t in it.tickers[:3]})
    quotes = market.quotes(syms) if syms else {}
    for it in new:
        text, markup = fmt_item(ctx, it, quotes)
        if tg.send_message(text, markup):
            st["sent_log"].append({"t": it.title, "l": it.link, "s": it.score, "tk": it.tickers, "ts": time.time()})
        else:                                   # Telegram failed -> let the next cycle retry it
            if it.uid in st["seen"]:
                st["seen"].remove(it.uid)
        time.sleep(0.4)                         # Telegram: max ~1 msg/sec per chat
    st["sent_log"] = st["sent_log"][-150:]


def market_open(now):
    return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)


def run_price_alerts(ctx):
    st, cfg = ctx.state.d, ctx.cfg["alerts"]
    now = datetime.now(ET)
    if not market_open(now) or time.time() - st["last_price_check"] < cfg["check_every_seconds"]:
        return
    st["last_price_check"] = time.time()
    st["price_alerts"] = {k: v for k, v in st["price_alerts"].items() if time.time() - v < 3 * 86400}
    step = cfg["price_move_pct"]
    for sym, q in market.quotes(list(ctx.watch())).items():
        if not q or q["date"] != now.date():
            continue
        bucket = int(abs(q["chg"]) // step)
        key = f"{now.date()}:{sym}:{'up' if q['chg'] > 0 else 'dn'}{bucket}"
        if bucket < 1 or key in st["price_alerts"]:
            continue
        st["price_alerts"][key] = time.time()
        word = "surging" if q["chg"] > 0 else "dropping"
        cap = (f"⚡ <b>{sym}</b> is {word}: <b>{q['chg']:+.2f}%</b> today\n"
               f"${q['price']:,.2f} (prev close ${q['prev']:,.2f})\n"
               "Check the news feed for the reason before acting.")
        try:
            png, _ = market.chart_png(sym, "1d")
            ctx.tg.send_photo(png, cap, chart_markup(sym))
        except Exception as e:
            log.warning("alert chart failed: %s", e)
            ctx.tg.send_message(cap)


def send_digest(ctx, name, d):
    cfg, st = ctx.cfg, ctx.state.d
    watch = ctx.watch()
    labels = {**cfg["indices"], **(d.get("extra") or {})}
    qs = market.quotes(list(labels) + list(watch))
    idx_rows = [(lab, qs[s]) for s, lab in labels.items() if qs.get(s)]
    w_rows = sorted([(s, qs[s]) for s in watch if qs.get(s)], key=lambda r: r[1]["chg"], reverse=True)
    if not idx_rows and not w_rows:
        ctx.tg.send_message(f"{d['title']}\n⚠️ Price data unavailable right now.")
        return
    cap = f"<b>{d['title']}</b>\n" + "\n".join(qline(l, q) for l, q in idx_rows)
    png = market.snapshot_png([(l, q["chg"]) for l, q in idx_rows] + [(s, q["chg"]) for s, q in w_rows])
    ctx.tg.send_photo(png, cap)

    parts = []
    if w_rows:
        parts.append("<b>Biggest movers (watchlist)</b>\n" + "\n".join(
            qline(s, q) for s, q in (w_rows[:2] + w_rows[-2:] if len(w_rows) > 4 else w_rows)))
    recent = [e for e in st["sent_log"] if time.time() - e["ts"] < 16 * 3600]
    top = sorted(recent, key=lambda e: e["s"], reverse=True)[:5]
    if top:
        parts.append("<b>Top stories since last briefing</b>\n" + "\n".join(
            f'• <a href="{esc(e["l"], quote=True)}">{esc(e["t"])}</a> <i>{" ".join(e["tk"][:3])}</i>' for e in top))
    today = datetime.now(ET).date()
    soon = []
    for s, e in zip(watch, market_map(market.next_earnings, list(watch))):
        e = e.date() if isinstance(e, datetime) else e
        if e and 0 <= (e - today).days <= 10:
            soon.append(f"• <b>{s}</b> reports {e:%a %b %d}")
    if soon:
        parts.append("📅 <b>Earnings in the next 10 days</b>\n" + "\n".join(soon))
    if parts:
        ctx.tg.send_message("\n\n".join(parts))


def market_map(fn, args):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(fn, args))


def run_digests(ctx):
    st = ctx.state.d
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return
    for name, d in ctx.cfg["digests"].items():
        h, m = map(int, d["time"].split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if sched <= now < sched + timedelta(hours=3) and st["digests"].get(name) != str(now.date()):
            st["digests"][name] = str(now.date())
            send_digest(ctx, name, d)


# ------------------------------------------------------------------ commands
def send_chart(ctx, sym, key, edit_msg=None):
    try:
        png, s = market.chart_png(sym, key)
    except Exception as e:
        ctx.tg.send_message(f"⚠️ No chart for <b>{esc(sym)}</b>: {esc(str(e))}")
        return
    cap = f"<b>{sym}</b> · {key.upper()} · ${s['last']:,.2f} ({s['chg']:+.2f}%)"
    if edit_msg:
        ctx.tg.edit_photo(edit_msg, png, cap, chart_markup(sym))
    else:
        ctx.tg.send_photo(png, cap, chart_markup(sym))


def quote_block(ctx, syms):
    qs = market.quotes(syms)
    ok = sorted([(s, q) for s, q in qs.items() if q], key=lambda r: r[1]["chg"], reverse=True)
    bad = [s for s, q in qs.items() if not q]
    text = "\n".join(qline(s, q) for s, q in ok) or "No data."
    return text + (f"\n⚠️ no data: {', '.join(bad)}" if bad else "")


def on_message(ctx, msg):
    if str(msg["chat"]["id"]) != ctx.tg.chat_id:
        return                                  # only YOU can talk to your bot
    parts = (msg.get("text") or "").split()
    if not parts or not parts[0].startswith("/"):
        return
    cmd, args = parts[0].split("@")[0].lower(), [a.upper() for a in parts[1:]]
    tg, st = ctx.tg, ctx.state.d
    syms = [a for a in args if SYM_RE.match(a) and a.lower() not in market.PERIODS]
    if cmd in ("/start", "/help"):
        tg.send_message(HELP)
    elif cmd == "/chart" and syms:
        key = next((a.lower() for a in args if a.lower() in market.PERIODS), "6m")
        send_chart(ctx, syms[0], key)
    elif cmd == "/price" and syms:
        tg.send_message(quote_block(ctx, syms))
    elif cmd == "/watchlist":
        tg.send_message("<b>Watchlist</b>\n" + quote_block(ctx, list(ctx.watch())))
    elif cmd == "/market":
        qs = market.quotes(list(ctx.cfg["indices"]))
        rows = [(lab, qs[s]) for s, lab in ctx.cfg["indices"].items() if qs.get(s)]
        if rows:
            tg.send_photo(market.snapshot_png([(l, q["chg"]) for l, q in rows]), "\n".join(qline(l, q) for l, q in rows))
        else:
            tg.send_message("⚠️ No index data right now.")
    elif cmd == "/news" and syms:
        name = ctx.watch().get(syms[0], {}).get("name", syms[0])
        its = sources.latest_for(syms[0], name)
        tg.send_message(f"<b>{syms[0]} · latest</b>\n" + "\n".join(
            f'• <a href="{esc(i.link, quote=True)}">{esc(i.title)}</a> <i>({esc(i.source)}, {ago(i.published)})</i>'
            for i in its) if its else f"No recent headlines for {syms[0]}.")
    elif cmd == "/digest":
        name = (args[0].lower() if args else "premarket")
        if name in ctx.cfg["digests"]:
            send_digest(ctx, name, ctx.cfg["digests"][name])
        else:
            tg.send_message("Use: /digest " + " | ".join(ctx.cfg["digests"]))
    elif cmd == "/add" and syms:
        s = syms[0]
        if not market.quotes([s])[s]:
            tg.send_message(f"⚠️ Can't find a price for {esc(s)}. Check the Yahoo Finance symbol.")
            return
        if s in st["watch_remove"]:
            st["watch_remove"].remove(s)
        if s not in ctx.cfg["watchlist"] and s not in st["watch_add"]:
            st["watch_add"].append(s)
        tg.send_message(f"➕ <b>{s}</b> added. News for it starts from the next cycle.")
    elif cmd == "/remove" and syms:
        s = syms[0]
        if s in st["watch_add"]:
            st["watch_add"].remove(s)
        elif s in ctx.cfg["watchlist"] and s not in st["watch_remove"]:
            st["watch_remove"].append(s)
        tg.send_message(f"➖ <b>{s}</b> removed.")
    elif cmd == "/status":
        tg.send_message(f"✅ alive · {len(ctx.watch())} tickers · {len(st['seen'])} headlines tracked · "
                        f"{len(st['sent_log'])} alerts in log · poll {poll_seconds(ctx)}s")
    else:
        tg.send_message("Unknown or incomplete command. /help")


def on_callback(ctx, cb):
    msg = cb.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != ctx.tg.chat_id:
        return
    ctx.tg.answer_callback(cb["id"])
    try:
        _, sym, key = (cb.get("data") or "").split("|")
    except ValueError:
        return
    if key in market.PERIODS and SYM_RE.match(sym):
        send_chart(ctx, sym, key, edit_msg=msg["message_id"] if msg.get("photo") else None)


def handle_updates(ctx, timeout=0):
    st = ctx.state.d
    ups = ctx.tg.get_updates(st["update_offset"], timeout)
    for u in ups:
        st["update_offset"] = u["update_id"] + 1
        try:
            if "message" in u:
                on_message(ctx, u["message"])
            elif "callback_query" in u:
                on_callback(ctx, u["callback_query"])
        except Exception:
            log.exception("update handling failed")
    if ups:
        ctx.state.save()


# ------------------------------------------------------------------ runner
def poll_seconds(ctx):
    now = datetime.now(ET)
    active = now.weekday() < 5 and 4 <= now.hour < 20
    return ctx.cfg["poll"]["active_seconds" if active else "idle_seconds"]


def cycle(ctx):
    for step in (run_news, run_price_alerts, run_digests):
        try:
            step(ctx)
        except Exception:
            log.exception("%s failed", step.__name__)
    ctx.state.save()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    for flag in ("--once", "--loop", "--get-chat-id", "--test"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default=os.getenv("STATE_PATH", "state.json"))
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN (env var, .env file or GitHub Secret).")
    tg = Telegram(token, os.getenv("TELEGRAM_CHAT_ID"))

    if a.get_chat_id:
        chats = {}
        for u in tg.get_updates(0, 0):
            m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
            if m.get("chat"):
                chats[m["chat"]["id"]] = m["chat"].get("username") or m["chat"].get("first_name", "?")
        print(chats or "No messages found. Send /start to your bot in Telegram, then run this again.")
        return
    if not tg.chat_id:
        sys.exit("Set TELEGRAM_CHAT_ID (run with --get-chat-id first).")
    if a.test:
        print("sent" if tg.send_message("✅ Test message from your market bot.") else "FAILED (check token/chat id)")
        return

    with open(a.config) as f:
        cfg = yaml.safe_load(f)
    ctx = Ctx(cfg, State(a.state), tg)

    if a.once:
        handle_updates(ctx, 0)
        cycle(ctx)
        return
    log.info("loop mode: polling every %ss while markets are active", cfg["poll"]["active_seconds"])
    while True:
        try:
            cycle(ctx)
        except Exception:
            log.exception("cycle crashed")
        end = time.time() + poll_seconds(ctx)
        while time.time() < end:              # long-poll Telegram: commands answer instantly
            try:
                handle_updates(ctx, timeout=int(min(25, max(1, end - time.time()))))
            except Exception:
                log.exception("update loop")
                time.sleep(5)


if __name__ == "__main__":
    main()
