# -*- coding: utf-8 -*-

import os
import re
import html
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from langchain_tavily import TavilySearch

from openalex_client import get_openalex_author_metrics, get_openalex_china_collaboration_evidence
from semantic_scholar_client import get_semantic_scholar_author_metrics
from safe_logging import safe_print
from opencli_homepage_client import read_homepage_with_opencli, reset_opencli_call_budget

load_dotenv()

PLACEHOLDER_TERMS = ("暂无", "未知", "公开信息", "nan", "None")
ENRICHMENT_PRIORITY_FIELDS = [
    "个人主页",
    "邮箱/电话",
    "研究兴趣",
    "工作单位",
    "职位",
    "工作经历",
    "教育背景",
    "主要成果",
    "国内合作学者与单位",
    "入选依据",
    "H指数",
    "i10指数",
    "总被引次数",
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s\])}>\"'，。；;]+")
EDUCATION_RE = re.compile(
    r"[^。；;\n]*(?:博士|硕士|本科|毕业|学位|Ph\.?D|DPhil|MD|B\.?S\.?|M\.?S\.?|"
    r"education|biography|received|earned|obtained)[^。；;\n]*",
    re.IGNORECASE,
)
POSITION_RE = re.compile(
    r"[^。；;\n]{0,80}(?:Professor|Associate Professor|Assistant Professor|"
    r"Director|Chair|Principal Investigator|PI|Research Scientist|Group Leader|"
    r"教授|副教授|助理教授|研究员|首席科学家|主任|负责人|课题组长)[^。；;\n]{0,80}",
    re.IGNORECASE,
)
INSTITUTION_RE = re.compile(
    r"[^。；;\n]{0,80}(?:University|Institute|Laboratory|Lab|College|School|"
    r"Center|Centre|Observatory|Hospital|大学|学院|研究所|实验室|中心|天文台|医院)[^。；;\n]{0,80}",
    re.IGNORECASE,
)
RESEARCH_RE = re.compile(
    r"[^。；;\n]{0,60}(?:research interests?|research focuses?|research areas?|"
    r"研究兴趣|研究方向|主要研究|课题组研究|focuses on|works on|interested in)"
    r"[^。；;\n]{0,220}",
    re.IGNORECASE,
)
ACHIEVEMENT_RE = re.compile(
    r"[^。；;\n]{0,80}(?:award|honou?r|fellow|member of|academy|prize|medal|"
    r"discovered|developed|led|published|citation|获奖|院士|会士|奖|发现|提出|"
    r"领导|发表|引用)[^。；;\n]{0,180}",
    re.IGNORECASE,
)
WORK_HISTORY_RE = re.compile(
    r"[^。；;\n]{0,80}(?:previously|formerly|served as|worked at|joined|appointed|"
    r"曾任|历任|任职于|加入|受聘于|长期在)[^。；;\n]{0,180}",
    re.IGNORECASE,
)

TITLE_KEYWORDS = [
    "院士",
    "Academician",
    "National Academy",
    "Royal Society",
    "IEEE Fellow",
    "ACM Fellow",
    "AAAI Fellow",
    "Turing Award",
    "图灵奖",
    "Nobel",
    "诺贝尔",
    "Fields Medal",
    "菲尔兹",
    "CIGR Fellow",
    "CIGR member",
    "CIGR会员",
    "World Food Prize",
    "世界粮食奖",
]

CN_COLLAB_TERMS = [
    "中国", "清华", "北京大学", "复旦", "上海交通", "浙江大学", "中国科学院",
    "Chinese", "China", "Tsinghua", "Peking University", "Fudan",
    "Shanghai Jiao Tong", "Zhejiang University", "Chinese Academy",
    "collaboration", "coauthor", "co-author", "合作", "合著",
]

BAD_HOMEPAGE_DOMAINS = [
    "openalex.org",
    "semanticscholar.org",
    "scholar.google",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "wikipedia.org",
]

HOMEPAGE_URL_HINTS = [
    "profile",
    "profiles",
    "people",
    "person",
    "persons",
    "staff",
    "faculty",
    "directory",
    "biography",
    "bio",
    "team",
    "our-people",
    "our-team",
]

WEAK_HOMEPAGE_URL_HINTS = [
    "search",
    "publication",
    "publications",
    "paper",
    "papers",
    "news",
    "event",
    "events",
    "project",
    "projects",
]

HOMEPAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 ExpertSearch/0.1"
    )
}


def _is_placeholder(value: Any) -> bool:
    text = str(value).strip()
    if not text:
        return True
    return any(term.lower() in text.lower() for term in PLACEHOLDER_TERMS)


def _has_valid_email(value: Any) -> bool:
    return bool(EMAIL_RE.search(str(value)))


def _has_valid_homepage(value: Any) -> bool:
    text = str(value).strip()
    if not text.startswith(("http://", "https://")):
        return False
    return not any(domain in text.lower() for domain in BAD_HOMEPAGE_DOMAINS)


def _append_information_source(row_data: dict[str, Any], url: Any) -> None:
    clean_url = str(url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return
    current = str(row_data.get("信息来源", "") or "")
    urls = re.findall(r"https?://[^\s；;,，)）]+", current)
    if clean_url not in urls:
        urls.append(clean_url)
    row_data["信息来源"] = "；".join(dict.fromkeys(url.rstrip("。.") for url in urls))


def _missing_enrichment_fields(row_data: dict[str, Any]) -> list[str]:
    missing = []
    for field in ENRICHMENT_PRIORITY_FIELDS:
        value = row_data.get(field)
        if field == "个人主页":
            is_missing = not _has_valid_homepage(value)
        elif field == "邮箱/电话":
            is_missing = not _has_valid_email(value)
        elif field in {"H指数", "i10指数", "总被引次数"}:
            is_missing = _parse_metric_number(value) <= 0
        else:
            is_missing = _is_placeholder(value)
        if is_missing:
            missing.append(field)
    return missing


def _enrichment_gap_score(row_data: dict[str, Any]) -> int:
    weights = {
        "个人主页": 4,
        "工作单位": 5,
        "职位": 4,
        "主要成果": 4,
        "国内合作学者与单位": 4,
        "邮箱/电话": 3,
        "工作经历": 3,
        "教育背景": 3,
        "入选依据": 3,
        "研究兴趣": 2,
        "H指数": 2,
        "i10指数": 2,
        "总被引次数": 2,
    }
    return sum(weights.get(field, 1) for field in _missing_enrichment_fields(row_data))


def _needs_homepage_details(row_data: dict[str, Any]) -> bool:
    return any(
        field in _missing_enrichment_fields(row_data)
        for field in [
            "邮箱/电话",
            "研究兴趣",
            "工作单位",
            "职位",
            "工作经历",
            "教育背景",
            "主要成果",
            "国内合作学者与单位",
            "入选依据",
        ]
    )


def _parse_metric_number(value: Any) -> float:
    if pd.isna(value):
        return 0.0
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else 0.0


def _estimate_existing_citations(row_data: dict[str, Any]) -> float:
    for column in ["总被引次数", "总引用数", "Total Citations", "Citations", "引用次数", "被引次数"]:
        value = _parse_metric_number(row_data.get(column))
        if value > 0:
            return value
    return 0.0


def _metric_match_trusted(row_data: dict[str, Any], metrics: dict[str, Any]) -> bool:
    current_h = _parse_metric_number(row_data.get("H指数"))
    current_citations = _estimate_existing_citations(row_data)
    metric_h = _parse_metric_number(metrics.get("h_index"))
    metric_citations = _parse_metric_number(
        metrics.get("cited_by_count")
        if "cited_by_count" in metrics
        else metrics.get("citation_count")
    )

    if current_h >= 20 and metric_h > 0 and metric_h < current_h * 0.65:
        return False
    if current_h >= 20 and metric_citations > 0 and metric_citations < max(100, current_h * 30):
        return False
    if current_h >= 10 and 0 < metric_h <= 5 and metric_citations < 500:
        return False
    if current_citations >= 1000 and 0 < metric_citations < current_citations * 0.2:
        return False
    if current_h == 0 and current_citations == 0 and 0 < metric_h <= 2 and metric_citations < 100:
        return False

    return True


def _simple_name(name: str) -> str:
    tokens = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", str(name).lower())
    return " ".join(token for token in tokens if len(token) > 1)


def _same_author(target: str, matched: Any) -> bool:
    target_simple = _simple_name(target)
    matched_simple = _simple_name(str(matched))
    if not target_simple or not matched_simple:
        return False
    return target_simple == matched_simple or target_simple in matched_simple or matched_simple in target_simple


def _text_mentions_author(name: str, text: str) -> bool:
    simple = _simple_name(name)
    haystack = str(text).lower()
    if not simple:
        return False

    if re.search(r"[\u4e00-\u9fff]", simple):
        return simple.replace(" ", "") in haystack.replace(" ", "")

    tokens = [token for token in simple.split() if len(token) > 1]
    if len(tokens) <= 1:
        return simple in haystack

    return tokens[0] in haystack and tokens[-1] in haystack


def _name_variants(name: str, institution: str, query: str) -> list[str]:
    variants = [name]
    simple = _simple_name(name)
    if simple and simple != name.lower():
        variants.append(simple)

    institution_text = str(institution).replace("；", " ").replace(";", " ")
    institution_tokens = institution_text.split()
    if institution_tokens:
        variants.append(f"{name} {institution_tokens[0]}")

    query_tokens = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", query)[:4])
    if query_tokens:
        variants.append(f"{name} {query_tokens}")

    return list(dict.fromkeys(variant.strip() for variant in variants if variant.strip()))


def _make_tavily() -> TavilySearch | None:
    if not os.environ.get("TAVILY_API_KEY"):
        return None
    return TavilySearch(
        max_results=int(os.environ.get("EXPERT_ENRICHMENT_TAVILY_RESULTS", "5")),
        search_depth=os.environ.get(
            "EXPERT_ENRICHMENT_TAVILY_SEARCH_DEPTH", "advanced"
        ),
        topic="general",
        include_answer=False,
        handle_tool_error=True,
    )


def _format_result_items(raw_result: Any) -> list[dict[str, str]]:
    if not isinstance(raw_result, dict):
        return []

    items = []
    for item in raw_result.get("results", []):
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        content = str(item.get("content") or item.get("raw_content") or "")
        items.append({"title": title, "url": url, "content": content})
    return items


def _search(tavily: TavilySearch | None, query: str) -> list[dict[str, str]]:
    if tavily is None:
        return []
    try:
        return _format_result_items(tavily.invoke({"query": query}))
    except Exception as exc:
        safe_print(f"[专家补全警告] Tavily 定向补查失败: {query} | {exc}")
        return []


def _html_to_text(page_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", page_html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_homepage_item(row_data: dict[str, Any]) -> list[dict[str, str]]:
    url = str(row_data.get("个人主页", "")).strip()
    name = str(row_data.get("专家姓名", "")).strip()
    if not _has_valid_homepage(url):
        row_data["主页访问方式"] = "未尝试"
        row_data["主页访问失败原因"] = "缺少有效个人主页 URL"
        return []

    try:
        response = requests.get(
            url,
            headers=HOMEPAGE_HEADERS,
            timeout=float(os.environ.get("EXPERT_ENRICHMENT_HOMEPAGE_TIMEOUT_SECONDS", "12")),
        )
        response.raise_for_status()
    except Exception as exc:
        safe_print(f"[专家补全警告] 个人主页访问失败: {name} | {url} | {exc}")
        row_data["主页访问失败原因"] = f"HTTP访问失败: {exc}"
        return _fetch_homepage_item_with_opencli(row_data, url, name)

    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        row_data["主页访问失败原因"] = f"HTTP返回不支持的内容类型: {content_type or '未知'}"
        return _fetch_homepage_item_with_opencli(row_data, url, name)

    page_text = _html_to_text(response.text)
    if not page_text:
        row_data["主页访问失败原因"] = "HTTP页面未提取到有效正文"
        return _fetch_homepage_item_with_opencli(row_data, url, name)
    if not _text_mentions_author(name, page_text[:4000]):
        row_data["主页访问失败原因"] = "HTTP页面正文未匹配专家姓名"
        return _fetch_homepage_item_with_opencli(row_data, url, name)

    row_data["主页访问方式"] = "HTTP"
    row_data["主页访问失败原因"] = ""
    return [{"title": f"{name} official homepage", "url": url, "content": page_text[:10000]}]


def _fetch_homepage_item_with_opencli(
    row_data: dict[str, Any],
    url: str,
    name: str,
) -> list[dict[str, str]]:
    result = read_homepage_with_opencli(url)
    if not result.ok:
        row_data["主页访问方式"] = "HTTP失败，OpenCLI未成功"
        row_data["主页访问失败原因"] = result.error or row_data.get("主页访问失败原因", "")
        return []

    page_text = re.sub(r"\s+", " ", result.content).strip()
    if not page_text:
        row_data["主页访问方式"] = "OpenCLI"
        row_data["主页访问失败原因"] = "OpenCLI未提取到有效正文"
        return []
    if not _text_mentions_author(name, page_text[:6000]):
        row_data["主页访问方式"] = "OpenCLI"
        row_data["主页访问失败原因"] = "OpenCLI页面正文未匹配专家姓名"
        return []

    row_data["主页访问方式"] = "OpenCLI"
    row_data["主页访问失败原因"] = ""
    return [{"title": f"{name} browser-rendered homepage", "url": url, "content": page_text[:16000]}]


def _candidate_homepage_urls(items: list[dict[str, str]]) -> list[tuple[str, dict[str, str]]]:
    candidates: list[tuple[str, dict[str, str]]] = []
    for item in items:
        direct_url = item["url"].strip()
        if _has_valid_homepage(direct_url):
            candidates.append((direct_url, item))

        urls = URL_RE.findall(f"{item['title']} {item['content']}")
        for url in urls:
            if _has_valid_homepage(url):
                candidates.append((url, item))

    seen = set()
    deduped = []
    for url, item in candidates:
        key = url.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            deduped.append((url, item))
    return deduped


def _homepage_score(url: str, item: dict[str, str], expert_name: str = "", institution: str = "") -> int:
    text = f"{item.get('title', '')} {item.get('content', '')} {url}"
    lowered_url = url.lower()
    lowered_text = text.lower()
    score = 0

    if expert_name and _text_mentions_author(expert_name, text[:3000]):
        score += 8
    name_tokens = [token for token in _simple_name(expert_name).split() if len(token) > 1]
    if name_tokens and any(token in lowered_url for token in name_tokens):
        score += 4

    institution_tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", str(institution))
        if len(token) >= 4
    ][:4]
    if institution_tokens and any(token in lowered_text for token in institution_tokens):
        score += 2

    if any(hint in lowered_url for hint in HOMEPAGE_URL_HINTS):
        score += 3
    if any(hint in lowered_url for hint in WEAK_HOMEPAGE_URL_HINTS):
        score -= 2

    if re.search(r"\.(edu|ac\.[a-z]{2}|edu\.[a-z]{2}|org|gov)(/|$)", lowered_url):
        score += 1
    if lowered_url.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx")):
        score -= 6

    return score


def _best_homepage(
    items: list[dict[str, str]],
    expert_name: str = "",
    institution: str = "",
    min_score: int = 2,
) -> str:
    scored = [
        (_homepage_score(url, item, expert_name, institution), url)
        for url, item in _candidate_homepage_urls(items)
    ]
    scored = [(score, url) for score, url in scored if score >= min_score]
    if not scored:
        return ""

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _best_email(items: list[dict[str, str]]) -> str:
    for item in items:
        text = f"{item['title']} {item['content']} {item['url']}"
        match = EMAIL_RE.search(text)
        if match:
            return match.group(0)
    return ""


def _best_education(items: list[dict[str, str]]) -> str:
    candidates = []
    for item in items:
        text = re.sub(r"\s+", " ", f"{item['title']}。{item['content']}")
        label_match = re.search(
            r"(?:Education|教育背景|学历)\s*[:：-]\s*(.{6,160}?)(?=\.\s+[A-Z][a-z]+\s+(?:received|won|was awarded|led|developed)|\s+(?:Research|Email|Phone|Contact|Awards?)\s*[:：-]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if label_match:
            candidates.append(label_match.group(1).strip(" 。；;"))
        for match in EDUCATION_RE.findall(text):
            cleaned = match.strip(" 。；;")
            if 10 <= len(cleaned) <= 180:
                candidates.append(cleaned)

    if candidates:
        return candidates[0]
    return ""


def _best_regex_candidate(items: list[dict[str, str]], pattern: re.Pattern, max_len: int = 180) -> str:
    candidates = []
    for item in items:
        text = re.sub(r"\s+", " ", f"{item['title']}。{item['content']}")
        for segment in _text_segments(text):
            match = pattern.search(segment)
            if match:
                cleaned = str(match.group(0)).strip(" 。；;:-")
                if 8 <= len(cleaned) <= max_len and not _looks_like_navigation(cleaned):
                    candidates.append(cleaned)
    return candidates[0] if candidates else ""


def _text_segments(text: str) -> list[str]:
    segments = re.split(r"(?<=[。；;.!?])\s+", text)
    return [segment.strip() for segment in segments if segment.strip()]


def _looks_like_navigation(text: str) -> bool:
    lowered = text.lower()
    nav_terms = ["cookie", "privacy", "login", "menu", "copyright", "skip to", "search", "navigation"]
    if any(term in lowered for term in nav_terms):
        return True
    if len(re.findall(r"\|", text)) >= 2:
        return True
    return False


def _best_position(items: list[dict[str, str]]) -> str:
    return _best_regex_candidate(items, POSITION_RE, max_len=150)


def _best_institution(items: list[dict[str, str]]) -> str:
    return _best_regex_candidate(items, INSTITUTION_RE, max_len=150)


def _best_research_interests(items: list[dict[str, str]]) -> str:
    for item in items:
        text = re.sub(r"\s+", " ", f"{item['title']}。{item['content']}")
        label_match = re.search(
            r"(?:research interests?|research focuses?|research areas?|研究兴趣|研究方向|主要研究)\s*[:：-]\s*(.{8,220}?)(?=\s+(?:Education|Email|Phone|Contact|Awards?)\s*[:：-]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if label_match:
            return label_match.group(1).strip(" 。；;")

    candidate = _best_regex_candidate(items, RESEARCH_RE, max_len=220)
    return re.sub(
        r"(?i)^(research interests?|research focuses?|research areas?|研究兴趣|研究方向|主要研究)\s*[:：-]?\s*",
        "",
        candidate,
    ).strip() if candidate else ""


def _best_work_history(items: list[dict[str, str]]) -> str:
    candidates = []
    for item in items:
        text = re.sub(r"\s+", " ", f"{item['title']}。{item['content']}")
        label_match = re.search(
            r"(?:Biography|Career|Professional Experience|工作经历|个人履历|职业经历)"
            r"\s*[:：-]\s*(.{12,260}?)(?=\s+(?:Education|Research|Email|Phone|Contact|Awards?)\s*[:：-]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if label_match:
            candidates.append(label_match.group(1).strip(" 。；;"))
        candidate = _best_regex_candidate([item], WORK_HISTORY_RE, max_len=240)
        if candidate:
            candidates.append(candidate)
    return "；".join(dict.fromkeys(candidates[:2]))


def _homepage_achievements(items: list[dict[str, str]], expert_name: str) -> str:
    candidates = []
    for item in items:
        text = f"{item['title']} {item['content']}"
        if expert_name and not _text_mentions_author(expert_name, text[:5000]):
            continue
        for segment in _text_segments(re.sub(r"\s+", " ", text)):
            match = ACHIEVEMENT_RE.search(segment)
            if match:
                cleaned = str(match.group(0)).strip(" 。；;:-")
                if 12 <= len(cleaned) <= 220 and not _looks_like_navigation(cleaned):
                    candidates.append(cleaned)
    return "；".join(dict.fromkeys(candidates[:2]))


def _title_evidence(items: list[dict[str, str]], expert_name: str = "") -> str:
    hits = []
    for item in items:
        text = f"{item['title']} {item['content']}"
        if expert_name and not _text_mentions_author(expert_name, text):
            continue
        for keyword in TITLE_KEYWORDS:
            if keyword.lower() in text.lower():
                hits.append(keyword)
    hits = list(dict.fromkeys(hits))
    return "、".join(hits)


def _collaboration_evidence(items: list[dict[str, str]]) -> str:
    for item in items:
        text = re.sub(r"\s+", " ", f"{item['title']} {item['content']}")
        if any(term.lower() in text.lower() for term in CN_COLLAB_TERMS):
            source = item["url"] or item["title"]
            snippet = text[:120].strip()
            return f"中国合作线索(合作依据: {snippet}；来源: {source})"
    return ""


def _needs_collaboration_evidence(value: Any) -> bool:
    text = str(value or "").strip()
    if _is_placeholder(text):
        return True

    generic_markers = [
        "具体个人需逐篇核实",
        "未确认",
        "待核实",
        "中国作者",
        "中国团队",
        "等单位",
        "等中国",
        "合作网络",
        "相关活动",
        "相关论文",
        "国际合著",
    ]
    if any(marker in text for marker in generic_markers):
        return True

    return False


def _apply_openalex_china_collaboration(row_data: dict[str, Any], query: str) -> bool:
    if not _needs_collaboration_evidence(row_data.get("国内合作学者与单位")):
        return False

    name = str(row_data.get("专家姓名", "")).strip()
    if not name or _is_placeholder(name):
        return False

    try:
        evidence = get_openalex_china_collaboration_evidence(
            name=name,
            openalex_id=str(row_data.get("OpenAlex作者ID", "")).strip(),
            query=query,
            max_items=int(os.environ.get("OPENALEX_COLLAB_MAX_ITEMS", "5")),
        )
    except Exception as exc:
        safe_print(f"[专家补全警告] OpenAlex 国内合作证据提取失败: {name} | {exc}")
        return False

    if not evidence:
        return False

    row_data["国内合作学者与单位"] = evidence
    return True


def _second_pass_openalex(row_data: dict[str, Any], query: str) -> dict[str, Any]:
    name = str(row_data.get("专家姓名", "")).strip()
    if not name or _is_placeholder(name):
        return {}

    best = {}
    max_variants = int(os.environ.get("OPENALEX_SECOND_PASS_VARIANTS", "3"))
    variants = _name_variants(name, str(row_data.get("工作单位", "")), query)[:max_variants]
    for variant in variants:
        try:
            metrics = get_openalex_author_metrics(variant, query)
        except Exception as exc:
            safe_print(f"[专家补全警告] OpenAlex 二次匹配失败: {variant} | {exc}")
            continue
        if metrics and _same_author(name, metrics.get("openalex_name", "")):
            if not best or _parse_metric_number(metrics.get("cited_by_count")) > _parse_metric_number(best.get("cited_by_count")):
                best = metrics
    return best


def _second_pass_s2(row_data: dict[str, Any], query: str) -> dict[str, Any]:
    name = str(row_data.get("专家姓名", "")).strip()
    if not name or _is_placeholder(name):
        return {}

    best = {}
    for variant in _name_variants(name, str(row_data.get("工作单位", "")), query):
        try:
            metrics = get_semantic_scholar_author_metrics(variant, query)
        except Exception as exc:
            safe_print(f"[专家补全警告] Semantic Scholar 二次匹配失败: {variant} | {exc}")
            continue
        if metrics and _same_author(name, metrics.get("s2_name", "")):
            if not best or _parse_metric_number(metrics.get("citation_count")) > _parse_metric_number(best.get("citation_count")):
                best = metrics
    return best


def _apply_openalex_metrics(row_data: dict[str, Any], metrics: dict[str, Any]) -> None:
    if not metrics:
        return
    if not _metric_match_trusted(row_data, metrics):
        return

    if _parse_metric_number(metrics.get("h_index")) > _parse_metric_number(row_data.get("H指数")):
        row_data["H指数"] = metrics.get("h_index")
    if _parse_metric_number(metrics.get("i10_index")) > _parse_metric_number(row_data.get("i10指数")):
        row_data["i10指数"] = metrics.get("i10_index")
    if _parse_metric_number(metrics.get("cited_by_count")) > _parse_metric_number(row_data.get("总被引次数")):
        row_data["总被引次数"] = metrics.get("cited_by_count")

    row_data["OpenAlex匹配姓名"] = metrics.get("openalex_name", "")
    row_data["OpenAlex作者ID"] = metrics.get("openalex_id", "")
    row_data["OpenAlex主题"] = metrics.get("topics", "")
    _append_information_source(row_data, metrics.get("openalex_id"))


def _apply_s2_metrics(row_data: dict[str, Any], metrics: dict[str, Any]) -> None:
    if not metrics:
        return
    if not _metric_match_trusted(row_data, metrics):
        return

    if _parse_metric_number(metrics.get("h_index")) > _parse_metric_number(row_data.get("H指数")):
        row_data["H指数"] = metrics.get("h_index")
    if _parse_metric_number(metrics.get("citation_count")) > _parse_metric_number(row_data.get("总被引次数")):
        row_data["总被引次数"] = metrics.get("citation_count")

    homepage = metrics.get("homepage")
    if homepage and not _has_valid_homepage(row_data.get("个人主页")):
        row_data["个人主页"] = homepage

    row_data["S2匹配姓名"] = metrics.get("s2_name", "")
    row_data["S2作者ID"] = metrics.get("s2_author_id", "")
    row_data["S2主页"] = metrics.get("s2_url", "")
    row_data["S2代表论文"] = metrics.get("top_papers", "")
    _append_information_source(row_data, metrics.get("s2_url"))


def _needs_tavily(row_data: dict[str, Any]) -> bool:
    return (
        not _has_valid_homepage(row_data.get("个人主页"))
        or not _has_valid_email(row_data.get("邮箱/电话"))
        or _is_placeholder(row_data.get("工作经历"))
        or _is_placeholder(row_data.get("教育背景"))
        or _is_placeholder(row_data.get("国内合作学者与单位"))
        or _is_placeholder(row_data.get("评价_顶级头衔标识"))
    )


def _apply_homepage_details(row_data: dict[str, Any]) -> bool:
    name = str(row_data.get("专家姓名", "")).strip()
    homepage_items = _fetch_homepage_item(row_data)
    if not homepage_items:
        return False
    _append_information_source(row_data, row_data.get("个人主页"))

    if not _has_valid_email(row_data.get("邮箱/电话")):
        email = _best_email(homepage_items)
        if email:
            row_data["邮箱/电话"] = email

    if _is_placeholder(row_data.get("研究兴趣")):
        research_interests = _best_research_interests(homepage_items)
        if research_interests:
            row_data["研究兴趣"] = research_interests

    if _is_placeholder(row_data.get("工作单位")):
        institution = _best_institution(homepage_items)
        if institution:
            row_data["工作单位"] = institution

    if _is_placeholder(row_data.get("职位")):
        position = _best_position(homepage_items)
        if position:
            row_data["职位"] = position

    if _is_placeholder(row_data.get("教育背景")):
        education = _best_education(homepage_items)
        if education:
            row_data["教育背景"] = education

    if _is_placeholder(row_data.get("工作经历")):
        work_history = _best_work_history(homepage_items)
        if work_history:
            row_data["工作经历"] = work_history

    if _is_placeholder(row_data.get("国内合作学者与单位")):
        collaboration = _collaboration_evidence(homepage_items)
        if collaboration:
            row_data["国内合作学者与单位"] = collaboration

    titles = _title_evidence(homepage_items, name)
    if titles and _is_placeholder(row_data.get("入选依据")):
        row_data["入选依据"] = f"因具有可验证的权威头衔、奖项或行业组织身份入选：{titles}"
    if titles and titles not in str(row_data.get("主要成果", "")):
        current = str(row_data.get("主要成果", "")).strip()
        row_data["主要成果"] = f"{current}；个人主页头衔/荣誉证据：{titles}" if current else f"个人主页头衔/荣誉证据：{titles}"

    achievements = _homepage_achievements(homepage_items, name)
    if achievements and achievements not in str(row_data.get("主要成果", "")):
        current = str(row_data.get("主要成果", "")).strip()
        row_data["主要成果"] = f"{current}；个人主页成果线索：{achievements}" if current and not _is_placeholder(current) else f"个人主页成果线索：{achievements}"

    return True


def _retry_homepage_with_tavily(row_data: dict[str, Any], query: str, tavily: TavilySearch | None) -> bool:
    if tavily is None:
        return False

    name = str(row_data.get("专家姓名", "")).strip()
    institution = str(row_data.get("工作单位", "")).strip()
    if not name or _is_placeholder(name):
        return False

    items = _search(tavily, f'"{name}" "{institution}" official homepage profile biography')
    homepage = _best_homepage(items, expert_name=name, institution=institution)
    if not homepage:
        items = _search(tavily, f'"{name}" "{institution}" faculty profile contact email')
        homepage = _best_homepage(items, expert_name=name, institution=institution)

    current_homepage = str(row_data.get("个人主页", "")).strip()
    if not homepage or homepage == current_homepage:
        return False

    row_data["个人主页"] = homepage
    return _apply_homepage_details(row_data)


def _apply_tavily_details(row_data: dict[str, Any], query: str, tavily: TavilySearch | None) -> bool:
    name = str(row_data.get("专家姓名", "")).strip()
    institution = str(row_data.get("工作单位", "")).strip()
    research = str(row_data.get("研究兴趣", "")).strip()
    if _is_placeholder(institution):
        institution = ""
    if _is_placeholder(research):
        research = ""
    if not name or _is_placeholder(name):
        return False

    base = f'"{name}" "{institution}" {research}'.strip()
    compact_search = os.environ.get(
        "EXPERT_ENRICHMENT_COMPACT_TAVILY", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if compact_search:
        profile_items = _search(
            tavily,
            (
                f'{base} official homepage email biography education PhD '
                "IEEE Fellow ACM Fellow AAAI Fellow National Academy award "
                "China collaboration Chinese university"
            ),
        )
        email_items = profile_items
        education_items = profile_items
        title_items = profile_items
        collab_items = profile_items
    else:
        profile_items = _search(tavily, f'{base} official homepage email biography education PhD CV')

        email_items = []
        if not _has_valid_email(row_data.get("邮箱/电话")):
            email_items = _search(tavily, f'"{name}" "{institution}" email contact')

        education_items = []
        if _is_placeholder(row_data.get("教育背景")):
            education_items = _search(tavily, f'"{name}" "{institution}" education biography PhD CV degree')

        title_items = []
        if _is_placeholder(row_data.get("入选依据")):
            title_items = _search(
                tavily,
                f'{base} IEEE Fellow ACM Fellow AAAI Fellow National Academy award honors',
            )

        collab_items = []
        if _needs_collaboration_evidence(row_data.get("国内合作学者与单位")):
            collab_items = _search(
                tavily,
                f'{base} China collaboration Chinese coauthor university project',
            )

    all_items = profile_items + email_items + education_items + title_items + collab_items
    for item in all_items:
        _append_information_source(row_data, item.get("url"))

    if not _has_valid_homepage(row_data.get("个人主页")):
        homepage = _best_homepage(profile_items + email_items + education_items, expert_name=name, institution=institution)
        if homepage:
            row_data["个人主页"] = homepage

    if not _has_valid_email(row_data.get("邮箱/电话")):
        email = _best_email(email_items + profile_items)
        if email:
            row_data["邮箱/电话"] = email

    if _is_placeholder(row_data.get("教育背景")):
        education = _best_education(education_items + profile_items)
        if education:
            row_data["教育背景"] = education

    if _is_placeholder(row_data.get("研究兴趣")):
        research_interests = _best_research_interests(profile_items)
        if research_interests:
            row_data["研究兴趣"] = research_interests

    if _is_placeholder(row_data.get("工作单位")):
        matched_institution = _best_institution(profile_items)
        if matched_institution:
            row_data["工作单位"] = matched_institution

    if _is_placeholder(row_data.get("职位")):
        position = _best_position(profile_items)
        if position:
            row_data["职位"] = position

    if _is_placeholder(row_data.get("工作经历")):
        work_history = _best_work_history(profile_items + education_items)
        if work_history:
            row_data["工作经历"] = work_history

    if _is_placeholder(row_data.get("国内合作学者与单位")):
        collaboration = _collaboration_evidence(collab_items)
        if collaboration:
            row_data["国内合作学者与单位"] = collaboration

    titles = _title_evidence(title_items, name)
    if titles and _is_placeholder(row_data.get("入选依据")):
        row_data["入选依据"] = f"因具有可验证的权威头衔、奖项或行业组织身份入选：{titles}"
    if titles and titles not in str(row_data.get("主要成果", "")):
        current = str(row_data.get("主要成果", "")).strip()
        row_data["主要成果"] = f"{current}；头衔/荣誉证据：{titles}" if current else f"头衔/荣誉证据：{titles}"

    achievements = _homepage_achievements(profile_items + title_items, name)
    if achievements and _is_placeholder(row_data.get("主要成果")):
        row_data["主要成果"] = f"公开网页成果线索：{achievements}"

    return bool(all_items)


def enrich_expert_details(
    df: pd.DataFrame,
    query: str = "",
    *,
    exhaustive: bool = False,
) -> pd.DataFrame:
    """
    对候选专家做保守的二次补全：
    1. 对 OpenAlex / Semantic Scholar 未命中的专家做姓名变体重试；
    2. 优先访问已检索到的专家个人主页，抽取邮箱、研究兴趣、机构、职位、教育、成果、国内合作、头衔线索；
    3. 对主页仍补不齐的前 N 位专家做 Tavily 定向补查。

    默认上限：
    - OpenAlex 二次匹配 4 人，可通过 EXPERT_ENRICHMENT_MAX_OPENALEX_RETRY_ROWS 调整；
    - OpenAlex 国内合作证据补全 20 人，可通过 EXPERT_ENRICHMENT_MAX_COLLAB_ROWS 调整；
    - Semantic Scholar 二次匹配默认关闭，可通过 EXPERT_ENRICHMENT_MAX_S2_RETRY_ROWS 调整；
    - 个人主页访问 30 人，可通过 EXPERT_ENRICHMENT_MAX_HOMEPAGE_ROWS 调整，设为 0 可关闭；
    - 主页失败后的 Tavily 纠错重试 12 人，可通过 EXPERT_ENRICHMENT_MAX_HOMEPAGE_RETRY_ROWS 调整；
    - Tavily 定向补查 8 人，可通过 EXPERT_ENRICHMENT_MAX_TAVILY_ROWS 调整，设为 0 可关闭。
    """
    if df.empty or "专家姓名" not in df.columns:
        return df

    # OpenCLI 的调用上限用于约束单批补全，而不是耗尽后让整个 Streamlit
    # 生命周期中的后续批次都失去浏览器回退能力。
    reset_opencli_call_budget()

    if exhaustive:
        # 最终定向补全接收所有关键字段仍有空白的专家，对传入行按实际缺口
        # 分流调用 OpenAlex、S2、主页和 Tavily，不再按总人数截断。
        target_rows = len(df)
        current_opencli_limit = int(os.environ.get("OPENCLI_HOMEPAGE_MAX_CALLS_PER_PROCESS", "25"))
        os.environ["OPENCLI_HOMEPAGE_MAX_CALLS_PER_PROCESS"] = str(
            max(current_opencli_limit, target_rows)
        )
        max_openalex_retry_rows = target_rows
        max_collab_rows = target_rows
        max_s2_retry_rows = min(
            target_rows,
            max(0, int(os.environ.get("FINAL_ENRICHMENT_MAX_S2_ROWS", "20"))),
        )
        max_homepage_rows = target_rows
        max_homepage_retry_rows = target_rows
        max_tavily_rows = target_rows
    else:
        max_openalex_retry_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_OPENALEX_RETRY_ROWS", "4"))
        max_collab_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_COLLAB_ROWS", "20"))
        max_s2_retry_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_S2_RETRY_ROWS", "0"))
        max_homepage_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_HOMEPAGE_ROWS", "30"))
        max_homepage_retry_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_HOMEPAGE_RETRY_ROWS", "12"))
        max_tavily_rows = int(os.environ.get("EXPERT_ENRICHMENT_MAX_TAVILY_ROWS", "8"))
    tavily = _make_tavily() if max_tavily_rows > 0 else None
    if max_tavily_rows > 0 and tavily is None:
        safe_print("[专家补全工具状态] Tavily 未配置或不可用，本轮跳过网页定向补查。")
        max_tavily_rows = 0
    openalex_retry_used = 0
    collab_used = 0
    s2_retry_used = 0
    homepage_used = 0
    homepage_retry_used = 0
    tavily_used = 0
    enriched_rows = []
    stats = {
        "openalex_attempted": 0,
        "openalex_improved": 0,
        "collab_attempted": 0,
        "collab_improved": 0,
        "s2_attempted": 0,
        "s2_improved": 0,
        "homepage_attempted": 0,
        "homepage_success": 0,
        "homepage_retry_attempted": 0,
        "homepage_retry_success": 0,
        "tavily_attempted": 0,
        "tavily_improved": 0,
    }
    work_df = df.copy()
    work_df["_enrichment_order"] = range(len(work_df))
    work_df["_enrichment_priority"] = work_df.apply(
        lambda row: _enrichment_gap_score(row.to_dict()),
        axis=1,
    )
    work_df = work_df.sort_values("_enrichment_priority", ascending=False)
    initial_missing = sum(
        len(_missing_enrichment_fields(row.to_dict()))
        for _, row in work_df.iterrows()
    )

    for _, row in work_df.iterrows():
        row_data = row.to_dict()
        original_order = int(row_data.pop("_enrichment_order"))
        row_data.pop("_enrichment_priority", None)
        row_data["_enrichment_order"] = original_order
        row_data.setdefault("成功访问主页", "未尝试")
        row_data.setdefault("主页访问方式", "未尝试")
        row_data.setdefault("主页访问失败原因", "")

        if openalex_retry_used < max_openalex_retry_rows and (
            _is_placeholder(row_data.get("OpenAlex作者ID"))
            or _parse_metric_number(row_data.get("总被引次数")) <= 0
        ):
            before = _enrichment_gap_score(row_data)
            _apply_openalex_metrics(row_data, _second_pass_openalex(row_data, query))
            openalex_retry_used += 1
            stats["openalex_attempted"] += 1
            stats["openalex_improved"] += int(_enrichment_gap_score(row_data) < before)

        if collab_used < max_collab_rows and _needs_collaboration_evidence(row_data.get("国内合作学者与单位")):
            before = _enrichment_gap_score(row_data)
            collab_used += 1
            _apply_openalex_china_collaboration(row_data, query)
            stats["collab_attempted"] += 1
            stats["collab_improved"] += int(_enrichment_gap_score(row_data) < before)

        if s2_retry_used < max_s2_retry_rows and (
            _parse_metric_number(row_data.get("H指数")) <= 0
            or _parse_metric_number(row_data.get("总被引次数")) <= 0
            or not _has_valid_homepage(row_data.get("个人主页"))
        ):
            before = _enrichment_gap_score(row_data)
            _apply_s2_metrics(row_data, _second_pass_s2(row_data, query))
            s2_retry_used += 1
            stats["s2_attempted"] += 1
            stats["s2_improved"] += int(_enrichment_gap_score(row_data) < before)

        if (
            homepage_used < max_homepage_rows
            and _has_valid_homepage(row_data.get("个人主页"))
            and (
                _needs_homepage_details(row_data)
                or str(row_data.get("成功访问主页", "")).strip() != "是"
            )
        ):
            homepage_used += 1
            stats["homepage_attempted"] += 1
            if _apply_homepage_details(row_data):
                row_data["成功访问主页"] = "是"
                stats["homepage_success"] += 1
            elif homepage_retry_used < max_homepage_retry_rows:
                homepage_retry_used += 1
                stats["homepage_retry_attempted"] += 1
                retry_success = _retry_homepage_with_tavily(row_data, query, tavily)
                row_data["成功访问主页"] = "是" if retry_success else "否"
                stats["homepage_retry_success"] += int(retry_success)
            else:
                row_data["成功访问主页"] = "否"

        if tavily_used < max_tavily_rows and _needs_tavily(row_data):
            before = _enrichment_gap_score(row_data)
            _apply_tavily_details(row_data, query, tavily)
            tavily_used += 1
            stats["tavily_attempted"] += 1
            stats["tavily_improved"] += int(_enrichment_gap_score(row_data) < before)
            if (
                row_data.get("成功访问主页") != "是"
                and homepage_used < max_homepage_rows
                and _has_valid_homepage(row_data.get("个人主页"))
            ):
                homepage_used += 1
                row_data["成功访问主页"] = "是" if _apply_homepage_details(row_data) else "否"

        enriched_rows.append(row_data)

    result = pd.DataFrame(enriched_rows).sort_values("_enrichment_order")
    result = result.drop(columns=["_enrichment_order"], errors="ignore").reset_index(drop=True)
    remaining_missing = sum(
        len(_missing_enrichment_fields(row.to_dict()))
        for _, row in result.iterrows()
    )
    mode = "最终定向" if exhaustive else "批次"
    homepage_methods = (
        result.get("主页访问方式", pd.Series(dtype=str))
        .fillna("未记录")
        .astype(str)
        .value_counts()
        .to_dict()
    )
    safe_print(
        f"[专家补全汇总] 模式={mode}；输入专家={len(df)}；"
        f"关键空白字段={initial_missing}->{remaining_missing}；"
        f"OpenAlex={stats['openalex_attempted']}次/改善{stats['openalex_improved']}人；"
        f"OpenAlex合作={stats['collab_attempted']}次/改善{stats['collab_improved']}人；"
        f"SemanticScholar={stats['s2_attempted']}次/改善{stats['s2_improved']}人；"
        f"主页访问={stats['homepage_attempted']}次/成功{stats['homepage_success']}人；"
        f"主页纠错={stats['homepage_retry_attempted']}次/成功{stats['homepage_retry_success']}人；"
        f"Tavily={stats['tavily_attempted']}人/改善{stats['tavily_improved']}人；"
        f"主页访问方式={homepage_methods}。"
    )
    return result
