# -*- coding: utf-8 -*-

from __future__ import annotations

import math
from typing import Any


RETRYABLE_SUBDOMAIN_STATUSES = {"失败", "部分成功", "待重试"}


def allocate_subdomain_workload(
    subdomains: list[str],
    total_experts: int,
    experts_per_round: int,
) -> list[dict[str, int | str]]:
    """将总人数均匀分配到细分领域，并按单轮容量向上取整计算轮次。"""
    cleaned_subdomains = list(
        dict.fromkeys(str(item or "").strip() for item in subdomains if str(item or "").strip())
    )
    if not cleaned_subdomains:
        return []
    total = int(total_experts)
    per_round = int(experts_per_round)
    if total < len(cleaned_subdomains):
        raise ValueError("专家总人数不能少于已选细分领域数量")
    if per_round <= 0:
        raise ValueError("每轮查询人数必须大于 0")

    base_target, remainder = divmod(total, len(cleaned_subdomains))
    workload = []
    for index, subdomain in enumerate(cleaned_subdomains):
        target = base_target + (1 if index < remainder else 0)
        workload.append(
            {
                "细分领域": subdomain,
                "目标人数": target,
                "查询轮次": math.ceil(target / per_round),
            }
        )
    return workload


def retry_attempt_count(task_result: dict[str, Any] | None) -> int:
    """读取批量任务已执行的手动续跑次数，兼容旧版会话状态。"""
    try:
        return max(0, int((task_result or {}).get("retry_attempt_count", 0)))
    except (TypeError, ValueError):
        return 0


def is_llm_connection_failure(error_message: str | None) -> bool:
    """识别应触发批量任务熔断的 LLM/API 连接类错误。"""
    text = str(error_message or "").strip().lower()
    if not text:
        return False
    connection_markers = (
        "connection error",
        "connecttimeout",
        "readtimeout",
        "timed out",
        "连接失败",
        "连接超时",
        "服务连接失败或超时",
    )
    llm_markers = (
        "llm/api",
        "研究员节点",
        "验证节点",
        "纠错节点",
        "模型中转",
        "大模型",
    )
    return any(marker in text for marker in connection_markers) and any(
        marker in text for marker in llm_markers
    )


def retryable_subdomains(statuses: list[dict[str, Any]] | None) -> list[str]:
    """按原始顺序返回失败、部分成功或尚未执行的细分领域。"""
    result = []
    for item in statuses or []:
        subdomain = str(item.get("细分领域", "") or "").strip()
        status = str(item.get("状态", "") or "").strip()
        if subdomain and status in RETRYABLE_SUBDOMAIN_STATUSES and subdomain not in result:
            result.append(subdomain)
    return result


def merge_subdomain_statuses(
    requested_subdomains: list[str],
    previous_statuses: list[dict[str, Any]] | None,
    current_statuses: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """将续跑状态覆盖到历史状态，同时保持最初任务清单顺序。"""
    by_subdomain: dict[str, dict[str, Any]] = {}
    for item in previous_statuses or []:
        key = str(item.get("细分领域", "") or "").strip()
        if key:
            by_subdomain[key] = dict(item)
    for item in current_statuses or []:
        key = str(item.get("细分领域", "") or "").strip()
        if key:
            by_subdomain[key] = dict(item)

    merged = []
    for subdomain in requested_subdomains:
        item = by_subdomain.get(subdomain)
        if item is None:
            item = {
                "细分领域": subdomain,
                "状态": "待重试",
                "实际人数": 0,
                "成功批次": 0,
                "失败尝试": 0,
                "最近错误": "尚未执行",
            }
        merged.append(item)
    return merged
