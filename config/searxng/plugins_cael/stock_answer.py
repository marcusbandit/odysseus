# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style stock quote instant answers.

Whole-query triggers like ``aapl stock``, ``stock nvda``, ``aktie tsla`` or
``$NVDA`` render a quote hero card with price, day change and a one-month
sparkline, like Google's finance widget. Company names work too: keyword
forms (``taiwan semiconductor stock``, ``aktie novo nordisk``) resolve the
name through Yahoo's search endpoint, and bare queries containing an
ALL-CAPS token (``TSMC taiwan semiconductors``) resolve when the match is
unambiguous (symbol relates to the caps token; with extra words, at least
one must overlap the matched company name, so ``DTU exam dates`` stays
clean). Quotes come from Yahoo's public v8 chart endpoint (range=1y,
interval=1d, fetched in a pre_search background thread so the API call runs
concurrently with the engine fan-out) and are cached in-process for five
minutes per ticker; any API failure or unknown ticker silently yields no
answer, never a broken search. The year of daily closes is embedded in the
card as JSON and custom/cards.js turns it into an interactive range chart.

Rendered by the custom template ``answer/cael/stock.html`` (bind mounted,
like this file; see docker-compose.yml).
"""

from __future__ import annotations

import re
import threading
import time
import typing

import json

from urllib.parse import quote as url_quote

from flask_babel import format_decimal

from searx.network import get as http_get
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx.result_types.answer import BaseAnswer

from searx.plugins.cael._prefetch import prefetch, await_prefetch

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

TEMPLATE = "answer/cael/stock.html"

# Yahoo answers plain JSON on the chart endpoint, but only with a
# browser-like User-Agent; the default httpx/curl UAs get HTTP 429.
API_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search?q={q}&quotesCount=5&newsCount=0"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"
ANSWER_URL = "https://finance.yahoo.com/quote/{symbol}"
CACHE_TTL = 300  # seconds, per uppercased ticker
RESOLVE_TTL = 3600  # seconds, per normalized company-name query

# quoteTypes the name resolver accepts from Yahoo search
RESOLVE_TYPES = frozenset({"EQUITY", "ETF", "INDEX", "CRYPTOCURRENCY"})

# keyword slots around the ticker: "stock aapl" / "stocks aapl" / "aktie tsla"
LEADING_WORDS = frozenset({"stock", "stocks", "aktie"})
# "aapl stock" / "tsla aktie"
TRAILING_WORDS = frozenset({"stock", "aktie"})
# multiword trailing phrases: "taiwan semiconductor stock price"
TRAILING_PHRASES = ("stock price", "share price", "stock chart", "aktiekurs")

# words in the ticker slot that mean the query is not about a quote
# ("stock photos", "stock market", ...): silently no answer
STOPLIST = frozenset(
    "photos photo image images footage video market markets "
    "price prices exchange options split news".split()
)

# ticker shapes: AAPL, NOVO-B.CO, BRK-B, ^GSPC; must contain a letter
TICKER_RE = re.compile(r"^[A-Za-z0-9.^-]{1,12}$")
HAS_LETTER_RE = re.compile(r"[A-Za-z]")
# dollar form "$NVDA": $ then 1 to 10 chars starting with a letter
DOLLAR_RE = re.compile(r"^\$([A-Za-z][A-Za-z0-9.^-]{0,9})$")

# sparkline geometry: 120x36 viewBox with 2px padding on every side
SPARK_W = 120.0
SPARK_H = 36.0
SPARK_PAD = 2.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[dict, float]] = {}


class StockAnswer(BaseAnswer, kw_only=True):
    """Stock quote answer with a precomputed SVG sparkline."""

    template: str = TEMPLATE
    answer: str
    """Human-readable summary, e.g. ``AAPL (Apple Inc.) 213.55 USD, +1.24% past day``."""
    symbol: str
    name: str
    price: str
    currency: str
    change: str
    change_pct: str
    up: bool
    spark_path: str
    spark_end_x: str
    spark_end_y: str
    series: str
    """JSON {"t": [...], "v": [...]} of about one year of daily closes,
    consumed by custom/cards.js for the interactive range chart."""

    def __hash__(self):
        return hash(self.answer)


def _clean_ticker(candidate: str) -> str | None:
    if candidate.lower() in STOPLIST:
        return None
    if not TICKER_RE.match(candidate) or not HAS_LETTER_RE.search(candidate):
        return None
    return candidate.upper()


CAPS_TOKEN_RE = re.compile(r"^[A-Z]{2,6}$")
PURE_NUMBER_RE = re.compile(r"^\d{4,}$")


def _extract_trigger(query: str) -> tuple[str, str] | None:
    """Classify the query as a stock trigger.

    Returns ("ticker", SYMBOL), ("name", phrase) for explicit keyword forms,
    ("bare", phrase) for keywordless company-name forms, or None.
    """
    words = query.split()
    if not words or len(words) > 7:
        return None

    # "$NVDA"
    if len(words) == 1:
        m = DOLLAR_RE.match(words[0])
        if m:
            sym = _clean_ticker(m.group(1))
            return ("ticker", sym) if sym else None

    # explicit keyword forms
    rest: list[str] | None = None
    if words[0].lower() in LEADING_WORDS and len(words) >= 2:
        rest = words[1:]
    elif words[-1].lower() in TRAILING_WORDS and len(words) >= 2:
        rest = words[:-1]
    elif len(words) >= 3 and " ".join(w.lower() for w in words[-2:]) in TRAILING_PHRASES:
        rest = words[:-2]
    if rest is not None:
        if any(w.lower() in STOPLIST for w in rest):
            return None
        if any(PURE_NUMBER_RE.match(w) for w in rest):
            return None
        if len(rest) == 1:
            sym = _clean_ticker(rest[0])
            if sym:
                return ("ticker", sym)
        return ("name", " ".join(rest)) if 1 <= len(rest) <= 6 else None

    # bare form: needs an ALL-CAPS ticker-looking token ("TSMC taiwan semi");
    # a lone token needs 3+ letters so "IT" (the movie) is not Gartner
    if len(words) <= 5:
        caps = [w for w in words if CAPS_TOKEN_RE.match(w)]
        if caps and not any(w.lower() in STOPLIST for w in words):
            if len(words) == 1 and len(words[0]) < 3:
                return None
            if not any(PURE_NUMBER_RE.match(w) for w in words):
                return ("bare", query.strip())
    return None


def _resolve_cache_key(phrase: str) -> str:
    return re.sub(r"\s+", " ", phrase.strip().lower())


_resolve_lock = threading.Lock()
_resolve_cache: dict[str, tuple[tuple[str, str] | None, float]] = {}


def _resolve_name(phrase: str, hint: str | None = None) -> tuple[str, str] | None:
    """Resolve a company name to (symbol, longname) via Yahoo search.

    ``hint`` is an ALL-CAPS token from the query: candidates whose symbol
    base matches it outrank everything (Yahoo search for "TSMC" only lists
    foreign venues of the right company plus lookalike tickers, so venue
    shape alone picks the wrong company). Venue shape breaks ties: primary
    listings tend to have no digits (TSMC34) and no suffix (.SA).
    """
    key = (hint or "") + "|" + _resolve_cache_key(phrase)
    now = time.monotonic()
    with _resolve_lock:
        hit = _resolve_cache.get(key)
        if hit and now - hit[1] < RESOLVE_TTL:
            return hit[0]
    resolved: tuple[str, str] | None = None
    try:
        resp = http_get(
            SEARCH_URL.format(q=url_quote(phrase, safe="")),
            headers={"User-Agent": USER_AGENT},
            timeout=3.0,
        )
        if resp.status_code == 200:
            candidates: list[tuple[int, int, int, str, str]] = []
            for index, quote in enumerate(resp.json().get("quotes", [])):
                if quote.get("quoteType") not in RESOLVE_TYPES:
                    continue
                symbol = quote.get("symbol")
                if not symbol:
                    continue
                name = quote.get("longname") or quote.get("shortname") or symbol
                base = symbol.split(".")[0].upper()
                sym_score = 0
                if hint:
                    if base == hint:
                        sym_score = 8
                    elif base.startswith(hint) or hint.startswith(base):
                        sym_score = 4 - min(3, abs(len(base) - len(hint)))
                venue = 0
                if not re.search(r"\d", base):
                    venue += 3
                if "." not in symbol:
                    venue += 1
                candidates.append((-sym_score, -venue, index, symbol.upper(), name))
            if candidates:
                candidates.sort()
                resolved = (candidates[0][3], candidates[0][4])
    except Exception:  # pylint: disable=broad-except
        # unresolved is cached too, so junk queries do not re-hit the API
        resolved = None
    with _resolve_lock:
        _resolve_cache[key] = (resolved, now)
    return resolved


def _canonicalize(symbol: str, longname: str) -> tuple[str, str]:
    """Swap a secondary-venue match (TSMC.BA, BRL/ARS listings) for the
    primary one by re-searching the company's own long name; keep the
    original when the round trip fails or lands on a different company."""
    primary = _resolve_name(longname)
    if primary is None or primary[0] == symbol:
        return symbol, longname
    orig_words = {w.lower().strip(".,()") for w in longname.split() if len(w) >= 4}
    new_words = {w.lower().strip(".,()") for w in primary[1].split() if len(w) >= 4}
    if orig_words & new_words:
        return primary
    return symbol, longname


def _bare_match_ok(query: str, symbol: str, names: list[str]) -> bool:
    """Acceptance gate for keywordless queries.

    The caps token must relate to the matched symbol (TSMC vs TSM works via
    prefix), and when extra words are present at least one must overlap the
    matched company name (prefix match, 4+ chars), so unrelated queries that
    happen to contain an acronym stay clean.
    """
    words = query.split()
    caps = [w for w in words if CAPS_TOKEN_RE.match(w)]
    base = symbol.split(".")[0].replace("-", "").upper()
    symbol_ok = False
    for token in caps:
        t = token.replace("-", "").upper()
        if t == base or t.startswith(base) or base.startswith(t):
            symbol_ok = True
            break
    if not symbol_ok:
        return False
    others = [w.lower() for w in words if not CAPS_TOKEN_RE.match(w)]
    if not others:
        return True
    name_words = [w.lower().strip(".,()") for n in names for w in n.split()]
    for token in others:
        if len(token) < 4:
            continue
        for nw in name_words:
            if nw.startswith(token[:4]) or token.startswith(nw[:4]):
                return True
    return False


def _fetch_quote(symbol: str) -> dict | None:
    """Fetch (or serve from cache) the numeric quote data for a ticker."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(symbol)
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0]
    try:
        resp = http_get(
            API_URL.format(symbol=url_quote(symbol, safe="")),
            headers={"User-Agent": USER_AGENT},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return None
        chart = resp.json()["chart"]
        if chart.get("error") or not chart.get("result"):
            return None
        result = chart["result"][0]
        meta = result["meta"]
        price = float(meta["regularMarketPrice"])
        stamps = result.get("timestamp") or []
        raw_closes = result["indicators"]["quote"][0]["close"]
        pairs = [
            (int(t), float(c))
            for t, c in zip(stamps, raw_closes)
            if t is not None and c is not None
        ]
        closes = [c for _, c in pairs]
        if not closes:
            return None
        # meta.previousClose is absent on range=1mo requests and
        # meta.chartPreviousClose is the close before the whole window (a
        # month back), so prefer the second-to-last daily close for a true
        # "past day" change
        prev = meta.get("previousClose")
        if prev is None and len(closes) >= 2:
            prev = closes[-2]
        if prev is None:
            prev = meta.get("chartPreviousClose")
        if not prev:  # None or 0: change is uncomputable
            return None
        data = {
            "price": price,
            "prev": float(prev),
            "currency": meta.get("currency") or "",
            "name": meta.get("shortName") or meta.get("longName") or symbol,
            "closes": closes,
            "pairs": pairs,
        }
    except Exception:  # pylint: disable=broad-except
        # any failure means "no answer", never a broken search
        return None
    with _cache_lock:
        _cache[symbol] = (data, now)
    return data


def _spark(closes: list[float]) -> tuple[str, str, str]:
    """Normalize the close series into an SVG path in the sparkline viewBox.

    Returns (path, end_x, end_y) with all coordinates rounded to 1 decimal.
    Flat series (or a single point) become a horizontal center line.
    """
    if len(closes) == 1:
        closes = closes * 2
    n = len(closes)
    lo, hi = min(closes), max(closes)
    step = (SPARK_W - 2 * SPARK_PAD) / (n - 1)
    if hi - lo < 1e-9:
        ys = [SPARK_H / 2.0] * n
    else:
        scale = (SPARK_H - 2 * SPARK_PAD) / (hi - lo)
        ys = [SPARK_PAD + (hi - c) * scale for c in closes]
    points = [(SPARK_PAD + i * step, ys[i]) for i in range(n)]
    path = "M {:.1f},{:.1f}".format(*points[0])
    path += "".join(" L {:.1f},{:.1f}".format(x, y) for x, y in points[1:])
    end_x, end_y = points[-1]
    return path, "{:.1f}".format(end_x), "{:.1f}".format(end_y)


def _fmt(value: float, frac: int = 2) -> str:
    try:
        return format_decimal(value, format="#,##0." + "0" * frac)
    except Exception:  # pylint: disable=broad-except
        return f"{value:,.{frac}f}"


def _signed(value: float, suffix: str = "") -> str:
    sign = "-" if value < 0 else "+"
    return sign + _fmt(abs(value)) + suffix


def _full_lookup(kind: str, payload: str) -> tuple[str, dict] | None:
    """Trigger -> (symbol, quote data), every step backed by its own cache."""
    if kind == "ticker":
        symbol = payload
    else:
        if kind == "bare":
            caps = [w for w in payload.split() if CAPS_TOKEN_RE.match(w)]
            resolved = _resolve_name(" ".join(caps), hint=caps[0])
        else:
            resolved = _resolve_name(payload)
        if resolved is None:
            return None
        symbol, longname = resolved
        if kind == "bare":
            if not _bare_match_ok(payload, symbol, [longname]):
                return None
            symbol, longname = _canonicalize(symbol, longname)
    data = _fetch_quote(symbol)
    if data is None:
        return None
    return symbol, data


@typing.final
class SXNGPlugin(Plugin):
    """Stock quote card for "<ticker> stock" / "$TICKER" queries."""

    id = "stock_answer"
    keywords = []  # triggers are not strictly first-word, gate in post_search

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Stock quotes",
            description=(
                "Shows a stock quote card with a one-month sparkline for "
                "queries like 'aapl stock', 'taiwan semiconductor stock', "
                "'TSMC' or '$NVDA'."
            ),
            examples=["aapl stock", "taiwan semiconductor stock", "$NVDA"],
            preference_section="query",
        )

    def pre_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> bool:
        # kick the whole lookup off in a background thread so the API round
        # trips overlap the engine fan-out instead of following it
        if search.search_query.pageno == 1:
            trigger = _extract_trigger(search.search_query.query)
            if trigger is not None:
                prefetch("stock:" + search.search_query.query, lambda: _full_lookup(*trigger))
        return True

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        trigger = _extract_trigger(search.search_query.query)
        if trigger is None:
            return results

        found = await_prefetch("stock:" + search.search_query.query, lambda: _full_lookup(*trigger))
        if found is None:
            return results
        symbol, data = found

        pairs = data.get("pairs") or [(0, c) for c in data["closes"]]
        month_ago = time.time() - 30 * 86400
        month = [c for t, c in pairs if t >= month_ago] or data["closes"]
        base = month[0] if month[0] else data["prev"]
        change = data["price"] - base
        change_pct = change / base * 100.0 if base else 0.0
        price_str = _fmt(data["price"])
        change_pct_str = _signed(change_pct, "%")
        spark_path, end_x, end_y = _spark(month)
        series = json.dumps(
            {"t": [t for t, _ in pairs], "v": [round(c, 4) for _, c in pairs]},
            separators=(",", ":"),
        )

        results.add(
            StockAnswer(
                answer="{sym} ({name}) {price} {cur}, {pct} past month".format(
                    sym=symbol,
                    name=data["name"],
                    price=price_str,
                    cur=data["currency"],
                    pct=change_pct_str,
                ),
                url=ANSWER_URL.format(symbol=url_quote(symbol, safe="")),
                symbol=symbol,
                name=data["name"],
                price=price_str,
                currency=data["currency"],
                change=_signed(change),
                change_pct=change_pct_str,
                up=change >= 0,
                spark_path=spark_path,
                spark_end_x=end_x,
                spark_end_y=end_y,
                series=series,
            )
        )
        return results
