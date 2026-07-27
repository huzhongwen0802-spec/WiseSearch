# -*- coding: utf-8 -*-

import os
import re
import time
import threading
from functools import lru_cache
from typing import Any

import requests

S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
_LAST_REQUEST_TS = 0.0
_COOLDOWN_UNTIL = 0.0
_RATE_LOCK = threading.Lock()


def _rate_limit() -> None:
    """
    Semantic Scholar 当前 key 限速为 1 request/sec。
    """
    global _LAST_REQUEST_TS
    with _RATE_LOCK:
        now = time.monotonic()
        if now < _COOLDOWN_UNTIL:
            raise RuntimeError(
                f"Semantic Scholar 正处于限流冷却期，约 {max(1, int(_COOLDOWN_UNTIL - now))} 秒后恢复"
            )
        elapsed = now - _LAST_REQUEST_TS
        min_interval = float(os.environ.get("SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS", "1.2"))
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _LAST_REQUEST_TS = time.monotonic()


def _headers() -> dict[str, str]:
    headers = {"User-Agent": "ExpertSearch/0.1"}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _get_s2(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    global _COOLDOWN_UNTIL

    max_attempts = max(1, int(os.environ.get("SEMANTIC_SCHOLAR_RETRY_ATTEMPTS", "2")))
    cooldown_seconds = max(
        10.0,
        float(os.environ.get("SEMANTIC_SCHOLAR_429_COOLDOWN_SECONDS", "60")),
    )
    last_error = None
    for attempt in range(1, max_attempts + 1):
        _rate_limit()
        try:
            response = requests.get(
                f"{S2_BASE_URL}/{endpoint.lstrip('/')}",
                params=params,
                headers=_headers(),
                timeout=30,
            )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "").strip()
                wait_seconds = cooldown_seconds
                if retry_after:
                    try:
                        wait_seconds = max(wait_seconds, float(retry_after))
                    except ValueError:
                        pass
                _COOLDOWN_UNTIL = time.monotonic() + wait_seconds
                last_error = RuntimeError(
                    f"Semantic Scholar 请求触发 429 限流，已进入 {wait_seconds:.0f} 秒冷却"
                )
                break
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(float(os.environ.get("SEMANTIC_SCHOLAR_RETRY_DELAY_SECONDS", "2")))
                continue
            break
    raise RuntimeError(f"Semantic Scholar 请求失败: {last_error}") from last_error


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower())


def _name_without_initials(name: str) -> str:
    tokens = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", _normalize_name(name))
    return " ".join(token for token in tokens if len(token) > 1)


def _author_score(author: dict[str, Any], name: str, query: str = "") -> float:
    display_name = _normalize_name(author.get("name", ""))
    target_name = _normalize_name(name)
    display_simple = _name_without_initials(display_name)
    target_simple = _name_without_initials(target_name)

    score = 0.0
    if display_name == target_name or display_simple == target_simple:
        score += 1000
    elif target_name and target_name in display_name:
        score += 250

    aliases = " ".join(author.get("aliases") or []).lower()
    if target_name and target_name in aliases:
        score += 200

    fields_text = str(author.get("homepage") or "").lower()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", query.lower()):
        if token in fields_text:
            score += 5

    score += float(author.get("hIndex") or 0)
    score += min(float(author.get("citationCount") or 0) / 10000, 50)
    return score


def search_s2_authors(name: str, limit: int = 5) -> list[dict[str, Any]]:
    data = _get_s2(
        "author/search",
        {
            "query": name,
            "limit": limit,
            "fields": "name,url,homepage,paperCount,citationCount,hIndex",
        },
    )
    return data.get("data", [])


def get_s2_author_detail(author_id: str) -> dict[str, Any]:
    return _get_s2(
        f"author/{author_id}",
        {
            "fields": (
                "name,url,homepage,paperCount,citationCount,hIndex,"
                "papers.title,papers.year,papers.citationCount,papers.url,papers.venue"
            )
        },
    )


@lru_cache(maxsize=512)
def get_semantic_scholar_author_metrics(name: str, query: str = "") -> dict[str, Any]:
    authors = search_s2_authors(name)
    if not authors:
        return {}

    best = max(authors, key=lambda author: _author_score(author, name, query))
    author_id = best.get("authorId")
    if author_id:
        try:
            detail = get_s2_author_detail(author_id)
            best.update(detail)
        except Exception:
            pass

    papers = sorted(
        best.get("papers") or [],
        key=lambda paper: paper.get("citationCount") or 0,
        reverse=True,
    )
    top_papers = []
    for paper in papers[:5]:
        title = paper.get("title") or "未知论文"
        year = paper.get("year") or "未知年份"
        citations = paper.get("citationCount") or 0
        venue = paper.get("venue") or "未知来源"
        url = paper.get("url") or ""
        top_papers.append(f"{title}({year}, {venue}, 引用{citations}, {url})")

    return {
        "s2_name": best.get("name"),
        "s2_author_id": best.get("authorId"),
        "s2_url": best.get("url"),
        "homepage": best.get("homepage"),
        "paper_count": best.get("paperCount"),
        "citation_count": best.get("citationCount"),
        "h_index": best.get("hIndex"),
        "top_papers": "；".join(top_papers),
    }


def search_s2_papers(query: str, limit: int = 8) -> list[dict[str, Any]]:
    data = _get_s2(
        "paper/search",
        {
            "query": query,
            "limit": limit,
            "fields": "title,year,venue,url,citationCount,authors.name,authors.authorId",
        },
    )
    return data.get("data", [])


def build_semantic_scholar_context(query: str) -> str:
    try:
        papers = search_s2_papers(query)
    except Exception as e:
        return f"### Semantic Scholar 检索失败\n{e}"

    if not papers:
        return "### Semantic Scholar 高被引论文\n未检索到结果。"

    lines = []
    for index, paper in enumerate(papers, start=1):
        authors = "、".join(
            author.get("name", "")
            for author in paper.get("authors", [])
            if author.get("name")
        )
        lines.append(
            "\n".join(
                [
                    f"[S2-{index}] {paper.get('title', '未知论文')}",
                    f"年份: {paper.get('year', '暂无公开信息')}",
                    f"来源: {paper.get('venue', '暂无公开信息')}",
                    f"引用: {paper.get('citationCount', '暂无公开信息')}",
                    f"链接: {paper.get('url', '暂无公开信息')}",
                    f"作者: {authors or '暂无公开信息'}",
                ]
            )
        )

    return "### Semantic Scholar 高被引论文与作者线索\n" + "\n\n".join(lines)
