"""LLM pipeline: classify, rank, digest and job-posting recommendations.

Two phases on the Anthropic API: a cheap model (Haiku) classifies/ranks the
articles in one batched call, a stronger model (Opus) writes the digest and
the profile-based 岗位推荐. Everything degrades gracefully: `available()`
returns False without ANTHROPIC_API_KEY and pipeline.py falls back to the
plain list report.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

# Load ANTHROPIC_API_KEY (and any other config) from .env in the repo root,
# so it doesn't have to be exported in the shell - see .env.example.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Interests used in the classification prompt. Edit prompts/interests.md,
# no code change needed.
INTERESTS = (Path(__file__).resolve().parent.parent / "prompts" / "interests.md").read_text()

MODEL_CLASSIFY = "claude-haiku-4-5"
MODEL_SUMMARIZE = "claude-opus-5"


class ArticleClassification(BaseModel):
    relevant: bool
    topic: str
    interest_score: int  # 1-5


def available() -> bool:
    """True when an API key is configured - everything else degrades to None."""
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _ask(client: anthropic.Anthropic, model: str, prompt: str,
         max_tokens: int = 4000) -> str:
    response = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _row_text(row) -> str:
    parts = [row["title"], row["source_name"], row["published_at"] or "", row["url"]]
    if row["summary"]:
        parts.append(row["summary"][:200])
    return " | ".join(parts)


def classify_article(client: anthropic.Anthropic, title: str,
                     summary: str | None) -> ArticleClassification:
    """Classify one article against INTERESTS: relevance, topic, score 1-5."""
    prompt = (f"兴趣档案：\n{INTERESTS}\n\n文章标题：{title}\n"
              f"摘要：{summary or '（无）'}\n\n"
              '只输出 JSON：{"relevant": true/false, "topic": "主题", '
              '"interest_score": 1-5}')
    data = json.loads(_extract_json(_ask(client, MODEL_CLASSIFY, prompt, 200)))
    return ArticleClassification(**data)


def rank_articles(client: anthropic.Anthropic, articles: list) -> list[tuple]:
    """Classify all articles in ONE batched call, drop irrelevant ones and
    return the rest as (article, classification) pairs by score, descending."""
    if not articles:
        return []
    listing = "\n".join(f"{i}. {_row_text(row)}" for i, row in enumerate(articles))
    prompt = (f"兴趣档案：\n{INTERESTS}\n\n对下列每篇文章判断是否与西安公考备考相关，"
              f"并给出主题和 1-5 的价值分（5 最高）：\n{listing}\n\n"
              '只输出 JSON 数组，每项形如 {"i": 序号, "relevant": true/false, '
              '"topic": "主题", "score": 1-5}，覆盖全部文章。')
    items = json.loads(_extract_json(_ask(client, MODEL_CLASSIFY, prompt, 8000)))
    by_index = {item["i"]: item for item in items}
    ranked = []
    for i, row in enumerate(articles):
        item = by_index.get(i)
        if not item or not item.get("relevant"):
            continue
        classification = ArticleClassification(
            relevant=True, topic=item.get("topic", ""),
            interest_score=int(item.get("score", 3)))
        ranked.append((row, classification))
    ranked.sort(key=lambda pair: pair[1].interest_score, reverse=True)
    return ranked


def summarize_article(client: anthropic.Anthropic, title: str,
                      summary: str | None, url: str) -> str:
    """Write a short (2-4 sentences) digest summary of one article."""
    prompt = (f"你是陕西公务员考试分析专家。用2-4句中文概括这篇文章及其对公考备考的价值：\n"
              f"标题：{title}\n摘要：{summary or '（无）'}\n链接：{url}\n只输出正文。")
    return _ask(client, MODEL_CLASSIFY, prompt, 600).strip()


def _extract_json(text: str) -> str:
    """Strip markdown fences / surrounding prose from a JSON reply."""
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    return match.group(1) if match else text


def _profile_text(profile: dict) -> str:
    return (f"学历：{profile.get('degree', '')}；学校：{profile.get('school', '')}；"
            f"专业：{profile.get('major', '')}；技能：{profile.get('skills', '')}；"
            f"备注：{profile.get('notes', '')}")


def recommend_positions(client: anthropic.Anthropic, profile: dict,
                        exam_rows: list) -> str:
    """Profile-based job recommendation markdown for the 报考-category rows."""
    if not exam_rows:
        return "本轮无报考类条目，暂无岗位推荐。"
    listing = "\n".join(f"- {_row_text(row)}" for row in exam_rows)
    prompt = f"""你是一名陕西公务员考试分析专家。考生档案：{_profile_text(profile)}

以下是近期西安招考公告。请结合档案专业（建筑学常见对口方向：住建、自然资源、规划、城管、
工程造价、不限专业岗等）判断每条公告中考生可能可报的岗位。

{listing}

输出 Markdown，每条格式：
- **单位/岗位** —— 匹配度：高/中/低
  理由：一句话；需注意的报考条件（专业目录、学历、户籍、证书、报名截止时间）。
只输出 Markdown，不要额外解释。"""
    return _ask(client, MODEL_SUMMARIZE, prompt, 4000).strip()


def build_digest(articles: list, profile: dict, top_n: int = 12) -> str:
    """Compose the full markdown report: ranked summary + job recommendations.

    Returns None when no API key is configured (pipeline falls back)."""
    if not available():
        return None
    client = _client()
    ranked = rank_articles(client, articles)
    if not ranked:
        return None
    top = [row for row, _ in ranked[:top_n]]
    listing = "\n".join(f"- {_row_text(row)}" for row in top)
    prompt = f"""你是一名陕西公务员考试分析专家，为一名备考西安公务员/事业单位的考生写今日情报日报。
考生档案：{_profile_text(profile)}

以下是近期信息（标题 | 来源 | 日期 | 链接 | 摘要）：
{listing}

输出 Markdown 日报，结构固定：
# 西安公考情报速递
（日期行自己按今天日期写）
## 一、报考速览（核心）
逐条列出招考类公告：招考单位、报名时间/截止时间（如原文有）、岗位要点、原文链接、重要提醒。
## 二、政策动向
摘要 + 对备考的意义。
## 三、申论素材
主题 / 事件 / 申论应用 / 金句。
## 四、面试热点
预测问题 + 答题框架（1/2/3）。
只输出 Markdown。"""
    report = _ask(client, MODEL_SUMMARIZE, prompt, 8000).strip()
    exam_rows = [row for row in top if (row["category"] or "") == "报考"]
    try:
        recommendation = recommend_positions(client, profile, exam_rows)
    except Exception as e:
        recommendation = f"（岗位推荐生成失败：{e}）"
    return (f"{report}\n\n## 岗位推荐（基于我的档案）\n\n{recommendation}\n")


def _main() -> None:
    if not available():
        print("No ANTHROPIC_API_KEY - degraded mode, nothing to test.")
        return
    import sqlite3
    import storage
    conn = storage.init_db()
    articles = storage.articles_since(conn, days=30)
    print(build_digest(articles, storage.get_profile(conn)) or "(no relevant articles)")


if __name__ == "__main__":
    _main()
