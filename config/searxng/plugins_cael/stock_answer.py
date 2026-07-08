# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style stock quote instant answers.

Whole-query triggers like ``aapl stock``, ``stock nvda``, ``aktie tsla`` or
``$NVDA`` render a quote hero card with price, day change and a one-month
sparkline, like Google's finance widget. Quotes come from Yahoo's public v8
chart endpoint (range=1mo, interval=1d) and are cached in-process for five
minutes per ticker; any API failure or unknown ticker silently yields no
answer, never a broken search.

Rendered by the custom template ``answer/cael/stock.html`` (bind mounted,
like this file; see docker-compose.yml).
"""

from __future__ import annotations

import re
import threading
import time
import typing

from urllib.parse import quote as url_quote

from flask_babel import format_decimal

from searx.network import get as http_get
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx.result_types.answer import BaseAnswer

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

TEMPLATE = "answer/cael/stock.html"

# Yahoo answers plain JSON on the chart endpoint, but only with a
# browser-like User-Agent; the default httpx/curl UAs get HTTP 429.
API_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"
ANSWER_URL = "https://finance.yahoo.com/quote/{symbol}"
CACHE_TTL = 300  # seconds, per uppercased ticker

# keyword slots around the ticker: "stock aapl" / "stocks aapl" / "aktie tsla"
LEADING_WORDS = frozenset({"stock", "stocks", "aktie"})
# "aapl stock" / "tsla aktie"
TRAILING_WORDS = frozenset({"stock", "aktie"})

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

    def __hash__(self):
        return hash(self.answer)


def _extract_symbol(query: str) -> str | None:
    """Return the uppercased ticker if the whole query is a stock trigger."""
    words = query.split()
    candidate = None
    if len(words) == 1:
        m = DOLLAR_RE.match(words[0])
        if m:
            candidate = m.group(1)
    elif len(words) == 2:
        if words[0].lower() in LEADING_WORDS:
            candidate = words[1]
        elif words[1].lower() in TRAILING_WORDS:
            candidate = words[0]
    if candidate is None:
        return None
    if candidate.lower() in STOPLIST:
        return None
    if not TICKER_RE.match(candidate) or not HAS_LETTER_RE.search(candidate):
        return None
    return candidate.upper()


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
        closes = [float(c) for c in result["indicators"]["quote"][0]["close"] if c is not None]
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
                "queries like 'aapl stock', 'stock nvda' or '$NVDA'."
            ),
            examples=["aapl stock", "stock nvda", "$NVDA"],
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        symbol = _extract_symbol(search.search_query.query)
        if symbol is None:
            return results

        data = _fetch_quote(symbol)
        if data is None:
            return results

        change = data["price"] - data["prev"]
        change_pct = change / data["prev"] * 100.0
        price_str = _fmt(data["price"])
        change_pct_str = _signed(change_pct, "%")
        spark_path, end_x, end_y = _spark(data["closes"])

        results.add(
            StockAnswer(
                answer="{sym} ({name}) {price} {cur}, {pct} past day".format(
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
            )
        )
        return results
