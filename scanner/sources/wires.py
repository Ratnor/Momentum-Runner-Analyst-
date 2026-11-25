"""Newswire RSS adapters (GlobeNewswire, PR Newswire, Business Wire, Accesswire) and
Yahoo Finance per-ticker headline RSS. All parsed with feedparser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import feedparser

from scanner.config import SourcesConfig
from scanner.sources.base import RawItem, SourceError, html_to_text, to_utc
from scanner.sources.http import HttpClient

log = logging.getLogger(__name__)


def parse_rss(text: str, source: str) -> list[RawItem]:
    """Parse an RSS/Atom feed into RawItems. Raises SourceError if nothing parseable."""
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise SourceError(f"{source}: feed parse error: {feed.bozo_exception}")
    items: list[RawItem] = []
    for e in feed.entries:
        title = html_to_text(e.get("title", "")).strip()
        link = e.get("link", "").strip()
        if not title or not link:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        tags = [t.get("term", "").strip() for t in e.get("tags", []) if t.get("term")]
        items.append(
            RawItem(
                source=source,
                url=link,
                headline=title,
                published_at=to_utc(datetime(*stamp[:6])) if stamp else None,
                summary=html_to_text(e.get("summary", "")),
                tags=tags,
            )
        )
    return items


@dataclass
class FeedResult:
    name: str
    items: list[RawItem]
    error: str | None = None


class WireClient:
    def __init__(self, http: HttpClient, cfg: SourcesConfig) -> None:
        self.http = http
        self.cfg = cfg

    def fetch_all(self) -> list[FeedResult]:
        """Fetch every configured feed. A dead feed is reported, never fatal."""
        results: list[FeedResult] = []
        for feed in self.cfg.wire_feeds:
            try:
                text = self.http.get_text(feed.url, use_cache=False)
                results.append(FeedResult(feed.name, parse_rss(text, feed.name)))
            except SourceError as exc:
                log.warning("feed %s failed: %s", feed.name, exc)
                results.append(FeedResult(feed.name, [], str(exc)))
        return results

    def yahoo_headlines(self, ticker: str) -> list[RawItem]:
        """Secondary per-ticker headline source (used for candidates and held positions)."""
        url = self.cfg.yahoo_headline_url.format(ticker=quote(ticker))
        return parse_rss(self.http.get_text(url), "yahoo")
