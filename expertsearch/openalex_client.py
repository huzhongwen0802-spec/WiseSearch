# -*- coding: utf-8 -*-

import os
import re
import threading
import time
from functools import lru_cache
from typing import Any

import requests
from requests import exceptions as requests_exceptions

from .safe_logging import safe_print

OPENALEX_BASE_URL = "https://api.openalex.org"
GREATER_CHINA_COUNTRY_CODES = {"CN", "HK", "MO", "TW"}
OPENALEX_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

_SESSION = requests.Session()
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_COOLDOWN_UNTIL = 0.0
_FAILURE_STREAK = 0


def _clip_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _api_params(params: dict[str, Any]) -> dict[str, Any]:
    merged = dict(params)
    api_key = os.environ.get("OPENALEX_API_KEY")
    mailto = os.environ.get("OPENALEX_MAILTO")

    if api_key:
        merged["api_key"] = api_key
    if mailto:
        merged["mailto"] = mailto

    return merged


def _openalex_timeout() -> float:
    return float(os.environ.get("OPENALEX_TIMEOUT_SECONDS", "25"))


def _openalex_min_interval() -> float:
    return float(os.environ.get("OPENALEX_MIN_INTERVAL_SECONDS", "1.5"))


def _openalex_max_retries() -> int:
    return int(os.environ.get("OPENALEX_MAX_RETRIES", "3"))


def _openalex_backoff_seconds(attempt: int) -> float:
    base = float(os.environ.get("OPENALEX_RETRY_BACKOFF_SECONDS", "2"))
    return min(base * (2 ** max(attempt - 1, 0)), 20)


def _openalex_failure_streak_limit() -> int:
    return int(os.environ.get("OPENALEX_FAILURE_STREAK_LIMIT", "8"))


def _openalex_cooldown_seconds() -> float:
    return float(os.environ.get("OPENALEX_COOLDOWN_SECONDS", "60"))


def _sanitize_error_text(error: Any) -> str:
    text = str(error)
    text = re.sub(r"([?&]api_key=)[^&\s)]+", r"\1***", text)
    text = re.sub(r"([?&]mailto=)[^&\s)]+", r"\1***", text)
    return text


def _params_cache_key(params: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in params.items()))


def _wait_for_openalex_slot() -> None:
    global _LAST_REQUEST_AT

    while True:
        with _RATE_LIMIT_LOCK:
            now = time.monotonic()
            if now < _COOLDOWN_UNTIL:
                wait = _COOLDOWN_UNTIL - now
                raise RuntimeError(f"OpenAlex 暂停请求 {wait:.0f} 秒：连续连接失败或限流。")

            wait = _LAST_REQUEST_AT + _openalex_min_interval() - now
            if wait <= 0:
                _LAST_REQUEST_AT = now
                return

        time.sleep(wait)


def _record_openalex_success() -> None:
    global _FAILURE_STREAK
    with _RATE_LIMIT_LOCK:
        _FAILURE_STREAK = 0


def _record_openalex_failure() -> None:
    global _FAILURE_STREAK, _COOLDOWN_UNTIL
    with _RATE_LIMIT_LOCK:
        _FAILURE_STREAK += 1
        if _FAILURE_STREAK >= _openalex_failure_streak_limit():
            _COOLDOWN_UNTIL = time.monotonic() + _openalex_cooldown_seconds()
            safe_print(
                f"[OpenAlex限流保护] 连续失败 {_FAILURE_STREAK} 次，"
                f"暂停 {_openalex_cooldown_seconds():.0f} 秒。"
            )


def _retry_after_seconds(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60)
            except ValueError:
                pass
    return _openalex_backoff_seconds(attempt)


def _is_transient_openalex_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            requests_exceptions.Timeout,
            requests_exceptions.ConnectionError,
            requests_exceptions.SSLError,
        ),
    ):
        return True

    if isinstance(error, requests_exceptions.HTTPError):
        response = getattr(error, "response", None)
        return response is not None and response.status_code in OPENALEX_TRANSIENT_STATUS_CODES

    return False


@lru_cache(maxsize=2048)
def _cached_get_openalex(endpoint: str, params_key: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    return _request_openalex(endpoint, dict(params_key))


def _request_openalex(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"{OPENALEX_BASE_URL}/{endpoint.lstrip('/')}"
    response = None
    max_retries = _openalex_max_retries()

    for attempt in range(1, max_retries + 1):
        try:
            _wait_for_openalex_slot()
            response = _SESSION.get(
                url,
                params=params,
                timeout=_openalex_timeout(),
                headers={"User-Agent": "ExpertSearch/0.1"},
            )
            response.raise_for_status()
            _record_openalex_success()
            return response.json()
        except Exception as error:
            if not _is_transient_openalex_error(error):
                raise RuntimeError(_sanitize_error_text(error)) from error

            if attempt < max_retries:
                delay = _retry_after_seconds(response, attempt)
                safe_print(
                    f"[OpenAlex重试] {endpoint} 第 {attempt}/{max_retries} 次失败，"
                    f"{delay:.1f} 秒后重试: {_sanitize_error_text(error)}"
                )
                time.sleep(delay)
                continue

            _record_openalex_failure()
            raise RuntimeError(_sanitize_error_text(error)) from error

    raise RuntimeError(f"OpenAlex 请求失败: {endpoint}")


def _get_openalex(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    merged_params = _api_params(params)
    return _cached_get_openalex(endpoint, _params_cache_key(merged_params))


def search_openalex_authors(query: str, per_page: int = 12) -> list[dict[str, Any]]:
    data = _get_openalex(
        "authors",
        {
            "search": query,
            "sort": "cited_by_count:desc",
            "per_page": per_page,
        },
    )
    return data.get("results", [])


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower())


def _name_without_initials(name: str) -> str:
    tokens = re.findall(r"[a-zA-Z\u4e00-\u9fff]+", _normalize_name(name))
    return " ".join(token for token in tokens if len(token) > 1)


def _author_score(author: dict[str, Any], name: str, query: str | None = None) -> float:
    display_name = _normalize_name(author.get("display_name", ""))
    target_name = _normalize_name(name)
    score = 0.0

    display_simple = _name_without_initials(display_name)
    target_simple = _name_without_initials(target_name)

    if display_name == target_name or display_simple == target_simple:
        score += 1000
    elif target_name and target_name in display_name:
        score += 300

    if query:
        topic_text = " ".join(
            topic.get("display_name", "")
            for topic in author.get("topics", [])
            if topic.get("display_name")
        ).lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", query.lower()):
            if token in topic_text:
                score += 10

    stats = author.get("summary_stats") or {}
    score += float(stats.get("h_index") or 0)
    score += min(float(author.get("cited_by_count") or 0) / 10000, 50)
    return score


@lru_cache(maxsize=512)
def get_openalex_author_metrics(name: str, query: str = "") -> dict[str, Any]:
    """
    按专家姓名从 OpenAlex 回填结构化学术指标。
    """
    authors = search_openalex_authors(name, per_page=8)
    if not authors:
        return {}

    best = max(authors, key=lambda author: _author_score(author, name, query))
    stats = best.get("summary_stats") or {}
    institution = _institution_label(best.get("last_known_institution"))
    return {
        "openalex_name": best.get("display_name"),
        "openalex_id": best.get("id"),
        "institution": institution,
        "works_count": best.get("works_count"),
        "cited_by_count": best.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "topics": _topic_names(best),
    }


def search_openalex_works(query: str, per_page: int = 12) -> list[dict[str, Any]]:
    query_variants = [f'"{query}"', query]
    for query_variant in query_variants:
        data = _get_openalex(
            "works",
            {
                "filter": f"title_and_abstract.search:{query_variant}",
                "sort": "cited_by_count:desc",
                "per_page": per_page,
            },
        )
        results = data.get("results", [])
        if results:
            return results

    return []


def search_openalex_works_by_author(author_id: str, per_page: int = 40) -> list[dict[str, Any]]:
    short_id = str(author_id or "").rstrip("/").split("/")[-1]
    if not short_id:
        return []

    data = _get_openalex(
        "works",
        {
            "filter": f"author.id:{short_id}",
            "sort": "cited_by_count:desc",
            "per_page": per_page,
        },
    )
    return data.get("results", [])


def _cn_authors_from_work(work: dict[str, Any], target_author_id: str = "") -> list[str]:
    target_short_id = str(target_author_id or "").rstrip("/").split("/")[-1]
    collaborators = []

    for authorship in work.get("authorships", []):
        author = authorship.get("author") or {}
        author_id = str(author.get("id") or "").rstrip("/").split("/")[-1]
        if target_short_id and author_id == target_short_id:
            continue

        cn_institutions = [
            _institution_label(institution)
            for institution in authorship.get("institutions", [])
            if institution.get("country_code") == "CN"
        ]
        if not cn_institutions:
            continue

        author_name = author.get("display_name") or "未知中国合作者"
        collaborators.append(f"{author_name}（{'、'.join(dict.fromkeys(cn_institutions[:2]))}）")

    return list(dict.fromkeys(collaborators))


@lru_cache(maxsize=512)
def get_openalex_china_collaboration_evidence(
    name: str,
    openalex_id: str = "",
    query: str = "",
    max_items: int = 3,
) -> str:
    """
    Return concrete China-based coauthor evidence for one expert.

    The output is intentionally compact for the Excel cell:
    Collaborator(Institution); evidence: Paper (year, OpenAlex)
    """
    metrics = {}
    author_id = str(openalex_id or "").strip()
    if not author_id:
        metrics = get_openalex_author_metrics(name, query)
        author_id = str(metrics.get("openalex_id") or "").strip()
    if not author_id:
        return ""

    works = search_openalex_works_by_author(
        author_id,
        per_page=int(os.environ.get("OPENALEX_COLLAB_WORKS_PER_AUTHOR", "40")),
    )
    evidence_items = []
    for work in works:
        cn_collaborators = _cn_authors_from_work(work, author_id)
        if not cn_collaborators:
            continue

        title = _clip_text(work.get("display_name", "未知论文"), 120)
        year = work.get("publication_year") or "未知年份"
        cited_by = work.get("cited_by_count") or 0
        evidence_items.append(
            f"{'、'.join(cn_collaborators[:3])}；合作依据：《{title}》（{year}，OpenAlex，引用{cited_by}次）"
        )
        if len(evidence_items) >= max_items:
            break

    return "；".join(evidence_items)


def _cn_authors_from_work(work: dict[str, Any], target_author_id: str = "") -> list[dict[str, Any]]:
    target_short_id = str(target_author_id or "").rstrip("/").split("/")[-1]
    collaborators = []

    for index, authorship in enumerate(work.get("authorships", []), start=1):
        author = authorship.get("author") or {}
        author_id = str(author.get("id") or "").rstrip("/").split("/")[-1]
        if target_short_id and author_id == target_short_id:
            continue

        cn_institutions = [
            _institution_label(institution)
            for institution in authorship.get("institutions", [])
            if institution.get("country_code") == "CN"
        ]
        if not cn_institutions:
            continue

        author_name = author.get("display_name") or "未知中国合作者"
        collaborators.append(
            {
                "label": f"{author_name}（{'、'.join(dict.fromkeys(cn_institutions[:3]))}）",
                "position": authorship.get("author_position") or "",
                "author_index": index,
            }
        )

    deduped = []
    seen = set()
    for collaborator in collaborators:
        if collaborator["label"] in seen:
            continue
        seen.add(collaborator["label"])
        deduped.append(collaborator)
    return deduped


def _target_author_position(work: dict[str, Any], target_author_id: str = "") -> str:
    target_short_id = str(target_author_id or "").rstrip("/").split("/")[-1]
    if not target_short_id:
        return ""

    for authorship in work.get("authorships", []):
        author = authorship.get("author") or {}
        author_id = str(author.get("id") or "").rstrip("/").split("/")[-1]
        if author_id == target_short_id:
            return authorship.get("author_position") or ""
    return ""


def _collaboration_strength(work: dict[str, Any], collaborators: list[dict[str, Any]], target_author_id: str) -> str:
    target_position = _target_author_position(work, target_author_id)
    collaborator_positions = {item.get("position") for item in collaborators}
    cited_by = int(work.get("cited_by_count") or 0)
    cn_count = len(collaborators)

    if target_position in {"first", "last"} and collaborator_positions.intersection({"first", "last"}):
        return "强合作"
    if cn_count >= 2 and cited_by >= 300:
        return "强合作"
    if cited_by >= 100 or cn_count >= 2:
        return "中合作"
    return "弱合作"


@lru_cache(maxsize=512)
def get_openalex_china_collaboration_evidence(
    name: str,
    openalex_id: str = "",
    query: str = "",
    max_items: int = 5,
) -> str:
    metrics = {}
    author_id = str(openalex_id or "").strip()
    if not author_id:
        metrics = get_openalex_author_metrics(name, query)
        author_id = str(metrics.get("openalex_id") or "").strip()
    if not author_id:
        return ""

    works = search_openalex_works_by_author(
        author_id,
        per_page=int(os.environ.get("OPENALEX_COLLAB_WORKS_PER_AUTHOR", "40")),
    )
    evidence_items = []
    max_collaborators = int(os.environ.get("OPENALEX_COLLAB_MAX_COLLABORATORS_PER_WORK", "5"))
    for work in works:
        cn_collaborators = _cn_authors_from_work(work, author_id)
        if not cn_collaborators:
            continue

        title = _clip_text(work.get("display_name", "未知论文"), 120)
        year = work.get("publication_year") or "未知年份"
        cited_by = work.get("cited_by_count") or 0
        strength = _collaboration_strength(work, cn_collaborators, author_id)
        collaborator_labels = [item["label"] for item in cn_collaborators[:max_collaborators]]
        evidence_items.append(
            f"{strength}：{'、'.join(collaborator_labels)}；合作依据：《{title}》（{year}，OpenAlex，引用{cited_by}次）"
        )
        if len(evidence_items) >= max_items:
            break

    return "；".join(evidence_items)


def _authors_from_works(works: list[dict[str, Any]], limit: int = 16) -> list[dict[str, Any]]:
    authors_by_id = {}
    for work in works:
        work_citations = work.get("cited_by_count") or 0
        for authorship in work.get("authorships", []):
            author = authorship.get("author") or {}
            author_id = author.get("id")
            if not author_id or author_id in authors_by_id:
                continue

            institutions = authorship.get("institutions", [])
            country_codes = {
                institution.get("country_code")
                for institution in institutions
                if institution.get("country_code")
            }
            if country_codes and country_codes.issubset(GREATER_CHINA_COUNTRY_CODES):
                continue
            authors_by_id[author_id] = {
                "id": author_id,
                "display_name": author.get("display_name", "未知作者"),
                "last_known_institution": institutions[0] if institutions else None,
                "works_count": "待作者详情补充",
                "cited_by_count": work_citations,
                "summary_stats": {},
                "topics": work.get("topics") or [],
                "evidence_work": work.get("display_name", "未知作品"),
            }

            if len(authors_by_id) >= limit:
                return list(authors_by_id.values())

    return list(authors_by_id.values())


def _get_author_detail(author_id: str) -> dict[str, Any]:
    short_id = author_id.rstrip("/").split("/")[-1]
    return _get_openalex(f"authors/{short_id}", {})


def _enrich_authors(authors: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    enriched = []
    for author in authors[:limit]:
        try:
            detail = _get_author_detail(author["id"])
            detail["evidence_work"] = author.get("evidence_work")
            enriched.append(detail)
        except Exception:
            enriched.append(author)

    return enriched


def _institution_label(institution: dict[str, Any] | None) -> str:
    if not institution:
        return "暂无公开信息"

    name = institution.get("display_name") or "暂无公开信息"
    country = institution.get("country_code")
    return f"{name}({country})" if country else name


def _topic_names(entity: dict[str, Any], limit: int = 4) -> str:
    topics = entity.get("topics") or []
    names = [topic.get("display_name") for topic in topics if topic.get("display_name")]
    return "、".join(names[:limit]) if names else "暂无公开信息"


def _format_authors(authors: list[dict[str, Any]]) -> str:
    if not authors:
        return "未从 OpenAlex 作者库检索到候选专家。"

    lines = []
    max_authors = int(os.environ.get("OPENALEX_CONTEXT_AUTHORS", "6"))
    for index, author in enumerate(authors[:max_authors], start=1):
        stats = author.get("summary_stats") or {}
        institution = _institution_label(author.get("last_known_institution"))
        lines.append(
            "\n".join(
                [
                    f"[A{index}] {_clip_text(author.get('display_name', '未知作者'), 80)}",
                    f"OpenAlex: {author.get('id', '暂无公开信息')}",
                    f"机构: {_clip_text(institution, 120)}",
                    f"作品数: {author.get('works_count', '暂无公开信息')}",
                    f"总引用: {author.get('cited_by_count', '暂无公开信息')}",
                    f"H指数: {stats.get('h_index', '暂无公开信息')}",
                    f"i10指数: {stats.get('i10_index', '暂无公开信息')}",
                    f"主题: {_clip_text(_topic_names(author), 160)}",
                    f"证据论文: {_clip_text(author.get('evidence_work', '暂无公开信息'), 180)}",
                ]
            )
        )

    return "\n\n".join(lines)


def _cn_collaborators(work: dict[str, Any]) -> str:
    collaborators = []
    for authorship in work.get("authorships", []):
        author_name = (authorship.get("author") or {}).get("display_name")
        for institution in authorship.get("institutions", []):
            if institution.get("country_code") == "CN":
                label = _institution_label(institution)
                if author_name:
                    collaborators.append(f"{author_name}({label})")
                else:
                    collaborators.append(label)

    return "；".join(dict.fromkeys(collaborators)) or "暂无公开信息"


def _work_authors(work: dict[str, Any], limit: int = 8) -> str:
    names = []
    for authorship in work.get("authorships", []):
        name = (authorship.get("author") or {}).get("display_name")
        if name:
            names.append(name)

    return "、".join(names[:limit]) if names else "暂无公开信息"


def _format_works(works: list[dict[str, Any]]) -> str:
    if not works:
        return "未从 OpenAlex 作品库检索到高被引论文。"

    lines = []
    max_works = int(os.environ.get("OPENALEX_CONTEXT_WORKS", "6"))
    for index, work in enumerate(works[:max_works], start=1):
        source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
        doi = work.get("doi") or "暂无公开信息"
        lines.append(
            "\n".join(
                [
                    f"[W{index}] {_clip_text(work.get('display_name', '未知作品'), 180)}",
                    f"年份: {work.get('publication_year', '暂无公开信息')}",
                    f"引用: {work.get('cited_by_count', '暂无公开信息')}",
                    f"来源: {_clip_text(source or '暂无公开信息', 100)}",
                    f"DOI: {doi}",
                    f"作者: {_clip_text(_work_authors(work, limit=6), 160)}",
                    f"中国合作线索: {_clip_text(_cn_collaborators(work), 180)}",
                ]
            )
        )

    return "\n\n".join(lines)


def build_openalex_context(query: str) -> str:
    errors = []
    blocks = []
    works = []

    try:
        works = search_openalex_works(query)
        authors = _enrich_authors(_authors_from_works(works))
        blocks.append("### OpenAlex 高被引论文反推作者候选\n" + _format_authors(authors))
    except Exception as e:
        errors.append(f"高被引论文反推作者失败: {e}")

    try:
        if not works:
            works = search_openalex_works(query)
        blocks.append("### OpenAlex 高被引作品与合作线索\n" + _format_works(works))
    except Exception as e:
        errors.append(f"作品检索失败: {e}")

    if blocks:
        if errors:
            blocks.append("### OpenAlex 检索警告\n" + "\n".join(errors))
        return "\n\n".join(blocks)

    return "### OpenAlex 检索失败\n" + "\n".join(errors)
