"""Common types for data-source adapters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

from scanner.sources.http import SourceError

__all__ = ["RawItem", "SourceError", "html_to_text", "text_hash", "to_utc"]


@dataclass
class RawItem:
    """One ingested filing or headline, before ticker matching and scoring."""

    source: str  # 'edgar', 'globenewswire', 'yahoo', ...
    url: str
    headline: str
    published_at: datetime | None  # timezone-aware UTC
    summary: str = ""  # plain-text teaser / first paragraph
    tags: list[str] = field(default_factory=list)  # RSS categories (may hold "NASDAQ:ABCD")
    cik: int | None = None  # EDGAR only
    company: str | None = None  # EDGAR only
    form_type: str | None = None  # EDGAR only
    accession: str | None = None  # EDGAR only
    items: list[str] = field(default_factory=list)  # 8-K item codes, e.g. ["8.01", "9.01"]
    body: str | None = None  # full text, when fetched

    @property
    def text_hash(self) -> str:
        return text_hash(self.headline + "\n" + self.summary + "\n" + (self.body or ""))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class _TextExtractor(HTMLParser):
    BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "title", "section", "article", "header", "footer",
    }
    SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip tags; keep block boundaries as newlines; collapse whitespace within lines."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    raw = "".join(p.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in raw.split("\n"))
    return "\n".join(ln for ln in lines if ln)
