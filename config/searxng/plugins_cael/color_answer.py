# SPDX-License-Identifier: AGPL-3.0-or-later
"""Color swatch instant answers.

``#c9bfff``, ``rgb(201, 191, 255)``, ``hsl(252, 100%, 87%)`` or the keyword
forms ``color c9bfff`` / ``colour #f9b`` / Danish ``farve f9b1dc`` render a
swatch card with the normalized hex, RGB and HSL notations, the WCAG relative
luminance, and a complement chip (hue rotated 180 degrees in HSL). A bare
six-digit hex WITHOUT ``#`` and without a keyword never triggers: it could be
a git hash.

Everything is computed here (hex expansion, RGB/HSL conversion, luminance,
readable text color by contrast ratio, complement); the template only prints
plugin-built strings, so its inline ``style`` attributes never see raw user
input. Purely local, no network access.

Bind mounted into the image next to the stock plugins (see docker-compose.yml)
and registered in settings.yml as ``searx.plugins.cael.color_answer.SXNGPlugin``.
"""

from __future__ import annotations

import colorsys
import re
import typing

from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx.result_types.answer import BaseAnswer

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

# First-word keywords that allow the hex value to omit its "#". Matched
# case-insensitively by hand in _parse_query (the PluginStorage keyword gate
# is not used: most triggers of this plugin are not keyword-shaped).
KEYWORDS = ("color", "colour", "farve")

MAX_QUERY_LEN = 48  # longest sensible trigger is ~35 chars

ANSWER_URL = "https://www.colorhexa.com/{rgbhex}"

# #rgb / #rrggbb / #rrggbbaa; group 1 tells whether the "#" was present
# (required unless a keyword introduced the value)
HEX_RE = re.compile(r"^(#?)([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGB_RE = re.compile(r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$", re.IGNORECASE)
RGBA_RE = re.compile(
    r"^rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*([01](?:\.\d+)?|0?\.\d+)\s*\)$",
    re.IGNORECASE,
)
HSL_RE = re.compile(
    r"^hsl\(\s*(\d{1,3}(?:\.\d+)?)\s*,\s*(\d{1,3}(?:\.\d+)?)%\s*,\s*(\d{1,3}(?:\.\d+)?)%\s*\)$",
    re.IGNORECASE,
)


class ColorAnswer(BaseAnswer, kw_only=True):
    """Swatch answer: all fields are display-ready strings."""

    template: str = "answer/cael/color.html"
    answer: str
    hex: str
    rgb: str
    hsl: str
    luminance: str
    text_on: str
    complement_hex: str
    complement_text_on: str
    complement_search: str
    alpha: str = ""

    def __hash__(self):
        return hash(self.answer)


def _expand_hex(digits: str) -> tuple[int, int, int, float | None]:
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    r, g, b = int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else None
    return r, g, b, alpha


def _parse_value(value: str, allow_bare_hex: bool) -> tuple[int, int, int, float | None] | None:
    m = HEX_RE.match(value)
    if m:
        if m.group(1) or allow_bare_hex:
            return _expand_hex(m.group(2).lower())
        return None  # bare hex without "#": likely a git hash, never trigger
    m = RGB_RE.match(value)
    if m:
        r, g, b = (int(x) for x in m.groups())
        if max(r, g, b) <= 255:
            return r, g, b, None
        return None
    m = RGBA_RE.match(value)
    if m:
        r, g, b = (int(x) for x in m.groups()[:3])
        alpha = float(m.group(4))
        if max(r, g, b) <= 255 and alpha <= 1.0:
            return r, g, b, alpha
        return None
    m = HSL_RE.match(value)
    if m:
        hue, sat, lig = (float(x) for x in m.groups())
        if hue <= 360 and sat <= 100 and lig <= 100:
            r, g, b = colorsys.hls_to_rgb((hue % 360) / 360, lig / 100, sat / 100)
            return round(r * 255), round(g * 255), round(b * 255), None
        return None
    return None


def _parse_query(query: str) -> tuple[int, int, int, float | None] | None:
    q = query.strip()
    if not q or len(q) > MAX_QUERY_LEN:
        return None
    parts = q.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in KEYWORDS:
        return _parse_value(parts[1].strip(), allow_bare_hex=True)
    return _parse_value(q, allow_bare_hex=False)


def _rel_luminance(r: int, g: int, b: int) -> float:
    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _text_on(lum: float) -> str:
    # WCAG contrast ratio against black vs against white; pick the higher
    contrast_black = (lum + 0.05) / 0.05
    contrast_white = 1.05 / (lum + 0.05)
    return "#000000" if contrast_black >= contrast_white else "#ffffff"


def _fmt_alpha(alpha: float) -> str:
    s = f"{alpha:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _build_answer(r: int, g: int, b: int, alpha: float | None) -> ColorAnswer:
    rgbhex = f"{r:02x}{g:02x}{b:02x}"
    if alpha is None:
        alpha_str = ""
        hex_str = "#" + rgbhex
        rgb_str = f"rgb({r}, {g}, {b})"
    else:
        alpha_str = _fmt_alpha(alpha)
        hex_str = f"#{rgbhex}{round(alpha * 255):02x}"
        rgb_str = f"rgba({r}, {g}, {b}, {alpha_str})"

    # colorsys works in HLS order with 0..1 components
    hue, lig, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    hsl_str = f"hsl({round(hue * 360) % 360}, {round(sat * 100)}%, {round(lig * 100)}%)"

    lum = _rel_luminance(r, g, b)

    cr, cg, cb = colorsys.hls_to_rgb((hue + 0.5) % 1.0, lig, sat)
    comp = (round(cr * 255), round(cg * 255), round(cb * 255))
    complement_hex = "#{:02x}{:02x}{:02x}".format(*comp)

    answer_str = f"{hex_str} = {rgb_str}, {hsl_str}"
    if alpha is not None:
        answer_str += f", alpha {alpha_str}"

    return ColorAnswer(
        answer=answer_str,
        hex=hex_str,
        rgb=rgb_str,
        hsl=hsl_str,
        luminance=f"{lum:.2f}",
        text_on=_text_on(lum),
        complement_hex=complement_hex,
        complement_text_on=_text_on(_rel_luminance(*comp)),
        # relative URL: a click on the complement chip searches for it
        complement_search="search?q=%23" + complement_hex.lstrip("#"),
        alpha=alpha_str,
        url=ANSWER_URL.format(rgbhex=rgbhex),
    )


@typing.final
class SXNGPlugin(Plugin):
    """Color swatch card for hex/rgb/hsl color queries."""

    id = "color_answer"

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Color swatch",
            description=(
                "Shows a swatch card with RGB/HSL notations, WCAG luminance "
                "and the complement for queries like '#c9bfff', "
                "'rgb(201, 191, 255)' or 'color f9b1dc'."
            ),
            examples=["#c9bfff", "rgb(201, 191, 255)", "hsl(252, 100%, 87%)", "color f9b1dc"],
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        parsed = _parse_query(search.search_query.query)
        if parsed is None:
            return results

        results.add(_build_answer(*parsed))
        return results
