"""One HTTP client for every data source: User-Agent, per-host rate limit, retries, daily cache.

- SEC hosts get the SEC User-Agent (name + email, from an env var / GitHub secret)
  and are throttled under SEC's 10 requests/second limit.
- Transient failures (connection errors, 429, 5xx) are retried with exponential backoff.
- Successful responses are cached on disk for the day, so re-runs and repeat
  lookups don't hit SEC or Yahoo again.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

SEC_HOSTS = ("sec.gov",)  # matches www.sec.gov, data.sec.gov, efts.sec.gov ...
RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A source could not be fetched or parsed. Callers log it and carry on."""


def is_sec_host(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in SEC_HOSTS)


class HttpClient:
    def __init__(
        self,
        *,
        generic_user_agent: str,
        sec_user_agent: str | None,
        sec_max_requests_per_sec: float,
        timeout: float,
        max_retries: int,
        cache_dir: Path | None,
        today: date,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.generic_user_agent = generic_user_agent
        self.sec_user_agent = sec_user_agent
        self.sec_interval = 1.0 / sec_max_requests_per_sec
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache_dir = (cache_dir / today.isoformat()) if cache_dir else None
        self.session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_sec_request = float("-inf")
        self.requests_made = 0
        self.cache_hits = 0

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".txt")

    # ------------------------------------------------------------------ fetch
    def get_text(self, url: str, *, use_cache: bool = True) -> str:
        """Fetch ``url`` as text. Raises SourceError after retries are exhausted."""
        cache_path = self._cache_path(url) if use_cache else None
        if cache_path is not None and cache_path.is_file():
            self.cache_hits += 1
            return cache_path.read_text(encoding="utf-8")

        host = urlsplit(url).hostname or ""
        sec = is_sec_host(host)
        if sec and not self.sec_user_agent:
            raise SourceError(f"SEC User-Agent not configured; skipping {url}")
        headers = {
            "User-Agent": self.sec_user_agent if sec else self.generic_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }

        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._sleep(min(2.0**attempt, 30.0))
            if sec:
                wait = self._last_sec_request + self.sec_interval - self._clock()
                if wait > 0:
                    self._sleep(wait)
                self._last_sec_request = self._clock()
            try:
                self.requests_made += 1
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, last_error)
                continue
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                log.warning("GET %s -> %s (attempt %d)", url, resp.status_code, attempt + 1)
                continue
            if resp.status_code >= 400:
                raise SourceError(f"GET {url} -> HTTP {resp.status_code}")
            text = resp.text
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(text, encoding="utf-8")
            return text
        raise SourceError(f"GET {url} failed after {self.max_retries + 1} attempts: {last_error}")
