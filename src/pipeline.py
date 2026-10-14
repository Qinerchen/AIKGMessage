"""One full "推送" round: collect -> dedup/store -> classify -> digest.

Runs synchronously inside a worker thread (started by api.py). Every stage
updates the run row so the UI can show progress; any uncaught error marks
the run failed instead of crashing the server.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from pathlib import Path

import ingest
import llm
import scraper
import storage
from ingest import Article

FEEDS_FILE = Path(__file__).resolve().parent.parent / "feeds.txt"

# 报考 keywords first - it is the platform's core output and must win ties.
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("报考", ["招聘", "公告", "公务员", "事业单位", "遴选", "报名", "岗位",
             "笔试", "资格复审", "体检", "人才引进", "考录", "录用", "招考"]),
    ("政策", ["政策", "报告", "任免", "方案", "意见", "规划", "文件", "条例",
             "解读"]),
]
FALLBACK_CATEGORY = "申论"

# Degraded-mode "可能可报" markers, matched against the poster's profile.
MATCH_HINTS = ["建筑", "规划", "住建", "土木", "工程", "设计", "不限专业", "本科"]

_SOURCE_NAMES = {site["url"]: site["name"] for site in scraper.SITES}


def classify(title: str) -> str:
    for category, keywords in CATEGORY_RULES:
        if any(k in title for k in keywords):
            return category
    return FALLBACK_CATEGORY


def match_hint(title: str, profile: dict) -> bool:
    """Cheap profile-based filter used when no LLM key is configured."""
    text = f"{title} {profile.get('major', '')}"
    return any(k in text for k in MATCH_HINTS)


def _rss_urls() -> list[str]:
    """Active lines from feeds.txt; unlike ingest.load_feed_urls, an empty
    file means NO rss stage (the tech defaults would pollute this domain)."""
    if not FEEDS_FILE.exists():
        return []
    return [ln.strip() for ln in FEEDS_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _source_name(source_url: str) -> str:
    return _SOURCE_NAMES.get(source_url, source_url)


def collect() -> tuple[list[Article], list[str]]:
    """Gather articles from RSS (optional) and the scraped gov sites."""
    articles: list[Article] = []
    skipped: list[str] = []

    urls = _rss_urls()
    if urls:
        try:
            rss_articles = asyncio.run(ingest.ingest(urls))
            articles.extend(rss_articles)
            print(f"pipeline: rss -> {len(rss_articles)} items")
        except Exception as e:
            print(f"pipeline: rss failed, skipped: {e}")
            skipped.append("RSS")

    scraped, scrape_skipped = scraper.scrape_all()
    articles.extend(scraped)
    skipped.extend(scrape_skipped)
    return articles, skipped


def store(articles: list[Article]) -> tuple[int, int]:
    """Dedup-store all articles with their category; return (new, total)."""
    conn = storage.init_db()
    try:
        web_urls = {site["url"] for site in scraper.SITES}
        by_source: dict[str, list[Article]] = {}
        for article in articles:
            by_source.setdefault(article.source_url, []).append(article)

        new = total = 0
        for source_url, items in by_source.items():
            source_id = storage.get_or_create_source(
                conn, _source_name(source_url), source_url)
            source_type = "web" if source_url in web_urls else "rss"
            for item in items:
                new += storage.save_articles(
                    conn, source_id, [item],
                    category=classify(item.title), source_type=source_type)
            total += len(items)
        return new, total
    finally:
        conn.close()


def fallback_digest(conn, run_new: int, skipped: list[str]) -> str:
    """Markdown report without any LLM: grouped entry lists + match hints."""
    profile = storage.get_profile(conn)
    recent = storage.articles_since(conn, days=30)
    grouped: dict[str, list] = {"报考": [], "政策": [], "申论": []}
    for row in recent:
        grouped.setdefault(row["category"] or "申论", []).append(row)

    lines = [
        "# 西安公考情报速递", "",
        f"日期：{time.strftime('%Y-%m-%d')}　｜　本轮新增 {run_new} 条", "",
        "> 当前未配置 LLM API Key，以下为条目列表模式（分类打标 + 可能可报提示）。",
        "",
    ]
    if skipped:
        lines += [f"> 本轮跳过的源：{'、'.join(skipped)}", ""]

    exam = grouped.get("报考", [])
    lines += ["## 一、报考速览（核心）", ""]
    if not exam:
        lines += ["（近30天暂无报考类条目）", ""]
    for row in exam:
        hint = "　✅ **可能可报**" if match_hint(row["title"], profile) else ""
        lines += [f"- [{row['title']}]({row['url']})",
                  f"  {row['source_name']}　{row['published_at'] or ''}{hint}"]
    lines.append("")

    for label, key in (("二、政策动向", "政策"), ("三、申论素材（未总结）", "申论")):
        lines += [f"## {label}", ""]
        for row in grouped.get(key, []):
            lines.append(f"- [{row['title']}]({row['url']})　"
                         f"{row['source_name']}　{row['published_at'] or ''}")
        lines.append("")

    lines += ["## 岗位推荐（降级提示）", "",
              "未配置 LLM，无法生成正式岗位推荐；上方带 ✅ 的报考条目为按档案关键词"
              f"（专业：{profile.get('major', '')}）初筛的「可能可报」提示，"
              "请以公告原文为准。"]
    return "\n".join(lines)


def run_pipeline(run_id: int) -> None:
    """Execute one round end-to-end, updating the run row at every stage."""
    conn = storage.init_db()
    try:
        storage.set_run_stage(conn, run_id, "正在采集（政府站点 + RSS）")
        articles, skipped = collect()

        storage.set_run_stage(conn, run_id, "正在去重入库")
        new_count, total_count = store(articles)

        storage.set_run_stage(conn, run_id, "正在生成汇总与岗位推荐")
        digest_md = None
        if llm.available():
            try:
                profile = storage.get_profile(conn)
                recent = storage.articles_since(conn, days=30)
                digest_md = llm.build_digest(recent, profile)
            except Exception as e:
                print(f"pipeline: llm stage failed, falling back: {e}")
                skipped.append(f"LLM({e})")
        if digest_md is None:
            digest_md = fallback_digest(conn, new_count, skipped)

        storage.finish_run(conn, run_id, "done", new_count=new_count,
                           total_count=total_count, digest_md=digest_md)
    except Exception as e:
        traceback.print_exc()
        storage.finish_run(conn, run_id, "failed", error=str(e))
    finally:
        conn.close()
