"""SQLite storage layer.

Schema (sources, articles), dedup by article URL (UNIQUE + INSERT OR IGNORE)
and the queries used by the MCP/HTTP layers. Raw SQL by design, no ORM;
every query is parametrized.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    title        TEXT NOT NULL,
    url          TEXT NOT NULL UNIQUE,
    published_at TEXT,
    summary      TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);

-- One row per "推送" click: the report snapshot IS the history.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running / done / failed
    stage       TEXT,
    new_count   INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    digest_md   TEXT,
    error       TEXT
);

-- Single-row personal profile used for job-posting recommendations.
CREATE TABLE IF NOT EXISTS profile (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""

# The repo-root digest.db, independent of the process working directory.
DB_PATH = str(Path(__file__).resolve().parent.parent / "digest.db")

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the DB and ensure the schema exists; safe to call repeatedly."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn

def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add later-added articles columns to pre-existing databases (idempotent)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    for column in ("category", "source_type"):
        if column not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")

def get_or_create_source(conn: sqlite3.Connection, name: str, url: str) -> int:
    """Return the id of the source with the given `url`, creating it if needed (idempotent)."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, url) VALUES (?, ?)", (name, url)
        )
    row = conn.execute("SELECT id FROM sources WHERE url = ?", (url,)).fetchone()
    return row[0]

def save_articles(
    conn: sqlite3.Connection,
    source_id: int,
    articles: list,
    category: str | None = None,
    source_type: str | None = None,
) -> int:
    """Store articles for a source, deduped by URL; return the number actually inserted."""
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with conn:
        for article in articles:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO articles
                    (source_id, title, url, published_at, summary, fetched_at,
                     category, source_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    article.title,
                    article.url,
                    article.published_at,
                    article.summary,
                    now,
                    category,
                    source_type,
                )
            )
            inserted += cursor.rowcount
    return inserted

def articles_since(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """Return articles fetched in the last `days` days, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        """
        SELECT articles.*, sources.name AS source_name
        FROM articles JOIN sources ON sources.id = articles.source_id
        WHERE articles.fetched_at >= ?
        ORDER BY articles.fetched_at DESC
        """,
        (cutoff,),
    )
    return cursor.fetchall()

def articles_per_source(conn: sqlite3.Connection, min_count: int = 0) -> list[sqlite3.Row]:
    """Return (source_name, article_count) per source, descending by count.
    `min_count` filters out small sources (HAVING)."""
    cursor = conn.execute(
        """
        SELECT sources.name AS source_name, COUNT(*) AS article_count
        FROM articles
        JOIN sources ON sources.id = articles.source_id
        GROUP BY sources.name
        HAVING COUNT(*) >= ?
        ORDER BY article_count DESC
        """,
        (min_count,),
    )
    return cursor.fetchall()

def search_articles(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """Case-insensitive substring search in title or summary."""
    pattern = f"%{query}%"
    cursor = conn.execute(
        """
        SELECT * FROM articles
        WHERE title LIKE ? OR summary LIKE ?
        ORDER BY fetched_at DESC
        """,
        (pattern, pattern),
    )
    return cursor.fetchall()


# ---------------------------------------------------------------- runs

def create_run(conn: sqlite3.Connection) -> int:
    """Open a new run row (status=running) and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        cursor = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now,)
        )
    return cursor.lastrowid

def set_run_stage(conn: sqlite3.Connection, run_id: int, stage: str) -> None:
    with conn:
        conn.execute("UPDATE runs SET stage = ? WHERE id = ?", (stage, run_id))

def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    new_count: int = 0,
    total_count: int = 0,
    digest_md: str | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, new_count = ?, total_count = ?,
                digest_md = ?, error = ?
            WHERE id = ?
            """,
            (now, status, new_count, total_count, digest_md, error, run_id),
        )

def get_run(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()

def has_running_run(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- profile

DEFAULT_PROFILE: dict = {
    "degree": "本科",
    "school": "兰州交通大学",
    "major": "建筑学",
    "skills": "UE（Unreal Engine）、编程、AI Agent 应用开发",
    "notes": "关注西安公务员/事业单位/人才引进中自己可报的岗位",
}

def get_profile(conn: sqlite3.Connection) -> dict:
    """Return the profile as a dict; seed the default on first access."""
    row = conn.execute("SELECT data FROM profile WHERE id = 1").fetchone()
    if row is None:
        with conn:
            conn.execute("INSERT INTO profile (id, data) VALUES (1, ?)",
                         (json.dumps(DEFAULT_PROFILE, ensure_ascii=False),))
        return dict(DEFAULT_PROFILE)
    return json.loads(row["data"])

def save_profile(conn: sqlite3.Connection, data: dict) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO profile (id, data) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data
            """,
            (json.dumps(data, ensure_ascii=False),),
        )


# ---------------------------------------------------------------- queries for the API

def query_articles(
    conn: sqlite3.Connection,
    days: int = 7,
    source: str | None = None,
    q: str | None = None,
    category: str | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Filtered article search for the web UI; all filters optional."""
    sql = """
        SELECT articles.*, sources.name AS source_name
        FROM articles JOIN sources ON sources.id = articles.source_id
        WHERE 1=1
    """
    params: list = []
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        sql += " AND articles.fetched_at >= ?"
        params.append(cutoff)
    if source:
        sql += " AND sources.name LIKE ?"
        params.append(f"%{source}%")
    if q:
        sql += " AND (articles.title LIKE ? OR articles.summary LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    if category:
        sql += " AND articles.category = ?"
        params.append(category)
    sql += " ORDER BY articles.published_at DESC, articles.fetched_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _main() -> None:
    conn = init_db()
    conn.row_factory = sqlite3.Row

    source_id = get_or_create_source(conn, "Hacker News", "https://hnrss.org/frontpage")
    print(f"source_id = {source_id}")

    @dataclass
    class _TestArticle:
        title: str
        url: str
        published_at: str | None
        summary: str | None

    test_articles = [
        _TestArticle("Python 3.13 news", "https://example.com/py313", None, "what is new"),
        _TestArticle("MCP servers in practice", "https://example.com/mcp", None, None),
    ]
    print("1st insert:", save_articles(conn, source_id, test_articles), "(new rows)")
    print("2nd insert:", save_articles(conn, source_id, test_articles), "(dedup -> 0)")

    print("Articles from the last 7 days:", len(articles_since(conn, days=7)))
    print(
        "Articles per source:",
        [(r["source_name"], r["article_count"]) for r in articles_per_source(conn)],
    )
    print("Search 'python':", len(search_articles(conn, "python")))


if __name__ == "__main__":
    _main()