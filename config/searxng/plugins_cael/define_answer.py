# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style dictionary instant answers.

``define serendipity``, ``definition of recursion`` or ``meaning of ephemeral``
render a dictionary card in the answers area, like Google's dictionary widget.
Definitions come from the free dictionaryapi.dev endpoint (English only) and
are cached in-process for an hour.

Rendered by the custom template ``answer/cael/define.html`` (bind mounted,
like this file; see docker-compose.yml).
"""

from __future__ import annotations

import re
import threading
import time
import typing

from urllib.parse import quote

import msgspec

from searx.network import get as http_get
from searx.plugins import Plugin, PluginInfo
from searx.plugins.cael._prefetch import await_prefetch, prefetch
from searx.result_types import EngineResults
from searx.result_types.answer import BaseAnswer

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

API_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{term}"
FALLBACK_URL = "https://en.wiktionary.org/wiki/{term}"
TEMPLATE = "answer/cael/define.html"
CACHE_TTL = 3600  # seconds

MAX_TERM_LEN = 40
MAX_TERM_WORDS = 3
MAX_MEANINGS = 3
MAX_DEFINITIONS = 3
MAX_SYNONYMS = 6

# The PluginStorage keyword gate compares the raw first word of the query
# against this list (exact, case-sensitive), so capitalized variants must be
# listed too.
KEYWORDS = [
    "define",
    "Define",
    "definition",
    "Definition",
    "meaning",
    "Meaning",
]

TRIGGERS = frozenset(k.lower() for k in KEYWORDS)

# 1-3 words of letters, hyphens and apostrophes, separated by single spaces
TERM_RE = re.compile(
    r"[A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*){0,%d}" % (MAX_TERM_WORDS - 1)
)

_cache_lock = threading.Lock()
# term (lowercased) -> (entries or None for a definitive not-found, monotonic ts)
_cache: dict[str, tuple[list | None, float]] = {}


class DefineMeaning(msgspec.Struct, kw_only=True):
    """One part-of-speech block of the dictionary card."""

    part_of_speech: str
    definitions: list[str] = []
    example: str = ""
    synonyms: list[str] = []


class DefineAnswer(BaseAnswer, kw_only=True):
    """Dictionary answer: word, phonetic and up to MAX_MEANINGS meanings."""

    template: str = TEMPLATE
    answer: str
    word: str
    phonetic: str = ""
    meanings: list[DefineMeaning] = []

    def __hash__(self):
        # content hash so AnswerSet dedups identical cards
        return hash(self.answer)


def _parse_query(query: str) -> str | None:
    """Extract the lookup term, or None when the query is not a lookup."""
    words = query.split()
    if not words or words[0].lower() not in TRIGGERS:
        return None
    rest = words[1:]
    if rest and rest[0].lower() in ("of", "for"):
        rest = rest[1:]
    if not rest or len(rest) > MAX_TERM_WORDS:
        return None
    term = " ".join(rest).replace("’", "'").rstrip("?.!,")
    if not term or len(term) > MAX_TERM_LEN:
        return None
    if not TERM_RE.fullmatch(term):
        return None
    return term


def _fetch_entries(term: str) -> list | None:
    key = term.lower()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[1] < CACHE_TTL:
            return hit[0]
    try:
        resp = http_get(API_URL.format(term=quote(term)), timeout=3.0)
    except Exception:  # pylint: disable=broad-except
        # network trouble: no answer, never a broken search; not cached so a
        # transient failure does not stick for an hour
        return None
    entries: list | None = None
    try:
        if resp.status_code == 200:
            data = resp.json()
            # success is a JSON array of entries; not-found is a JSON object
            if isinstance(data, list) and data:
                entries = data
    except Exception:  # pylint: disable=broad-except
        return None
    # cache definitive answers, including not-found (entries is None)
    with _cache_lock:
        _cache[key] = (entries, now)
    return entries


def _clean_str(value: typing.Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mk_meaning(raw: dict) -> DefineMeaning | None:
    pos = _clean_str(raw.get("partOfSpeech"))
    raw_defs = raw.get("definitions")
    if not pos or not isinstance(raw_defs, list):
        return None

    definitions: list[str] = []
    example = ""
    synonyms: list[str] = []
    seen = set()

    def _add_synonyms(values: typing.Any) -> None:
        if not isinstance(values, list):
            return
        for v in values:
            text = _clean_str(v)
            if text and text.lower() not in seen and len(synonyms) < MAX_SYNONYMS:
                seen.add(text.lower())
                synonyms.append(text)

    _add_synonyms(raw.get("synonyms"))
    for d in raw_defs:
        if not isinstance(d, dict):
            continue
        text = _clean_str(d.get("definition"))
        if text and len(definitions) < MAX_DEFINITIONS:
            definitions.append(text)
        if not example:
            # `example` is optional per definition; take the first one found
            example = _clean_str(d.get("example"))
        _add_synonyms(d.get("synonyms"))

    if not definitions:
        return None
    return DefineMeaning(
        part_of_speech=pos,
        definitions=definitions,
        example=example,
        synonyms=synonyms,
    )


def _build_answer(term: str, entries: list) -> DefineAnswer | None:
    first = entries[0]
    if not isinstance(first, dict):
        return None
    word = _clean_str(first.get("word")) or term

    phonetic = _clean_str(first.get("phonetic"))
    if not phonetic:
        phonetics = first.get("phonetics")
        if isinstance(phonetics, list):
            for p in phonetics:
                if isinstance(p, dict) and _clean_str(p.get("text")):
                    phonetic = _clean_str(p.get("text"))
                    break

    meanings: list[DefineMeaning] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_meanings = entry.get("meanings")
        if not isinstance(raw_meanings, list):
            continue
        for raw in raw_meanings:
            if len(meanings) >= MAX_MEANINGS:
                break
            if isinstance(raw, dict):
                meaning = _mk_meaning(raw)
                if meaning is not None:
                    meanings.append(meaning)
        if len(meanings) >= MAX_MEANINGS:
            break
    if not meanings:
        return None

    url = FALLBACK_URL.format(term=quote(term))
    source_urls = first.get("sourceUrls")
    if isinstance(source_urls, list) and source_urls and _clean_str(source_urls[0]):
        url = _clean_str(source_urls[0])

    return DefineAnswer(
        answer=f"{word}: {meanings[0].definitions[0]}",
        word=word,
        phonetic=phonetic,
        meanings=meanings,
        url=url,
    )


@typing.final
class SXNGPlugin(Plugin):
    """Dictionary card for "define <term>" queries."""

    id = "define_answer"
    keywords = KEYWORDS

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Dictionary",
            description=(
                "Shows an English dictionary card for queries like "
                "'define serendipity' or 'meaning of ephemeral'."
            ),
            examples=["define serendipity", "meaning of ephemeral"],
            keywords=self.keywords,
            preference_section="query",
        )

    def pre_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> bool:
        # dictionary API fetch overlaps the engine fan-out
        if search.search_query.pageno == 1:
            term = _parse_query(search.search_query.query)
            if term:
                prefetch("define:" + term.lower(), lambda: _fetch_entries(term))
        return True

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        term = _parse_query(search.search_query.query)
        if term is None:
            return results

        await_prefetch("define:" + term.lower(), lambda: None)
        entries = _fetch_entries(term)
        if not entries:
            return results

        answer = _build_answer(term, entries)
        if answer is None:
            return results

        results.add(answer)
        return results
