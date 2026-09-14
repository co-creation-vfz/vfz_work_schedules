# =============================================================================
# cache.py
# Today's non-working list, held in memory.
#
# This is the reason the API exists rather than giving Workato the database
# directly. Workato calls on every @tag in a comment, but the table is
# rewritten once a day by the resync -- so almost every request can be answered
# without touching MySQL at all.
#
# Three properties matter, and each one is a failure mode avoided:
#
#   Keyed by date    A cache keyed only by time would keep serving yesterday's
#                    list past midnight, and the whole point of the table is
#                    which day it describes.
#   Serves stale on  If MySQL is unreachable, returning an empty list would
#   failure          mean "everyone is working" -- suppressing a legitimate
#                    warning. A few minutes stale is far better than wrong.
#   Single refresh   A burst of @tags arriving on a cold cache must not become
#                    a burst of identical queries. One thread refreshes, the
#                    rest wait for it.
# =============================================================================

import threading
import time
from datetime import date as Date
from typing import Any, Callable, Dict, List, Optional


class NonWorkingCache:
    """Caches one day's non-working list, with a TTL."""

    def __init__(self, loader: Callable[[Date], List[Dict[str, Any]]], ttl_seconds: int):
        """
        :param loader:      Called as ``loader(on_date)`` to read the real rows.
        :param ttl_seconds: How long a loaded list stays fresh. 0 disables
                            caching, which is useful in a test.
        """
        self._loader = loader
        self._ttl = max(0, ttl_seconds)
        self._lock = threading.Lock()

        self._date: Optional[Date] = None
        self._rows: Optional[List[Dict[str, Any]]] = None
        self._loaded_at: float = 0.0
        # Kept separately from _rows so a failed refresh can still serve the
        # last good answer while reporting that it is stale.
        self._error: Optional[BaseException] = None

        self.hits = 0
        self.misses = 0
        self.stale_serves = 0

    def get(self, on_date: Date) -> Dict[str, Any]:
        """
        Today's rows, from cache when fresh.

        :returns: ``{"rows": [...], "cached": bool, "ageSeconds": float,
                    "stale": bool}``. ``stale`` is True when MySQL could not be
                    read and this is the last known good answer.
        :raises:  Whatever the loader raised, but only when there is no
                  previous answer to fall back on.
        """
        with self._lock:
            fresh = (
                self._rows is not None
                and self._date == on_date
                and (time.monotonic() - self._loaded_at) < self._ttl
            )
            if fresh:
                self.hits += 1
                return {
                    "rows": list(self._rows),
                    "cached": True,
                    "ageSeconds": round(time.monotonic() - self._loaded_at, 3),
                    "stale": False,
                }

            # Inside the lock deliberately: a burst of @tags on a cold cache
            # would otherwise all miss and all query. One reads, the others
            # get the result it just stored.
            self.misses += 1
            try:
                rows = self._loader(on_date)
            except Exception as exc:
                self._error = exc
                if self._rows is not None and self._date == on_date:
                    # Better a few minutes old than an empty list, which reads
                    # as "everyone is working".
                    self.stale_serves += 1
                    return {
                        "rows": list(self._rows),
                        "cached": True,
                        "ageSeconds": round(time.monotonic() - self._loaded_at, 3),
                        "stale": True,
                    }
                raise

            self._error = None
            self._date = on_date
            self._rows = rows
            self._loaded_at = time.monotonic()
            return {
                "rows": list(rows),
                "cached": False,
                "ageSeconds": 0.0,
                "stale": False,
            }

    def invalidate(self) -> None:
        """Drop the cached list, so the next request re-reads MySQL."""
        with self._lock:
            self._rows = None
            self._date = None
            self._loaded_at = 0.0

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "staleServes": self.stale_serves,
                "hitRate": round(self.hits / total, 4) if total else None,
                "ttlSeconds": self._ttl,
                "cachedDate": self._date.isoformat() if self._date else None,
                "cachedRows": len(self._rows) if self._rows is not None else None,
                "ageSeconds": (
                    round(time.monotonic() - self._loaded_at, 3)
                    if self._rows is not None
                    else None
                ),
                "lastError": str(self._error) if self._error else None,
            }
