# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style currency instant answers.

Turns bare queries like ``400 usd`` (also ``$400``, ``400 kr``, ``400 dollars``)
into a currency-conversion answer without needing an explicit target, the way
Google shows a converter card. Explicit forms (``400 usd in eur``,
``50 eur to dkk``, Danish ``400 usd i dkk``) are handled too.

Self-contained on purpose: it reads ``searx/data/currencies.json`` directly
instead of going through ``searx.data.CURRENCIES``, whose SQLite-backed cache
expires its rows after 7 days of uptime while keeping the "currencies loaded"
flag, which silently kills the built-in ``currency_convert`` engine. Rates
come from DuckDuckGo's spice endpoint (the same source the built-in engine
uses) and are cached in-process for 10 minutes.

Bind mounted into the image next to the stock plugins (see docker-compose.yml)
and registered in settings.yml as ``searx.plugins.currency_answer.SXNGPlugin``.
"""

from __future__ import annotations

import json
import re
import threading
import time
import typing

from flask_babel import format_decimal

from searx.data.currencies import CurrenciesDB
from searx.network import get as http_get
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

# Where a bare "400 usd" converts to; when the query already is the default
# target ("400 dkk"), convert to the fallback instead.
DEFAULT_TARGET = "DKK"
FALLBACK_TARGET = "EUR"

RATES_URL = "https://duckduckgo.com/js/spice/currency/1/{frm}/{to}"
ANSWER_URL = "https://www.google.com/finance/quote/{frm}-{to}"
RATE_TTL = 600  # seconds

MAX_QUERY_LEN = 60

# The upstream names table maps ambiguous words to surprising codes
# ("dollars" -> AUD, "$" -> ARS, "kr" -> last match wins). Pin the common
# readings for this household.
OVERRIDES = {
    "$": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "£": "GBP",  # pound sign
    "pound": "GBP",
    "pounds": "GBP",
    "¥": "JPY",  # yen sign
    "yen": "JPY",
    "€": "EUR",  # euro sign
    "kr": "DKK",
    "krone": "DKK",
    "kroner": "DKK",
}

# ISO 4217 codes that are also ordinary English/tech words ("top 10 movies",
# "php 8", "amd 7900", "crc 32"). These only count as a currency when the
# intent is unambiguous: written in UPPERCASE after the amount ("400 TRY"),
# or used in an explicit conversion ("100 try in dkk"). Everything else
# (full names like "turkish lira") is unaffected.
AMBIGUOUS_ISO = frozenset(
    "ALL AMD BAM BOB BSD COP CRC CUP CVE ERN GEL MAD MOP PEN PHP RON SOS STD TOP TRY XXX".split()
)

# "<amount + currency> in|to|i <currency>" (i = Danish "in")
CONVERT_RE = re.compile(r"^\s*(.+?)\s+(?:in|to|i)\s+(.+?)\s*$", re.IGNORECASE)
# "<currency?> <amount> <currency?>": "$400", "400 usd", "400 danish krone"
AMOUNT_RE = re.compile(r"^\s*(\D{1,30}?)?\s*(\d[\d.,\s]*)\s*(\D{1,30}?)?\s*$")

_db_lock = threading.Lock()
_names: dict[str, str] = {}
_iso: dict[str, dict[str, str]] = {}

_rates_lock = threading.Lock()
_rates: dict[tuple[str, str], tuple[float, float]] = {}


def _load_db() -> None:
    global _iso
    with _db_lock:
        if _names:
            return
        with open(CurrenciesDB.json_file, encoding="utf-8") as f:
            data = json.load(f)
        _iso = data["iso4217"]
        for name, code in data["names"].items():
            # upstream convention: on ambiguous names the last entry wins
            _names[name] = code[-1] if isinstance(code, list) else code
        _names.update(OVERRIDES)


def _to_iso4217(token: str | None, ambiguous_ok: bool = False) -> str | None:
    if not token:
        return None
    token = re.sub(r"\s+", " ", token.strip().lower().replace("-", " "))
    token = token.rstrip(".")
    if not token:
        return None
    _load_db()
    code = token.upper()
    if code in AMBIGUOUS_ISO and not ambiguous_ok:
        return None
    if code in _iso:
        return code
    return _names.get(token)


def _iso_name(code: str) -> str:
    return _iso.get(code, {}).get("en", code)


def _parse_amount(s: str) -> float | None:
    s = re.sub(r"\s", "", s)
    if not s:
        return None
    if "." in s and "," in s:
        # both separators present: the rightmost one is the decimal mark
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        other = "," if dec == "." else "."
        s = s.replace(other, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        if s.count(sep) == 1 and len(tail) != 3:
            s = head + "." + tail  # decimal mark: "400,5" / "400.50"
        else:
            s = s.replace(sep, "")  # thousands: "1,000" / "1.000.000"
    try:
        return float(s)
    except ValueError:
        return None


def _parse_amount_currency(text: str, explicit: bool = False) -> tuple[float, str] | None:
    m = AMOUNT_RE.match(text)
    if not m:
        return None
    prefix, amount_str, suffix = m.groups()
    amount = _parse_amount(amount_str)
    if amount is None:
        return None
    # ambiguous word-like codes: OK in explicit conversions, otherwise only
    # as an UPPERCASE suffix ("400 TRY" yes, "top 10 movies" no)
    suffix_upper = bool(suffix) and suffix.strip().isupper()
    cur = _to_iso4217(suffix, ambiguous_ok=explicit or suffix_upper) or _to_iso4217(prefix, ambiguous_ok=explicit)
    if cur is None:
        return None
    return amount, cur


def _parse_query(query: str) -> tuple[float, str, str] | None:
    if len(query) > MAX_QUERY_LEN:
        return None
    m = CONVERT_RE.match(query)
    if m:
        base = _parse_amount_currency(m.group(1), explicit=True)
        to = _to_iso4217(m.group(2), ambiguous_ok=True)
        if base and to:
            return base[0], base[1], to
    base = _parse_amount_currency(query)
    if base:
        amount, frm = base
        to = DEFAULT_TARGET if frm != DEFAULT_TARGET else FALLBACK_TARGET
        return amount, frm, to
    return None


def _get_rate(frm: str, to: str) -> float | None:
    now = time.monotonic()
    with _rates_lock:
        hit = _rates.get((frm, to))
        if hit and now - hit[1] < RATE_TTL:
            return hit[0]
    try:
        resp = http_get(RATES_URL.format(frm=frm, to=to), timeout=3.0)
        text = resp.text
        # response is JSONP: ddg_spice_currency({...});
        payload = json.loads(text[text.find("(") + 1 : text.rfind(")")])
        rate = float(payload["to"][0]["mid"])
    except Exception:  # pylint: disable=broad-except
        # any failure means "no answer", never a broken search
        return None
    with _rates_lock:
        _rates[(frm, to)] = (rate, now)
    return rate


def _fmt(value: float, frac: int = 2) -> str:
    try:
        return format_decimal(value, format="#,##0." + "0" * frac)
    except Exception:  # pylint: disable=broad-except
        return f"{value:,.{frac}f}"


@typing.final
class SXNGPlugin(Plugin):
    """Currency conversion answers for amount + currency queries."""

    id = "currency_answer"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Currency converter",
            description=(
                "Shows a conversion answer for queries like '400 usd' "
                "(default target " + DEFAULT_TARGET + ") or '400 usd in eur'."
            ),
            examples=["400 usd", "400 usd in eur"],
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        parsed = _parse_query(search.search_query.query)
        if parsed is None:
            return results
        amount, frm, to = parsed

        rate = _get_rate(frm, to)
        if rate is None:
            return results

        answer = "{amt} {frm_name} = {conv} {to_name} (1 {frm} = {rate} {to})".format(
            amt=_fmt(amount),
            frm_name=_iso_name(frm),
            conv=_fmt(amount * rate),
            to_name=_iso_name(to),
            frm=frm,
            rate=_fmt(rate, 4),
            to=to,
        )
        results.add(
            results.types.Answer(
                answer=answer,
                url=ANSWER_URL.format(frm=frm, to=to),
                # custom hero-card template (bind mounted, see docker-compose.yml);
                # it splits the answer string on the exact " = " / " (" separators
                # produced above, so keep the format string and template in sync
                template="answer/cael/currency.html",
            )
        )
        return results
