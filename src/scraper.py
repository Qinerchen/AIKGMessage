"""Direct page scraping for government sites without RSS.

All targets are static list pages on xa.gov.cn (server-rendered); each <li>
item holds a <span>YYYY-MM-DD</span> date followed by an
<a href="/gk/..." title="标题"> link. One failing site is skipped with a log
line - it never brings down the whole run.

xapta.xa.gov.cn (西安人事考试网) is a Vue SPA rendered client-side, so it
cannot be scraped here; its 公务员/事业单位 announcements are published on
the xa.gov.cn gwykl channel below, which covers the same content.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ingest import Article

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# name -> shown as the source name; url -> list page; path -> href filter.
SITES: list[dict] = [
    {
        "name": "西安市公务员考录",
        "url": "https://www.xa.gov.cn/gk/rsxx/gwykl/1.html",
        "path": r"^/gk/rsxx/gwykl/2\d{15,}\.html$",
    },
    {
        "name": "西安市人事任免",
        "url": "https://www.xa.gov.cn/gk/rsxx/rsrm/1.html",
        "path": r"^/gk/rsxx/rsrm/2\d{15,}\.html$",
    },
    {
        "name": "西安市政府文件",
        "url": "https://www.xa.gov.cn/gk/zcfg/zcwj/xaszfwj/1.html",
        "path": r"^/gk/zcfg/zcwj/xaszfwj/2\d{15,}\.html$",
    },
]

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def fetch_site(site: dict) -> list[Article]:
    """Download one list page and extract (date, title, link) items.

    Raises on network/parse failure - the caller decides whether to skip."""
    response = httpx.get(site["url"], headers={"User-Agent": UA},
                         timeout=15.0, follow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    articles: list[Article] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if not re.match(site["path"], href):
            continue
        title = (link.get("title") or link.get_text()).strip()
        if not title:
            continue
        url = urljoin(str(response.url), href)
        if url in seen:
            continue
        seen.add(url)
        published_at = None
        item = link.find_parent("li")
        if item:
            for candidate in item.find_all("span"):
                if DATE_RE.fullmatch(candidate.get_text(strip=True) or ""):
                    published_at = candidate.get_text(strip=True)
                    break
        articles.append(Article(
            source_url=site["url"], title=title, url=url,
            published_at=published_at, summary=None,
        ))
    return articles


def scrape_all() -> tuple[list[Article], list[str]]:
    """Scrape every configured site; return (articles, skipped_site_names)."""
    articles: list[Article] = []
    skipped: list[str] = []
    for site in SITES:
        try:
            found = fetch_site(site)
            articles.extend(found)
            print(f"scraper: {site['name']} -> {len(found)} items")
        except Exception as e:  # one dead site never kills the run
            print(f"scraper: {site['name']} failed, skipped: {e}")
            skipped.append(site["name"])
    return articles, skipped


def _main() -> None:
    articles, skipped = scrape_all()
    print(f"total {len(articles)} items, skipped: {skipped}")
    for article in articles[:8]:
        print(f"  [{article.published_at}] {article.title} -> {article.url}")


if __name__ == "__main__":
    _main()
