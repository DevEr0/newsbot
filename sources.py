"""News ingestion: fetch RSS/Atom feeds in parallel, dedupe, score by relevance to YOUR stocks."""
import calendar
import hashlib
import logging
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests

log = logging.getLogger("sources")
UA = "Mozilla/5.0 (compatible; AIMarketBot/1.0)"
_last_fetch = {}   # url -> unix time (only matters for --loop mode)


@dataclass
class Item:
    uid: str
    title: str
    link: str
    source: str
    published: datetime
    tickers: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    score: int = 0
    topic: bool = False
    feed_tickers: list = field(default_factory=list)


# ---------------------------------------------------------------- feeds
def gnews(query):
    q = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"https://news.google.com/rss/search?{q}"


def build_feeds(cfg, watch):
    n = cfg["news"]
    feeds = []
    for sym, meta in watch.items():
        name = (meta or {}).get("name", sym)
        query = f'"{name}" stock when:1d' if name != sym else f"{sym} stock when:1d"
        feeds.append({"url": gnews(query), "tickers": [sym], "every": n["google_every_seconds"]})
        feeds.append({"url": f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(sym)}"
                             f"&region=US&lang=en-US", "tickers": [sym], "every": 60, "src": "Yahoo Finance"})
    for q in n.get("topic_queries", []):
        feeds.append({"url": gnews(f"{q} when:1d"), "topic": True, "every": n["google_every_seconds"]})
    for url in n.get("extra_feeds", []):
        feeds.append({"url": url, "topic": True, "every": 120})
    sec = cfg.get("sec", {})
    if sec.get("enabled") and os.getenv("SEC_USER_AGENT"):
        for sym, cik in sec.get("ciks", {}).items():
            feeds.append({"url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
                                 f"{cik}&type=8-K&dateb=&owner=include&count=10&output=atom",
                          "tickers": [sym], "every": 120, "src": f"SEC 8-K ({sym})", "sec": True})
    return feeds


def _fetch(feed, force):
    now = time.time()
    if not force and now - _last_fetch.get(feed["url"], 0) < feed.get("every", 60) - 1:
        return []
    _last_fetch[feed["url"]] = now
    headers = {"User-Agent": os.getenv("SEC_USER_AGENT", UA) if feed.get("sec") else UA}
    try:
        r = requests.get(feed["url"], headers=headers, timeout=15)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:
        log.warning("feed failed (%s): %s", feed["url"][:70], e)
        return []
    default_src = feed.get("src") or (parsed.feed.get("title", "") if parsed.feed else "") or "web"
    out = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        link = e.get("link") or ""
        if not title:
            continue
        src = (e.get("source") or {}).get("title") or default_src      # Google News gives the outlet here
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        ts = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc) if ts else datetime.now(timezone.utc)
        out.append(Item(uid=hashlib.sha1((link or title).encode()).hexdigest()[:16], title=title, link=link,
                        source=src, published=pub, topic=bool(feed.get("topic")),
                        feed_tickers=list(feed.get("tickers", []))))
    return out


def fetch_all(feeds, force=False):
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = ex.map(lambda f: _fetch(f, force), feeds)
    return [i for batch in results for i in batch]


# ---------------------------------------------------------------- scoring
def _norm(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())


def _matchers(watch):
    m = {}
    for sym, meta in watch.items():
        meta = meta or {}
        names = [meta.get("name", sym)] + list(meta.get("aliases", []))
        pats = [re.compile(rf"\b{re.escape(x)}\b", re.I) for x in names if x and x != sym]
        m[sym] = (re.compile(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])"), pats)  # symbol: case-sensitive
    return m


def _keywords(cfg):
    return [(int(w), word, re.compile(rf"\b{re.escape(str(word))}\b", re.I))
            for w, words in cfg["keywords"].items() for word in words]


def score_item(it, matchers, kws, cfg):
    n = cfg["news"]
    tick = {s for s, (sym_re, pats) in matchers.items()
            if sym_re.search(it.title) or any(p.search(it.title) for p in pats)}
    tick |= set(it.feed_tickers)
    hits = sorted({(w, word) for w, word, rx in kws if rx.search(it.title)}, reverse=True)
    score = 2 if tick else (1 if it.topic else 0)
    score += min(sum(w for w, _ in hits), n.get("keyword_cap", 6))
    if any(t in it.source.lower() for t in n.get("trusted_sources", [])):
        score += 1
    it.tickers = sorted(tick)
    it.reasons = [w for _, w in hits[:3]]
    it.score = score
    return it


def process(items, watch, cfg, st, seed=False):
    """Return NEW, relevant, de-duplicated items (oldest first). Marks everything examined as seen."""
    n = cfg["news"]
    now = datetime.now(timezone.utc)
    seen = set(st["seen"])
    tsets = [set(t.split()) for t in st["titles"][-300:]]
    matchers, kws = _matchers(watch), _keywords(cfg)
    out = []
    for it in sorted(items, key=lambda i: i.published):
        if it.uid in seen:
            continue
        seen.add(it.uid)
        st["seen"].append(it.uid)
        if (now - it.published).total_seconds() > n["max_age_hours"] * 3600:
            continue
        norm = _norm(it.title)
        toks = set(norm.split())
        if len(toks) >= 4 and any(len(toks & t) / len(toks | t) >= 0.75 for t in tsets):
            continue                                   # same story from another outlet
        score_item(it, matchers, kws, cfg)
        if it.score < n["min_score"]:
            continue
        st["titles"].append(norm)
        tsets.append(toks)
        out.append(it)
    st["seen"] = st["seen"][-6000:]
    st["titles"] = st["titles"][-400:]
    if seed:
        return []
    if len(out) > n["max_per_cycle"]:
        out = sorted(out, key=lambda i: i.score, reverse=True)[: n["max_per_cycle"]]
    return sorted(out, key=lambda i: i.published)


def latest_for(sym, name, limit=6):
    """For /news TICKER: newest headlines, no scoring/dedupe vs. history."""
    q = f'"{name}" stock when:1d' if name != sym else f"{sym} stock when:1d"
    feeds = [{"url": gnews(q), "tickers": [sym]},
             {"url": f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(sym)}"
                     f"&region=US&lang=en-US", "tickers": [sym], "src": "Yahoo Finance"}]
    items = fetch_all(feeds, force=True)
    uniq, seen = [], []
    for it in sorted(items, key=lambda i: i.published, reverse=True):
        toks = set(_norm(it.title).split())
        if any(len(toks & s) / max(1, len(toks | s)) >= 0.75 for s in seen):
            continue
        seen.append(toks)
        uniq.append(it)
    return uniq[:limit]
