# -*- coding: utf-8 -*-

import os
import re
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from langchain_tavily import TavilySearch

from safe_logging import safe_print

load_dotenv()

DIRECT_DEATH_TERMS = [
    "passed away",
    "died",
    "death of",
    "deceased",
    "late professor",
    "逝世",
    "去世",
    "辞世",
    "已故",
    "讣告",
]

TITLE_DEATH_TERMS = DIRECT_DEATH_TERMS + ["obituary", "in memoriam"]

STRONG_DEATH_SOURCE_HINTS = [
    "obituary",
    "memoriam",
    "news",
    "announcement",
    "tribute",
    "academy",
    "university",
    "institute",
    "society",
    "prize",
]


def _simple_name(name: object) -> str:
    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff]+", str(name or "").lower())
    return " ".join(token for token in tokens if len(token) > 1)


def _mentions_name(name: object, text: object) -> bool:
    simple_name = _simple_name(name)
    haystack = str(text or "").lower()
    if not simple_name:
        return False
    if re.search(r"[\u4e00-\u9fff]", simple_name):
        return simple_name.replace(" ", "") in haystack.replace(" ", "")

    tokens = simple_name.split()
    if len(tokens) < 2:
        return simple_name in haystack
    return tokens[0] in haystack and tokens[-1] in haystack


def _has_death_relation(name: object, segment: str, is_title: bool = False) -> bool:
    simple_name = _simple_name(name)
    lowered = str(segment or "").lower()
    if not simple_name or not _mentions_name(name, lowered):
        return False

    if re.search(r"[\u4e00-\u9fff]", simple_name):
        compact_name = re.escape(simple_name.replace(" ", ""))
        compact_text = re.sub(r"\s+", "", lowered)
        return bool(
            re.search(rf"{compact_name}.{{0,40}}(?:逝世|去世|辞世|已故)", compact_text)
            or re.search(rf"(?:讣告|悼念).{{0,40}}{compact_name}", compact_text)
        )

    tokens = simple_name.split()
    first = re.escape(tokens[0])
    last = re.escape(tokens[-1])
    name_pattern = rf"\b{first}(?:\s+[a-z][a-z.-]*){{0,3}}\s+{last}\b"
    after_name = rf"{name_pattern}.{{0,80}}\b(?:passed away|died|is deceased|was deceased)\b"
    before_name = rf"\b(?:death of|obituary for)\b.{{0,40}}{name_pattern}"
    late_name = rf"\bthe late\b.{{0,30}}{name_pattern}"
    if re.search(after_name, lowered) or re.search(before_name, lowered) or re.search(late_name, lowered):
        return True
    if is_title:
        return bool(
            re.search(rf"\b(?:obituary|in memoriam)\b.{{0,50}}{name_pattern}", lowered)
            or re.search(rf"{name_pattern}.{{0,50}}\b(?:obituary|in memoriam)\b", lowered)
        )
    return False


def _death_segments(name: object, text: object, is_title: bool = False) -> list[str]:
    segments = re.split(r"(?<=[。！？.!?;；])\s*", str(text or ""))
    matches = []
    for segment in segments:
        if _has_death_relation(name, segment, is_title=is_title):
            matches.append(" ".join(segment.split())[:320])
    return matches


def _source_domain(url: object) -> str:
    try:
        return urlparse(str(url or "")).netloc.lower()
    except ValueError:
        return ""


def evaluate_survival_evidence(
    name: object,
    items: list[dict[str, str]],
) -> tuple[str, str]:
    """
    仅当网页证据同时明确提及专家姓名和死亡表述时，才确认已故。
    一个标题级强证据或两个相互独立的正文证据可确认已故。
    """
    evidence = []
    for item in items:
        title = str(item.get("title", ""))
        content = str(item.get("content", ""))
        url = str(item.get("url", ""))
        title_matches = _death_segments(name, title, is_title=True)
        body_matches = _death_segments(name, content)
        if not title_matches and not body_matches:
            continue

        domain = _source_domain(url)
        combined = f"{title} {url}".lower()
        strong = bool(title_matches) or any(hint in combined for hint in STRONG_DEATH_SOURCE_HINTS)
        snippet = (title_matches or body_matches)[0]
        evidence.append(
            {
                "url": url,
                "domain": domain,
                "snippet": snippet,
                "strong": strong,
            }
        )

    distinct_domains = {item["domain"] or item["url"] for item in evidence}
    strong_evidence = [item for item in evidence if item["strong"]]
    confirmed = bool(strong_evidence) or len(distinct_domains) >= 2
    if confirmed:
        chosen = (strong_evidence or evidence)[:2]
        detail = "；".join(
            f"{item['snippet']} | {item['url']}" for item in chosen
        )
        return "确认已故", detail

    if evidence:
        detail = "；".join(
            f"单一弱证据待人工核验：{item['snippet']} | {item['url']}"
            for item in evidence[:2]
        )
        return "疑似已故待复核", detail
    return "未发现死亡证据", "独立网页检索未发现同时匹配专家姓名与明确死亡表述的证据"


def _make_tavily() -> TavilySearch | None:
    if not os.environ.get("TAVILY_API_KEY"):
        return None
    return TavilySearch(
        max_results=int(os.environ.get("SURVIVAL_VERIFICATION_TAVILY_RESULTS", "5")),
        search_depth="advanced",
        topic="general",
        include_answer=False,
        handle_tool_error=True,
    )


def _search_items(tavily: TavilySearch, query: str) -> list[dict[str, str]]:
    raw_result = tavily.invoke({"query": query})
    if not isinstance(raw_result, dict):
        return []
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("content") or item.get("raw_content") or ""),
        }
        for item in raw_result.get("results", [])
    ]


def verify_survival_status(
    df: pd.DataFrame,
    query: str = "",
    *,
    exhaustive: bool = False,
) -> pd.DataFrame:
    """
    独立于 LLM 的生存状态核验层。

    已有独立核验结果会被复用，避免最终总表合并时重复调用 Tavily。
    """
    if df.empty or "专家姓名" not in df.columns:
        return df

    work_df = df.copy()
    for column, default in [
        ("生存状态", "待核验"),
        ("独立生存状态核验", ""),
        ("生存状态核验依据", ""),
    ]:
        if column not in work_df.columns:
            work_df[column] = default

    tavily = _make_tavily()
    max_rows = (
        len(work_df)
        if exhaustive
        else int(os.environ.get("SURVIVAL_VERIFICATION_MAX_ROWS", "25"))
    )
    if tavily is None or max_rows <= 0:
        missing = work_df["独立生存状态核验"].fillna("").astype(str).str.strip().eq("")
        work_df.loc[missing, "独立生存状态核验"] = "未执行"
        work_df.loc[missing, "生存状态核验依据"] = "未配置 Tavily 或独立核验上限为 0"
        return work_df

    existing = work_df["独立生存状态核验"].fillna("").astype(str).str.strip()
    candidate_indices = work_df.index[
        existing.isin({"", "未执行", "核验失败"})
    ].tolist()
    candidate_indices.sort(
        key=lambda index: 0
        if re.search(
            r"已故|去世|逝世|deceased|died|passed away|obituary",
            str(work_df.at[index, "生存状态"]),
            flags=re.IGNORECASE,
        )
        else 1
    )

    checked = 0
    confirmed_deceased = 0
    suspected_deceased = 0
    failed = 0
    for index in candidate_indices:
        if checked >= max_rows:
            work_df.at[index, "独立生存状态核验"] = "未执行"
            work_df.at[index, "生存状态核验依据"] = "超过本批独立核验数量上限"
            continue

        name = str(work_df.at[index, "专家姓名"]).strip()
        institution = str(work_df.at[index, "工作单位"] if "工作单位" in work_df.columns else "").strip()
        if not name:
            work_df.at[index, "独立生存状态核验"] = "未执行"
            work_df.at[index, "生存状态核验依据"] = "缺少专家姓名"
            continue

        search_query = (
            f'"{name}" "{institution}" obituary OR "in memoriam" OR died OR '
            f'"passed away" OR 逝世 OR 去世 OR 讣告'
        )
        try:
            items = _search_items(tavily, search_query)
            status, evidence = evaluate_survival_evidence(name, items)
        except Exception as exc:
            status = "核验失败"
            evidence = f"独立网页检索失败: {exc}"
            safe_print(f"[生存状态核验警告] {name} | {exc}")

        work_df.at[index, "独立生存状态核验"] = status
        work_df.at[index, "生存状态核验依据"] = evidence
        if status == "确认已故":
            work_df.at[index, "生存状态"] = "已故"
            confirmed_deceased += 1
        elif status == "疑似已故待复核":
            suspected_deceased += 1
        elif status == "核验失败":
            failed += 1
        elif status == "未发现死亡证据" and str(work_df.at[index, "生存状态"]).strip() == "已故":
            work_df.at[index, "生存状态"] = "待核验"
        checked += 1

    safe_print(
        f"[生存状态核验汇总] 本轮核验 {checked} 位，"
        f"确认已故 {confirmed_deceased} 位，疑似待复核 {suspected_deceased} 位，"
        f"核验失败 {failed} 位。"
    )
    return work_df
