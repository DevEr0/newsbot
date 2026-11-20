"""Prices (yfinance) and charts (matplotlib, dark theme)."""
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yfinance as yf

log = logging.getLogger("market")

PERIODS = {"1d": ("1d", "5m"), "5d": ("5d", "15m"), "1m": ("1mo", "1d"),
           "6m": ("6mo", "1d"), "1y": ("1y", "1d"), "5y": ("5y", "1wk")}
BG, FG, GRID, UP, DOWN = "#0e1117", "#e6e6e6", "#262b36", "#26a69a", "#ef5350"
_cache = {}   # symbol -> (time, quote)


def history(sym, key):
    period, interval = PERIODS[key]
    df = yf.Ticker(sym).history(period=period, interval=interval)
    if df is None or df.empty:
        raise ValueError(f"no data for {sym}")
    return df


def _quote(sym):
    hit = _cache.get(sym)
    if hit and time.time() - hit[0] < 45:
        return hit[1]
    try:
        df = yf.Ticker(sym).history(period="5d", interval="1d")
        close = df["Close"].dropna()
        price, prev = float(close.iloc[-1]), float(close.iloc[-2])
        q = {"price": price, "prev": prev, "chg": (price / prev - 1) * 100, "date": close.index[-1].date()}
    except Exception as e:
        log.warning("quote %s failed: %s", sym, e)
        q = None
    _cache[sym] = (time.time(), q)
    return q


def quotes(symbols):
    symbols = list(dict.fromkeys(symbols))
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(symbols, ex.map(_quote, symbols)))


def next_earnings(sym):
    """Best-effort: yfinance's calendar is flaky, so failures are silent."""
    try:
        cal = yf.Ticker(sym).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        return dates[0] if dates else None
    except Exception:
        return None


def _style(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(color=GRID, linewidth=0.6)
    for s in ax.spines.values():
        s.set_color(GRID)


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def chart_png(sym, key="6m"):
    """Price + SMA20/50 + volume. X axis = bar index so overnight/weekend gaps don't draw as flat lines."""
    df = history(sym, key)
    close, vol = df["Close"], df["Volume"]
    first, last = float(close.iloc[0]), float(close.iloc[-1])
    chg = (last / first - 1) * 100
    color = UP if chg >= 0 else DOWN
    x = list(range(len(df)))

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True,
                                  gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.05}, facecolor=BG)
    _style(ax); _style(axv)
    ax.plot(x, close, color=color, linewidth=1.8)
    ax.fill_between(x, close, float(close.min()), color=color, alpha=0.10)
    if key in ("1m", "6m", "1y"):
        for w, c in ((20, "#ffb74d"), (50, "#64b5f6")):
            if len(close) > w:
                ax.plot(x, close.rolling(w).mean(), color=c, linewidth=1, label=f"SMA{w}")
        ax.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=8, loc="upper left")
    ax.set_title(f"{sym}  ${last:,.2f}  ({chg:+.2f}% over {key.upper()})", color=FG, fontsize=12, loc="left")
    axv.bar(x, vol, color=color, alpha=0.55, width=1.0)
    axv.set_yticks([])

    fmt = "%a %H:%M" if key in ("1d", "5d") else ("%Y-%m" if key == "5y" else "%b %d")
    ticks = [round(i * (len(df) - 1) / 5) for i in range(6)]
    axv.set_xticks(ticks)
    axv.set_xticklabels([df.index[i].strftime(fmt) for i in ticks])
    return _png(fig), {"last": last, "chg": chg}


def snapshot_png(rows, title="Market snapshot (1-day change)"):
    """rows = [(label, pct_change)] -> sorted horizontal bar chart."""
    rows = sorted([r for r in rows if r[1] is not None], key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(8, 0.38 * len(rows) + 1.3), facecolor=BG)
    _style(ax)
    ax.grid(axis="y", visible=False)
    vals = [r[1] for r in rows]
    ax.barh([r[0] for r in rows], vals, color=[UP if v >= 0 else DOWN for v in vals])
    for i, v in enumerate(vals):
        ax.text(v, i, f" {v:+.2f}%", va="center", ha="left" if v >= 0 else "right", color=FG, fontsize=8)
    ax.axvline(0, color=FG, linewidth=0.6)
    ax.tick_params(axis="y", labelsize=9)
    pad = max(abs(v) for v in vals) * 0.25 if vals else 1
    ax.set_xlim(min(vals + [0]) - pad, max(vals + [0]) + pad)
    ax.set_title(title, color=FG, fontsize=12, loc="left")
    return _png(fig)
