# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import hashlib
import copy
import json
import math
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from langchain_tavily import TavilySearch

from .safe_logging import safe_print


PROJECT_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_LOGGED_CONFIGURATIONS: set[tuple[str, str]] = set()
_BUDGET_LOCK = threading.RLock()
_BUDGET_LIMIT_CREDITS: int | None = None
_BUDGET_USED_CREDITS = 0
_BUDGET_REQUESTS = 0
_BUDGET_USAGE_PATH: Path | None = None
_CONSECUTIVE_FAILURES = 0
_CIRCUIT_OPENED_AT = 0.0
_RESULT_CACHE: dict[tuple[Any, ...], Any] = {}


class TavilySoftFailure(RuntimeError):
    """A Tavily-only failure that callers should degrade around."""


class TavilyBudgetExceeded(TavilySoftFailure):
    """The current task has consumed its configured Tavily credit budget."""


def estimate_tavily_credit_plan(
    target_experts: int,
    *,
    experts_per_candidate_batch: int = 10,
) -> dict[str, int]:
    """Estimate the normal-path credits while keeping retries outside the estimate."""
    target = max(1, int(target_experts or 1))
    batch_size = max(1, int(experts_per_candidate_batch or 1))
    candidate_batches = int(math.ceil(target / batch_size))
    # The researcher runs two advanced searches per candidate batch. Enrichment
    # and independent survival verification each run one advanced search/person.
    candidate_requests = candidate_batches * 2
    enrichment_requests = target
    survival_requests = target
    candidate_credits = candidate_requests * 2
    enrichment_credits = enrichment_requests * 2
    survival_credits = survival_requests * 2
    return {
        "target_experts": target,
        "candidate_batches": candidate_batches,
        "candidate_requests": candidate_requests,
        "enrichment_requests": enrichment_requests,
        "survival_requests": survival_requests,
        "expected_requests": candidate_requests + enrichment_requests + survival_requests,
        "candidate_credits": candidate_credits,
        "enrichment_credits": enrichment_credits,
        "survival_credits": survival_credits,
        "expected_credits": candidate_credits + enrichment_credits + survival_credits,
    }


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _project_tavily_values() -> dict[str, str]:
    configured_path = str(os.environ.get("EXPERTSEARCH_DOTENV_PATH", "") or "").strip()
    env_path = Path(configured_path) if configured_path else PROJECT_ENV_PATH
    if not env_path.is_file():
        return {}
    try:
        values = dotenv_values(env_path, encoding="utf-8-sig")
    except Exception as exc:
        safe_print(f"[Tavily配置警告] 无法读取项目环境文件: {exc}")
        return {}
    return {
        str(key): str(value).strip()
        for key, value in values.items()
        if value is not None and str(value).strip()
    }


def current_tavily_configuration() -> tuple[str, str | None, str]:
    """Resolve the current key without retaining a stale module-level credential."""
    project_values = _project_tavily_values()
    process_key = str(os.environ.get("TAVILY_API_KEY", "") or "").strip()
    project_key = str(project_values.get("TAVILY_API_KEY", "") or "").strip()
    prefer_process = _truthy(os.environ.get("EXPERTSEARCH_PREFER_PROCESS_TAVILY_KEY"))
    if prefer_process and process_key:
        api_key = process_key
        source = "进程环境"
    elif project_key:
        api_key = project_key
        source = "项目.env"
    else:
        api_key = process_key
        source = "进程环境" if process_key else "未配置"

    project_base_url = str(project_values.get("TAVILY_API_BASE_URL", "") or "").strip()
    process_base_url = str(os.environ.get("TAVILY_API_BASE_URL", "") or "").strip()
    api_base_url = (process_base_url if prefer_process else project_base_url) or process_base_url or None
    return api_key, api_base_url, source


def tavily_is_configured() -> bool:
    return bool(current_tavily_configuration()[0])


def tavily_key_fingerprint(api_key: str | None = None) -> str:
    key = str(api_key or current_tavily_configuration()[0] or "").strip()
    if not key:
        return "missing"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _write_budget_state() -> None:
    if _BUDGET_USAGE_PATH is None:
        return
    payload = {
        "limit_credits": _BUDGET_LIMIT_CREDITS,
        "used_credits": _BUDGET_USED_CREDITS,
        "requests": _BUDGET_REQUESTS,
        "updated_at": time.time(),
    }
    try:
        _BUDGET_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = _BUDGET_USAGE_PATH.with_suffix(_BUDGET_USAGE_PATH.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary_path.replace(_BUDGET_USAGE_PATH)
    except OSError as exc:
        safe_print(f"[Tavily额度警告] 无法保存额度检查点: {exc}")


def configure_tavily_budget(
    target_experts: int,
    *,
    usage_path: str | Path | None = None,
) -> dict[str, int | None]:
    """Configure a durable per-task budget at five credits per requested expert."""
    global _BUDGET_LIMIT_CREDITS, _BUDGET_USED_CREDITS, _BUDGET_REQUESTS
    global _BUDGET_USAGE_PATH, _CONSECUTIVE_FAILURES, _CIRCUIT_OPENED_AT

    target = max(1, int(target_experts or 1))
    credits_per_expert = max(
        1.0,
        float(os.environ.get("TAVILY_CREDITS_PER_EXPERT", "5")),
    )
    calculated_limit = int(math.ceil(target * credits_per_expert))
    configured_cap = max(0, int(os.environ.get("TAVILY_MAX_CREDITS_PER_JOB", "0")))
    limit = min(calculated_limit, configured_cap) if configured_cap else calculated_limit
    path = Path(usage_path).resolve() if usage_path else None
    used = 0
    requests = 0
    if path and path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            used = max(0, int(existing.get("used_credits", 0) or 0))
            requests = max(0, int(existing.get("requests", 0) or 0))
        except (OSError, ValueError, TypeError):
            used = 0
            requests = 0

    with _BUDGET_LOCK:
        _BUDGET_LIMIT_CREDITS = limit
        _BUDGET_USED_CREDITS = used
        _BUDGET_REQUESTS = requests
        _BUDGET_USAGE_PATH = path
        _CONSECUTIVE_FAILURES = 0
        _CIRCUIT_OPENED_AT = 0.0
        _RESULT_CACHE.clear()
        _write_budget_state()
    estimate = estimate_tavily_credit_plan(target)
    safe_print(
        f"[Tavily额度] 目标专家={target}；额度上限={limit} credits；"
        f"常规路径预计={estimate['expected_requests']}次请求/"
        f"{estimate['expected_credits']} credits；"
        f"预留重试额度={max(0, limit - estimate['expected_credits'])} credits；"
        f"已使用={used} credits；历史请求={requests}次。"
    )
    return tavily_budget_status()


def tavily_budget_status() -> dict[str, int | None]:
    with _BUDGET_LOCK:
        remaining = (
            None
            if _BUDGET_LIMIT_CREDITS is None
            else max(0, _BUDGET_LIMIT_CREDITS - _BUDGET_USED_CREDITS)
        )
        return {
            "limit_credits": _BUDGET_LIMIT_CREDITS,
            "used_credits": _BUDGET_USED_CREDITS,
            "remaining_credits": remaining,
            "requests": _BUDGET_REQUESTS,
        }


def _estimated_credits(search: TavilySearch) -> int:
    depth = str(getattr(search, "search_depth", "") or "basic").lower()
    return 2 if depth == "advanced" else 1


def _cache_key(search: TavilySearch, query: str, fingerprint: str) -> tuple[Any, ...]:
    try:
        max_results = int(getattr(search, "max_results", 0) or 0)
    except (TypeError, ValueError):
        max_results = 0
    return (
        fingerprint,
        str(getattr(search, "search_depth", "") or "basic"),
        max_results,
        str(getattr(search, "topic", "") or "general"),
        " ".join(str(query or "").lower().split()),
    )


def _reserve_budget(estimated_credits: int) -> None:
    global _BUDGET_USED_CREDITS, _BUDGET_REQUESTS
    with _BUDGET_LOCK:
        if (
            _BUDGET_LIMIT_CREDITS is not None
            and _BUDGET_USED_CREDITS + estimated_credits > _BUDGET_LIMIT_CREDITS
        ):
            raise TavilyBudgetExceeded(
                f"本任务 Tavily 额度已达到 {_BUDGET_LIMIT_CREDITS} credits，"
                "后续网页补查将自动跳过"
            )
        _BUDGET_USED_CREDITS += estimated_credits
        _BUDGET_REQUESTS += 1
        # Persist the reservation before network I/O so a worker crash or a failed
        # response cannot make a resumed task exceed its original request budget.
        _write_budget_state()


def _release_or_reconcile_budget(estimated_credits: int, actual_credits: int | None) -> None:
    global _BUDGET_USED_CREDITS
    with _BUDGET_LOCK:
        if actual_credits is None:
            actual_credits = estimated_credits
        _BUDGET_USED_CREDITS = max(
            0,
            _BUDGET_USED_CREDITS - estimated_credits + max(0, actual_credits),
        )
        _write_budget_state()


def _release_failed_reservation(estimated_credits: int) -> None:
    # Failed requests remain charged against the local budget. Tavily may have
    # received and billed a request even when the client did not receive a response.
    with _BUDGET_LOCK:
        _write_budget_state()


def _check_circuit() -> None:
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPENED_AT
    threshold = max(1, int(os.environ.get("TAVILY_FAILURE_CIRCUIT_THRESHOLD", "3")))
    cooldown = max(0.0, float(os.environ.get("TAVILY_FAILURE_COOLDOWN_SECONDS", "60")))
    with _BUDGET_LOCK:
        if _CONSECUTIVE_FAILURES < threshold:
            return
        if cooldown and time.time() - _CIRCUIT_OPENED_AT >= cooldown:
            _CONSECUTIVE_FAILURES = 0
            _CIRCUIT_OPENED_AT = 0.0
            return
        raise TavilySoftFailure(
            f"Tavily 连续失败 {_CONSECUTIVE_FAILURES} 次，已暂时跳过；"
            f"约 {int(cooldown)} 秒后自动恢复探测"
        )


def _record_failure() -> None:
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPENED_AT
    with _BUDGET_LOCK:
        _CONSECUTIVE_FAILURES += 1
        _CIRCUIT_OPENED_AT = time.time()


def _record_success() -> None:
    global _CONSECUTIVE_FAILURES, _CIRCUIT_OPENED_AT
    with _BUDGET_LOCK:
        _CONSECUTIVE_FAILURES = 0
        _CIRCUIT_OPENED_AT = 0.0


def _log_configuration(api_key: str, api_base_url: str | None, source: str) -> None:
    fingerprint = tavily_key_fingerprint(api_key)
    target = api_base_url or "https://api.tavily.com"
    marker = (fingerprint, target)
    if marker in _LOGGED_CONFIGURATIONS:
        return
    _LOGGED_CONFIGURATIONS.add(marker)
    safe_print(
        f"[Tavily配置] 来源={source}；Key指纹={fingerprint}；API地址={target}。"
    )


def create_tavily_search(**kwargs: Any) -> TavilySearch:
    api_key, api_base_url, source = current_tavily_configuration()
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not configured")
    _log_configuration(api_key, api_base_url, source)
    # langchain-tavily 0.2.x must receive the plain key here. Wrapping the key in
    # SecretStr before passing a nested API wrapper serializes it as "**********".
    kwargs["tavily_api_key"] = api_key
    if api_base_url:
        kwargs["api_base_url"] = api_base_url
    kwargs.setdefault("include_usage", True)
    return TavilySearch(**kwargs)


def invoke_tavily_search(
    search: TavilySearch,
    query: str,
    *,
    stage: str,
) -> Any:
    """Invoke Tavily and leave auditable, credential-safe success metadata in logs."""
    wrapper = getattr(search, "api_wrapper", None)
    secret = getattr(wrapper, "tavily_api_key", None)
    try:
        actual_key = secret.get_secret_value() if secret is not None else ""
    except Exception:
        actual_key = ""
    fingerprint = tavily_key_fingerprint(actual_key)
    cache_key = _cache_key(search, query, fingerprint)
    with _BUDGET_LOCK:
        cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        safe_print(f"[Tavily缓存命中] 阶段={stage}；Key指纹={fingerprint}。")
        return copy.deepcopy(cached)

    _check_circuit()
    estimated_credits = _estimated_credits(search)
    _reserve_budget(estimated_credits)
    try:
        result = search.invoke({"query": query})
    except Exception as exc:
        _release_failed_reservation(estimated_credits)
        _record_failure()
        safe_print(
            f"[Tavily调用失败] 阶段={stage}；错误类型={type(exc).__name__}；"
            f"Key指纹={fingerprint}；原因={exc}"
        )
        raise
    if isinstance(result, dict) and result.get("error"):
        _release_failed_reservation(estimated_credits)
        _record_failure()
        error = result["error"]
        safe_print(
            f"[Tavily调用失败] 阶段={stage}；错误类型={type(error).__name__}；"
            f"Key指纹={fingerprint}；原因={error}"
        )
        raise RuntimeError(f"Tavily API 调用失败: {error}")
    if not isinstance(result, dict):
        _release_failed_reservation(estimated_credits)
        _record_failure()
        summary = " ".join(str(result or "").split())[:300]
        safe_print(
            f"[Tavily调用失败] 阶段={stage}；错误类型=UnexpectedResponse；"
            f"Key指纹={fingerprint}；原因={summary}"
        )
        raise RuntimeError(f"Tavily 未返回结构化检索结果: {summary}")
    if isinstance(result, dict):
        result_count = len(result.get("results", []) or [])
        request_id = str(result.get("request_id", "") or "未返回")
        usage = result.get("usage")
        usage_text = str(usage) if usage not in (None, "", {}) else "未返回"
        actual_credits = None
        if isinstance(usage, dict):
            try:
                parsed_credits = int(math.ceil(float(usage.get("credits", 0) or 0)))
                actual_credits = parsed_credits if parsed_credits > 0 else None
            except (TypeError, ValueError):
                actual_credits = None
        _release_or_reconcile_budget(estimated_credits, actual_credits)
        _record_success()
        with _BUDGET_LOCK:
            _RESULT_CACHE[cache_key] = copy.deepcopy(result)
            cache_limit = max(1, int(os.environ.get("TAVILY_QUERY_CACHE_SIZE", "4096")))
            while len(_RESULT_CACHE) > cache_limit:
                _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
        budget = tavily_budget_status()
    safe_print(
        f"[Tavily调用成功] 阶段={stage}；结果数={result_count}；"
        f"request_id={request_id}；usage={usage_text}；"
        f"Key指纹={fingerprint}；任务额度={budget['used_credits']}/"
        f"{budget['limit_credits'] or '未限制'} credits。"
    )
    return result
