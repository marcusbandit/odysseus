# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style weather instant answers.

``weather copenhagen``, ``vejret lyngby`` or a bare ``weather`` (defaults to
DEFAULT_LOCATION) render a weather card in the answers area, like Google's
weather widget. Geocoding and units come from SearXNG's own ``searx.weather``
helpers; conditions and the forecast API are shared with the built-in
``open_meteo`` engine (which stays available on the weather tab / ``!om``,
this plugin brings the card to the general tab where people actually search).

Rendered by the custom template ``answer/cael_weather.html`` (bind mounted,
like this file; see docker-compose.yml).
"""

from __future__ import annotations

import threading
import time
import typing

from datetime import datetime
from urllib.parse import urlencode

from searx import weather
from searx.engines.open_meteo import WMO_TO_CONDITION, api_url, data_of_interest
from searx.network import get as http_get
from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults, WeatherAnswer

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

DEFAULT_LOCATION = "Copenhagen"
TEMPLATE = "answer/cael_weather.html"
FORECAST_WINDOW_H = 24  # show chips for the next N hours
FORECAST_STEP_H = 3  # one chip every N hours
CACHE_TTL = 600  # seconds

# The PluginStorage keyword gate compares the raw first word of the query
# against this list (exact, case-sensitive), so capitalized variants must be
# listed too.
KEYWORDS = [
    "weather",
    "Weather",
    "forecast",
    "Forecast",
    "vejr",
    "Vejr",
    "vejret",
    "Vejret",
    "vejrudsigt",
    "Vejrudsigt",
]

_cache_lock = threading.Lock()
_cache: dict[tuple[float, float], tuple[dict, float]] = {}


def _fetch_forecast(location: weather.GeoLocation) -> dict | None:
    key = (location.latitude, location.longitude)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0]
    args = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "timeformat": "unixtime",
        "timezone": "auto",
        "format": "json",
        "current": ",".join(data_of_interest),
        "forecast_days": 2,
        "hourly": ",".join(data_of_interest),
    }
    try:
        resp = http_get(f"{api_url}/v1/forecast?{urlencode(args)}", timeout=3.0)
        data = resp.json()
        data["current"]["time"]  # sanity: shape check, KeyError on API change
    except Exception:  # pylint: disable=broad-except
        # any failure means "no answer", never a broken search
        return None
    with _cache_lock:
        _cache[key] = (data, now)
    return data


def _mk_item(location: weather.GeoLocation, d: dict) -> WeatherAnswer.Item:
    condition = WMO_TO_CONDITION.get(d["weather_code"], "partly cloudy")
    return WeatherAnswer.Item(
        location=location,
        temperature=weather.Temperature(val=d["temperature_2m"], unit="°C"),
        condition=condition,
        summary=condition.capitalize(),
        feels_like=weather.Temperature(val=d["apparent_temperature"], unit="°C"),
        wind_from=weather.Compass(d["wind_direction_10m"]),
        wind_speed=weather.WindSpeed(val=d["wind_speed_10m"], unit="km/h"),
        pressure=weather.Pressure(val=d["pressure_msl"], unit="hPa"),
        humidity=weather.RelativeHumidity(d["relative_humidity_2m"]),
        cloud_cover=d["cloud_cover"],
    )


@typing.final
class SXNGPlugin(Plugin):
    """Weather card for "weather <place>" queries on any tab."""

    id = "weather_answer"
    keywords = KEYWORDS

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Weather",
            description="Shows a weather card for queries like 'weather copenhagen'.",
            examples=["weather copenhagen", "vejret lyngby"],
            keywords=self.keywords,
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        sq = search.search_query
        if sq.pageno > 1:
            return results

        words = sq.query.split()
        if not words or words[0] not in self.keywords:
            return results
        place = " ".join(words[1:]).strip() or DEFAULT_LOCATION

        try:
            location = weather.GeoLocation.by_query(place)
        except ValueError:
            return results

        data = _fetch_forecast(location)
        if data is None:
            return results

        try:
            answer = WeatherAnswer(
                current=_mk_item(location, data["current"]),
                service="Open-Meteo",
                url=f"https://www.yr.no/en/forecast/daily-table/{location.latitude:.4f},{location.longitude:.4f}",
                template=TEMPLATE,
            )
            now_unix = data["current"]["time"]
            for index, t in enumerate(data["hourly"]["time"]):
                if t <= now_unix or t - now_unix > FORECAST_WINDOW_H * 3600:
                    continue
                local_dt = datetime.fromtimestamp(t, tz=location.zoneinfo)
                if local_dt.hour % FORECAST_STEP_H != 0:
                    continue
                hourly = {key: data["hourly"][key][index] for key in data_of_interest}
                item = _mk_item(location, hourly)
                item.datetime = weather.DateTime(local_dt)
                answer.forecasts.append(item)
        except (KeyError, IndexError, ValueError, TypeError):
            return results

        results.add(answer)
        return results
