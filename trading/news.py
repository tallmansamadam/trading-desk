"""RSS headline ingestion and sentiment scoring.

No API key and no dependency: public RSS feeds parsed with the stdlib, scored
with a finance-specific lexicon in the Loughran-McDonald tradition. General
purpose sentiment word lists are actively misleading on financial text —
"liability", "crude", "tender" and "capital" are neutral or positive there and
negative in ordinary English — so a finance lexicon is not a nicety.

WHAT THIS IS WORTH, stated plainly so nobody mistakes it for alpha:
  * Headline sentiment is the most heavily arbitraged signal in existence. By
    the time an RSS feed carries it, execution desks have traded it.
  * Bag-of-words scoring cannot read context. "Beats estimates but guides
    lower" scores positive on word count and is bearish in fact.
  * Feeds are minutes-to-hours late and carry no timestamp discipline.
Treat this as a RISK FILTER — a reason to stand aside when something is on fire
— rather than a reason to enter. That is the only use it reliably supports.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone

USER_AGENT = "Mozilla/5.0 (trading-desk research)"

FEEDS = {
    "yahoo_symbol": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
    "google_symbol": "https://news.google.com/rss/search?q={symbol}+stock&hl=en-US&gl=US&ceid=US:en",
    "google_market": "https://news.google.com/rss/search?q=stock+market&hl=en-US&gl=US&ceid=US:en",
}

# --- finance lexicon (Loughran-McDonald flavoured, trimmed to headline usage) --

NEGATIVE = {
    "loss", "losses", "losing", "lost", "decline", "declines", "declined",
    "declining", "fall", "falls", "fell", "falling", "drop", "drops", "dropped",
    "plunge", "plunges", "plunged", "slump", "slumps", "slumped", "tumble",
    "tumbles", "tumbled", "sink", "sinks", "sank", "crash", "crashes", "crashed",
    "weak", "weaker", "weakness", "miss", "misses", "missed", "cut", "cuts",
    "downgrade", "downgraded", "downgrades", "bearish", "recession", "layoff",
    "layoffs", "bankruptcy", "bankrupt", "default", "defaults", "lawsuit",
    "lawsuits", "probe", "investigation", "fraud", "warn", "warns", "warning",
    "warned", "risk", "risks", "risky", "concern", "concerns", "worried",
    "worry", "fear", "fears", "selloff", "sell-off", "correction", "volatile",
    "volatility", "uncertainty", "uncertain", "slowdown", "slowing", "shortfall",
    "deficit", "impairment", "writedown", "write-down", "restructuring",
    "delisting", "halt", "halted", "suspend", "suspended", "recall", "breach",
    "sue", "sued", "penalty", "fine", "fined", "subpoena", "resign", "resigned",
    "resignation", "scandal", "collapse", "collapsed", "plummet", "plummeted",
    "underperform", "disappointing", "disappoint", "disappointed", "negative",
    "worse", "worst", "struggle", "struggles", "struggling", "pressure",
    "headwind", "headwinds", "slash", "slashed", "freeze", "frozen", "crisis",
}

POSITIVE = {
    "gain", "gains", "gained", "rise", "rises", "rose", "rising", "surge",
    "surges", "surged", "jump", "jumps", "jumped", "soar", "soars", "soared",
    "rally", "rallies", "rallied", "climb", "climbs", "climbed", "advance",
    "advances", "advanced", "beat", "beats", "record", "records", "high",
    "highs", "strong", "stronger", "strength", "upgrade", "upgraded",
    "upgrades", "bullish", "outperform", "outperforms", "growth", "grow",
    "grows", "growing", "profit", "profits", "profitable", "boost", "boosts",
    "boosted", "raise", "raises", "raised", "expand", "expands", "expansion",
    "approve", "approved", "approval", "win", "wins", "won", "award",
    "awarded", "partnership", "breakthrough", "launch", "launches", "launched",
    "recovery", "recover", "recovered", "rebound", "rebounds", "rebounded",
    "positive", "better", "best", "exceed", "exceeds", "exceeded", "tailwind",
    "optimistic", "confidence", "dividend", "buyback", "upside", "momentum",
}

# Words that invert the polarity of the next few tokens.
NEGATORS = {"not", "no", "never", "without", "despite", "fails", "fail",
            "failed", "unable", "lacks", "lack", "denies", "denied", "avoids"}

# Words that amplify or damp the term that follows.
INTENSIFIERS = {"very": 1.5, "sharply": 1.5, "significantly": 1.4, "surges": 1.3,
                "slightly": 0.6, "marginally": 0.5, "modestly": 0.7}

TOKEN = re.compile(r"[a-z][a-z'-]+")


@dataclass
class Headline:
    title: str
    source: str
    link: str
    published: str
    score: float = 0.0
    hits: list[str] = field(default_factory=list)


def score_text(text: str) -> tuple[float, list[str]]:
    """Signed sentiment in roughly [-1, 1], plus the terms that drove it.

    Negation flips the following three tokens, which is crude but catches the
    common headline forms ("fails to beat", "not profitable").
    """
    words = TOKEN.findall(text.lower())
    if not words:
        return 0.0, []
    total, hits = 0.0, []
    flip_until = -1
    for i, w in enumerate(words):
        if w in NEGATORS:
            flip_until = i + 3
            continue
        weight = 1.0
        if i > 0 and words[i - 1] in INTENSIFIERS:
            weight = INTENSIFIERS[words[i - 1]]
        val = 0.0
        if w in POSITIVE:
            val = 1.0
        elif w in NEGATIVE:
            val = -1.0
        if val:
            if i <= flip_until:
                val = -val
                hits.append(f"NOT-{w}")
            else:
                hits.append(w)
            total += val * weight
    # squash so a long headline full of terms cannot dominate
    norm = total / (len(words) ** 0.5)
    return max(-1.0, min(1.0, norm)), hits


def _fetch(url: str, timeout: int = 12) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def parse_rss(xml_text: str, source: str) -> list[Headline]:
    out: list[Headline] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        title = html.unescape(title)
        out.append(Headline(
            title=title,
            source=source,
            link=(item.findtext("link") or "").strip(),
            published=(item.findtext("pubDate") or "").strip(),
        ))
    return out


class NewsFeed:
    """Fetches and scores headlines, with a cache so a trading loop polling
    every few seconds does not hammer the providers."""

    TTL = 120.0

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[Headline]]] = {}

    def headlines(self, symbol: str, limit: int = 25) -> list[Headline]:
        symbol = symbol.upper()
        now = time.time()
        hit = self._cache.get(symbol)
        if hit and now - hit[0] < self.TTL:
            return hit[1]

        items: list[Headline] = []
        seen: set[str] = set()
        for name in ("yahoo_symbol", "google_symbol"):
            raw = _fetch(FEEDS[name].format(symbol=symbol))
            if not raw:
                continue
            for h in parse_rss(raw, name):
                key = h.title.lower()[:90]
                if key in seen:
                    continue
                seen.add(key)
                h.score, h.hits = score_text(h.title)
                items.append(h)
        items = items[:limit]
        self._cache[symbol] = (now, items)
        return items

    def sentiment(self, symbol: str) -> dict:
        """Aggregate view for one symbol."""
        items = self.headlines(symbol)
        if not items:
            return {"symbol": symbol.upper(), "n": 0, "score": 0.0,
                    "label": "NO DATA", "negative": 0, "positive": 0,
                    "worst": None, "best": None}
        scored = [h for h in items if h.score != 0]
        avg = sum(h.score for h in scored) / len(scored) if scored else 0.0
        neg = sum(1 for h in items if h.score < -0.05)
        pos = sum(1 for h in items if h.score > 0.05)
        if avg <= -0.25:
            label = "BEARISH"
        elif avg <= -0.08:
            label = "LEAN BEAR"
        elif avg >= 0.25:
            label = "BULLISH"
        elif avg >= 0.08:
            label = "LEAN BULL"
        else:
            label = "NEUTRAL"
        ordered = sorted(items, key=lambda h: h.score)
        return {
            "symbol": symbol.upper(), "n": len(items), "scored": len(scored),
            "score": round(avg, 3), "label": label,
            "negative": neg, "positive": pos,
            "worst": ordered[0] if ordered else None,
            "best": ordered[-1] if ordered else None,
            "items": items,
        }

    def risk_veto(self, symbol: str, threshold: float = -0.25,
                  min_items: int = 4) -> tuple[bool, str]:
        """Should the desk stand aside on this symbol right now?

        This is the only use of headline sentiment defensible enough to wire
        into a trading decision: a brake, never an accelerator. It refuses to
        veto on thin evidence rather than guessing.
        """
        s = self.sentiment(symbol)
        if s["n"] < min_items:
            return False, f"only {s['n']} headlines — not enough to judge, no veto"
        if s["score"] <= threshold:
            worst = s["worst"].title[:70] if s["worst"] else ""
            return True, f"sentiment {s['score']:+.2f} ({s['label']}) — e.g. \"{worst}\""
        return False, f"sentiment {s['score']:+.2f} ({s['label']}) — no veto"
