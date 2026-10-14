"""Async RSS/Atom ingestion.

Fetches all configured feeds concurrently (httpx.AsyncClient + asyncio.gather),
parses them with feedparser (sync, so it runs via asyncio.to_thread) and
returns a flat list of Article objects. A failing feed is skipped, it never
brings down the whole run. Dedup happens later in the storage layer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import feedparser
import httpx

FEEDS_FILE = Path(__file__).resolve().parent.parent / "feeds.txt"

# Used whenever feeds.txt is missing or has no active entries.
DEFAULT_FEEDS: list[str] = [
    "https://hnrss.org/newest?points=100",
    "https://lobste.rs/rss",
    "https://simonwillison.net/atom/everything/",
]


def load_feed_urls(path: Path = FEEDS_FILE) -> list[str]:
    """Read feed URLs from feeds.txt (one per line, # comments ignored);
    fall back to DEFAULT_FEEDS when the file is missing or empty."""
    if not path.exists():
        return DEFAULT_FEEDS
    lines = path.read_text(encoding="utf-8").splitlines()
    urls = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return urls or DEFAULT_FEEDS


FEED_URLS: list[str] = load_feed_urls()


@dataclass
class Article:
    """One article from an RSS/Atom feed."""

    source_url: str           # URL of the feed the article came from
    title: str
    url: str
    published_at: str | None  # ISO 8601 string, or None
    summary: str | None       # short excerpt from the feed, if present


async def fetch_feed(client: httpx.AsyncClient, url: str) -> str | None:
    """Download raw XML of one feed; return None on failure so one dead
    source never kills the whole run."""
    try:
        response = await client.get(url, timeout=10.0)
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as e:
        print(f"fetch feed {url} failed: {e}")
        return None


async def fetch_all_feeds(urls: list[str]) -> dict[str, str]:
    """Download all feeds concurrently over one shared AsyncClient.

    Returns {feed_url: raw_xml} for the feeds that succeeded."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(fetch_feed(client, url) for url in urls))
    return {url: xml for url, xml in zip(urls, results) if xml is not None}


def _parse_sync(raw_xml: str) -> feedparser.FeedParserDict:
    # feedparser is blocking; this thin wrapper runs via asyncio.to_thread.
    return feedparser.parse(raw_xml)


async def parse_feed_entries(source_url: str, raw_xml: str) -> list[Article]:
    """Parse raw feed XML into Article objects. Entries without a URL are
    skipped (nothing to link to); a missing title falls back to a placeholder
    - either way, one malformed entry never kills the whole run."""
    feed = await asyncio.to_thread(_parse_sync, raw_xml)
    articles = []
    for entry in feed.entries:
        url = getattr(entry, "link", None)
        if not url:
            continue
        articles.append(
            Article(
                source_url=source_url,
                title=getattr(entry, "title", "(no title)"),
                url=url,
                published_at=getattr(entry, "published", None),
                summary=getattr(entry, "summary", None),
            )
        )
    return articles


async def ingest(urls: list[str]) -> list[Article]:
    """Fetch and parse all sources; return a flat list of articles."""
    raw_feeds = await fetch_all_feeds(urls)
    parsed = await asyncio.gather(
        *(parse_feed_entries(source_url, xml) for source_url, xml in raw_feeds.items())
    )
    return [article for feed_articles in parsed for article in feed_articles]


async def _main() -> None:
    

    articles = await ingest(FEED_URLS)
    print(f"Fetched {len(articles)} articles from {len(FEED_URLS)} sources.")
    for article in articles[:5]:
        print(f"  - {article.title}")


if __name__ == "__main__":
    asyncio.run(_main())
