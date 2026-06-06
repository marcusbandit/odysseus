# SPDX-License-Identifier: AGPL-3.0-or-later
"""Custom *offline* engine: ``!gm`` -> real Google Maps.

Returns a single result whose URL points at the real Google Maps site
(``https://maps.google.com/maps?q=<query>``). No HTTP request is made: the
URL is built directly from the query, so the user stays on the SearXNG
results page and clicks through to Google Maps.

Registered in ``config/searxng/settings.yml``::

  - name: google maps
    engine: google_maps
    shortcut: gm
    categories: [map]
    disabled: true

The file is bind-mounted into the container at
``/usr/local/searxng/searx/engines/google_maps.py`` (see docker-compose.yml).
"""

from urllib.parse import quote_plus

from searx.result_types import EngineResults

# Offline engine: SearXNG calls search(query, params) directly and never
# performs a network request on our behalf.
engine_type = "offline"
categories = ["map"]
# Bang-only by default. Flip to ``disabled: false`` in settings.yml to also
# surface the "Open in Google Maps" link on the normal Map tab.
disabled = True

about = {
    "website": "https://maps.google.com/",
    "wikidata_id": "Q12013",
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": False,
    "results": "URL",
}


def search(query, params):  # pylint: disable=unused-argument
    """Build a single Google Maps link from the query."""
    res = EngineResults()
    query = (query or "").strip()
    if not query:
        return res

    url = "https://maps.google.com/maps?q=" + quote_plus(query)
    res.add(
        res.types.MainResult(
            url=url,
            title="Open in Google Maps: %s" % query,
            content="Open this search on the real Google Maps (maps.google.com).",
        )
    )
    return res
