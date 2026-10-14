"""HTTP API for the local 公考情报平台.

Serves the single-page UI plus a small JSON API:
POST /api/run kicks one collect->digest round in a worker thread, every run
is archived (GET /api/runs[/{id}]) so history survives restarts. SQLite
connections are opened per request via dependency injection.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import pipeline
import storage
import scraper

app = FastAPI(title="西安公考情报平台")

STATIC_DIR = Path(__file__).resolve().parent / "static"
WEB_SOURCE_URLS = {site["url"] for site in scraper.SITES}


def get_db():
    """Per-request DB connection (FastAPI dependency with teardown)."""
    conn = storage.init_db()
    try:
        yield conn
    finally:
        conn.close()


class SourceIn(BaseModel):
    url: str
    name: str | None = None


class ProfileIn(BaseModel):
    degree: str = ""
    school: str = ""
    major: str = ""
    skills: str = ""
    notes: str = ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/run")
def start_run(db=Depends(get_db)):
    """Trigger one full round; only one may run at a time."""
    if storage.has_running_run(db):
        raise HTTPException(409, "已有一轮推送正在进行中")
    run_id = storage.create_run(db)
    threading.Thread(target=pipeline.run_pipeline, args=(run_id,),
                     daemon=True).start()
    return {"id": run_id}


@app.get("/api/runs")
def list_runs(limit: int = 50, db=Depends(get_db)):
    return [dict(row) for row in storage.list_runs(db, limit)]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, db=Depends(get_db)):
    row = storage.get_run(db, run_id)
    if row is None:
        raise HTTPException(404, "运行记录不存在")
    return dict(row)


@app.get("/api/articles")
def list_articles(days: int = 30, source: str | None = None,
                  q: str | None = None, category: str | None = None,
                  db=Depends(get_db)):
    rows = storage.query_articles(db, days=days, source=source, q=q,
                                  category=category)
    return [dict(row) for row in rows]


@app.get("/api/sources")
def list_sources(db=Depends(get_db)):
    rows = db.execute(
        """
        SELECT sources.id, sources.name, sources.url, COUNT(articles.id) AS count
        FROM sources LEFT JOIN articles ON articles.source_id = sources.id
        GROUP BY sources.id ORDER BY count DESC
        """
    ).fetchall()
    return [
        {**dict(row), "type": "web" if row["url"] in WEB_SOURCE_URLS else "rss"}
        for row in rows
    ]


@app.post("/api/sources", status_code=201)
def add_source(body: SourceIn, db=Depends(get_db)):
    """Add an RSS feed (web sources are fixed in scraper.SITES)."""
    if body.url in WEB_SOURCE_URLS:
        raise HTTPException(400, "该站点由内置爬虫采集，无需添加")
    import httpx
    try:
        response = httpx.get(body.url, headers={"User-Agent": scraper.UA},
                             timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        if "<feed" not in response.text[:2000] and "<rss" not in response.text[:2000]:
            raise HTTPException(400, "该地址不像是有效的 RSS/Atom 源")
    except httpx.HTTPError as e:
        raise HTTPException(400, f"源不可访问：{e}")
    storage.get_or_create_source(db, body.name or body.url, body.url)
    with pipeline.FEEDS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(body.url + "\n")
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
def remove_source(source_id: int, db=Depends(get_db)):
    row = db.execute("SELECT * FROM sources WHERE id = ?",
                     (source_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "数据源不存在")
    if row["url"] in WEB_SOURCE_URLS:
        raise HTTPException(400, "内置政府站点不可删除")
    lines = pipeline._rss_urls()
    kept = [ln for ln in lines if ln != row["url"]]
    pipeline.FEEDS_FILE.write_text(
        "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    with db:
        db.execute("DELETE FROM articles WHERE source_id = ?", (source_id,))
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return {"ok": True}


@app.get("/api/profile")
def get_profile(db=Depends(get_db)):
    return storage.get_profile(db)


@app.put("/api/profile")
def save_profile(body: ProfileIn, db=Depends(get_db)):
    data = body.model_dump()
    if not any(data.values()):
        raise HTTPException(400, "档案内容不能全为空")
    storage.save_profile(db, data)
    return storage.get_profile(db)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
