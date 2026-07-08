# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background prefetch shared by the cael instant-answer plugins.

Card plugins fetch external APIs (rates, quotes, definitions). Doing that in
post_search serializes the fetch AFTER the engine fan-out, adding its full
latency to the page. Instead, pre_search kicks the fetch off in a daemon
thread (the fetch functions populate their own module TTL caches), the
engines run meanwhile, and post_search joins the thread, usually finished,
then reads the warm cache.

Usage in a plugin:
    def pre_search(self, request, search):
        if _looks_like_trigger(search.search_query.query):
            prefetch("stock:" + symbol, lambda: _fetch_quote(symbol))
        return True

    def post_search(self, request, search):
        ...
        data = await_prefetch("stock:" + symbol, lambda: _fetch_quote(symbol))

The fetch function must be idempotent and do its own caching; await_prefetch
falls back to calling it inline when nothing was prefetched (then it is a
cache hit if the thread finished, or a normal inline fetch otherwise).
"""

from __future__ import annotations

import threading
import typing

_lock = threading.Lock()
_inflight: dict[str, threading.Thread] = {}

JOIN_TIMEOUT = 4.0  # seconds; fetches themselves time out well below this


def prefetch(key: str, fn: typing.Callable[[], typing.Any]) -> None:
    """Start fn in a background thread unless one is already running for key."""
    with _lock:
        existing = _inflight.get(key)
        if existing is not None and existing.is_alive():
            return

        def _run() -> None:
            try:
                fn()
            finally:
                with _lock:
                    if _inflight.get(key) is threading.current_thread():
                        del _inflight[key]

        thread = threading.Thread(target=_run, daemon=True, name=f"cael-prefetch-{key}")
        _inflight[key] = thread
        thread.start()


def await_prefetch(key: str, fn: typing.Callable[[], typing.Any]) -> typing.Any:
    """Join a pending prefetch for key, then return fn() (warm-cache read)."""
    with _lock:
        thread = _inflight.get(key)
    if thread is not None:
        thread.join(timeout=JOIN_TIMEOUT)
    return fn()
