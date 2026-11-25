"""Test helpers: a fake HTTP client that serves saved fixture files by URL."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from scanner.config import Settings
from scanner.sources.http import SourceError

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    """Serves fixture text by URL; any unknown URL behaves like a dead source."""

    def __init__(self, routes: dict[str, str]) -> None:
        self.routes = routes
        self.requested: list[str] = []
        self.requests_made = 0
        self.cache_hits = 0

    def get_text(self, url: str, *, use_cache: bool = True) -> str:
        self.requested.append(url)
        self.requests_made += 1
        if url not in self.routes:
            raise SourceError(f"GET {url} -> HTTP 503")
        return fixture(self.routes[url])


def standard_routes(s: Settings, *, sec: bool = True) -> dict[str, str]:
    src = s.sources
    routes: dict[str, str] = {}
    feeds = {f.name: f.url for f in src.wire_feeds}
    routes[feeds["globenewswire"]] = "globenewswire.rss"
    routes[feeds["prnewswire"]] = "prnewswire.rss"
    # businesswire and accesswire are left unrouted: they simulate dead feeds.
    if not sec:
        return routes
    per_form = {"8-K": "edgar_current_8k.atom", "424B4": "edgar_current_424b4.atom", "S-3": "edgar_current_s3.atom"}
    for form in src.edgar_forms:
        url = src.edgar_current_url.format(form=quote(form), start=0, count=src.edgar_page_size)
        routes[url] = per_form.get(form, "edgar_current_empty.atom")
    routes[src.ticker_map_url] = "company_tickers_exchange.json"
    routes[src.submissions_url.format(cik=4444444)] = "submissions_4444444.json"
    routes[src.submissions_url.format(cik=5555555)] = "submissions_5555555.json"
    base = "https://www.sec.gov/Archives/edgar/data/1111111/000111111126000042/"
    routes[base + "0001111111-26-000042-index.htm"] = "acme_8k_index.htm"
    routes[base + "ex99-1.htm"] = "acme_ex99_1.htm"
    return routes
