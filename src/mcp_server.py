"""MCP server exposing the digest pipeline as tools.

Runs on stdio transport; register with an MCP host, e.g.:
claude mcp add news-digest -- uv run --directory <repo> python src/mcp_server.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import httpx
from mcp.server import MCPServer

import ingest
import storage

mcp = MCPServer("news-digest")

DB_PATH = str(Path(__file__).resolve().parent.parent / "digest.db")

FEEDS_HEADER = """\
# Your feeds - one URL per line, lines starting with # are ignored.
# While this file has no active entries, the bundled defaults are used
# (see DEFAULT_FEEDS in src/ingest.py).
"""


def _is_public_host(url: str) -> bool:
    """Best-effort SSRF guard for add_source: reject URLs whose host resolves
    to loopback/private/link-local ranges, so a prompted model cannot point
    the fetch at localhost or the internal network."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def _read_feeds_file() -> list[str]:
    """Active entries in feeds.txt; empty list means bundled defaults apply."""
    if not ingest.FEEDS_FILE.exists():
        return []
    return [
        ln.strip() for ln in ingest.FEEDS_FILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _write_feeds_file(urls: list[str]) -> None:
    body = "\n".join(urls)
    ingest.FEEDS_FILE.write_text(FEEDS_HEADER + (body + "\n" if body else ""),
                                 encoding="utf-8")


@mcp.tool()
async def fetch_latest() -> str:
    """Fetch the latest articles from all configured RSS feeds and store
    them (duplicates are skipped). Use when the user wants to refresh the
    archive or asks about the newest articles."""
    articles = await ingest.ingest(ingest.load_feed_urls())
    db_conn = storage.init_db(DB_PATH)

    try:
        by_source: dict[str, list] = {}

        for article in articles:
            by_source.setdefault(article.source_url, []).append(article)

        total_inserted = 0
        for source_url, source_articles in by_source.items():
            source_id = storage.get_or_create_source(db_conn, source_url, source_url)
            total_inserted += storage.save_articles(db_conn, source_id, source_articles)
    finally:
        db_conn.close()

    return f"Added {total_inserted} new articles from {len(by_source)} sources."

def db_connect() -> sqlite3.Connection:
    db_conn = storage.init_db(DB_PATH)
    db_conn.row_factory = sqlite3.Row

    return db_conn

@mcp.tool()
async def search_archive(query: str) -> str:
    """Search stored articles by keyword (matches title or summary).
    Use when the user asks about a specific topic from the archive."""
    db_conn = db_connect()

    try:
        rows = storage.search_articles(db_conn, query)
    finally:
        db_conn.close()

    if not rows:
        return "Nothing found."

    return "\n".join(f"- {row['title']} ({row['url']})" for row in rows)

@mcp.tool()
async def get_articles_for_digest(days: int = 7, topic: str | None = None) -> str:
    """Return raw article data (title, URL, source, summary) from the last
    `days` days as a markdown list. Use when you (the host model) should
    write the digest yourself - this tool only provides the source material.

    `topic` is an optional keyword pre-filter (whole-word match) - useful for
    large archives. Omit it to get everything and pick relevant articles
    yourself, which handles synonyms and related themes better."""
    db_connection = db_connect()

    try:
        articles = storage.articles_since(db_connection, days)
        source_names = {
            row["id"]: row["name"] for row in db_connection.execute("SELECT id, name FROM sources")
        }
    finally:
        db_connection.close()

    if topic:
        pattern = re.compile(rf"\b{re.escape(topic)}\b", re.IGNORECASE)
        articles = [
            row for row in articles
            if pattern.search(row["title"])
            or (row["summary"] and pattern.search(row["summary"]))
        ]

    if not articles:
        day_or_days_string = "days" if days != 1 else "day"
        scope = f" matching '{topic}'" if topic else ""
        return f"No articles{scope} in the last {days} {day_or_days_string}."

    heading_scope = f" about '{topic}'" if topic else ""
    lines = [f"# Articles{heading_scope} from the last {days} days ({len(articles)})", ""]
    for row in articles:
        source_name = source_names.get(row["source_id"], "?")
        lines.append(f"- [{row['title']}]({row['url']}) - {source_name}")

        if row["summary"]:
            lines.append(f"  {row['summary']}")

    return "\n".join(lines)

@mcp.tool()
async def make_digest(days: int = 7) -> str:
    """Build a ready-made markdown digest of the most relevant articles
    from the last `days` days using the server's own LLM pipeline.
    Not implemented yet - use get_articles_for_digest instead."""
    db_connection = db_connect()

    try:
        articles = storage.articles_since(db_connection, days)
    finally:
        db_connection.close()

    if not articles:
        return "Nothing to digest."

    raise NotImplementedError



@mcp.tool()
async def list_sources() -> str:
    """List the feeds the digest currently aggregates, with the number of
    stored articles per source. Reports whether the app runs on bundled
    default feeds (feeds.txt is empty) or the user's own selection."""
    own = _read_feeds_file()
    active = own or ingest.DEFAULT_FEEDS

    db_conn = db_connect()
    try:
        counts = {
            row["source_name"]: row["article_count"]
            for row in storage.articles_per_source(db_conn)
        }
    finally:
        db_conn.close()

    mode = "user-defined feeds" if own else "bundled defaults - feeds.txt is empty"
    lines = [f"Active sources ({mode}):"]
    for url in active:
        lines.append(f"- {url} ({counts.get(url, 0)} articles stored)")
    return "\n".join(lines)


@mcp.tool()
async def add_source(url: str, keep_defaults: bool = False) -> str:
    """Validate and add a new RSS/Atom feed to feeds.txt. Use when the user
    wants to follow a new source. The feed is downloaded and parsed first -
    invalid or dead URLs are rejected, nothing is written.

    While feeds.txt is empty the app runs on bundled default feeds. When
    adding the user's FIRST own feed, ask them once whether to keep the
    defaults too (then call this with keep_defaults=True) or start fresh
    with only their own sources. Do not ask again once feeds.txt has
    entries."""
    if not url.startswith(("http://", "https://")):
        return f"Rejected: '{url}' is not an http(s) URL."
    if not await asyncio.to_thread(_is_public_host, url):
        return f"Rejected: {url} does not resolve to a public address."

    async with httpx.AsyncClient() as client:
        raw = await ingest.fetch_feed(client, url)
    if raw is None:
        return f"Rejected: could not download {url}."
    feed = await asyncio.to_thread(ingest._parse_sync, raw)
    if not feed.entries:
        return f"Rejected: {url} does not look like an RSS/Atom feed (no entries)."

    own = _read_feeds_file()
    if url in own:
        return f"Already present: {url}"

    if own:
        new_list = own + [url]
    elif keep_defaults:
        new_list = [*ingest.DEFAULT_FEEDS, url]
    else:
        new_list = [url]
    _write_feeds_file(new_list)

    title = feed.feed.get("title", url)
    return f"Added '{title}' ({url}). Active sources now: {len(new_list)}."


@mcp.tool()
async def remove_source(url: str) -> str:
    """Remove a feed from feeds.txt. Use when the user no longer wants a
    source. Already stored articles from that source stay in the archive."""
    own = _read_feeds_file()
    if url not in own:
        hint = (
            " (the app runs on bundled defaults - they cannot be removed one"
            " by one; add the user's own feeds to replace them)"
            if not own else ""
        )
        return f"Not in feeds.txt: {url}{hint}"

    remaining = [u for u in own if u != url]
    _write_feeds_file(remaining)
    note = " feeds.txt is now empty, so the bundled defaults apply again." if not remaining else ""
    return f"Removed {url}.{note}"


@mcp.prompt()
def setup_sources(topic: str) -> str:
    """Reusable prompt: find, validate and add quality feeds for a topic."""
    return (
        f"Suggest 3-5 quality RSS/Atom feeds about {topic}. Check "
        "list_sources first - if the app still runs on bundled defaults, ask "
        "the user whether to keep them alongside the new feeds or start "
        "fresh. Then add each candidate via add_source (it validates the "
        "feed) and report which were added and which were rejected."
    )


@mcp.prompt()
def daily_digest(topic: str = "", days: int = 1) -> str:
    """Reusable prompt: refresh the archive and write a themed digest,
    optionally focused on one topic."""
    focus = f" Focus only on articles related to: {topic}." if topic else ""
    return (
        f"Fetch the latest articles, then load the archive from the last {days} "
        f"day(s) via get_articles_for_digest and write a digest.{focus} Group "
        "articles by theme and keep bullets short. Render every article as a "
        "clickable markdown link - [title](url) - using the URL from the tool "
        "output, so the reader can open the source directly; never list a title "
        "without its link. End with a one-line note about what was left out."
    )


if __name__ == "__main__":
    mcp.run()
