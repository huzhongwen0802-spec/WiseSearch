# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from .batch_execution import (
    is_llm_connection_failure,
    merge_subdomain_statuses,
    retryable_subdomains,
)
from .error_diagnostics import checkpoint_error_updates, diagnose_error
from .main import run_agent_task
from .safe_logging import safe_print
from .search_checkpoint import (
    checkpoint_root,
    clear_search_worker_pid,
    load_search_checkpoint,
    mark_search_checkpoint,
    record_batch_file,
    write_search_worker_pid,
)
from .tavily_client import configure_tavily_budget, tavily_budget_status
from .utils import (
    data_source_summary,
    finalize_merged_experts,
    normalize_expert_name_for_dedupe,
    remove_non_expert_rows,
    write_expert_excel,
)


DEFAULT_EXPERTS_PER_ROUND = 10


def _safe_filename_part(value: str) -> str:
    safe_value = re.sub(r'[\\/:*?"<>|]+', "_", str(value).strip())
    safe_value = re.sub(r"\s+", "_", safe_value)
    return safe_value.strip("._") or "专家检索"


def _round_conditions(target_count: int, experts_per_round: int) -> list[str]:
    focuses = [
        "顶尖权威学者，如院士、最高奖项获得者和领域奠基人",
        "高影响力学者，如高被引研究者、重要学术组织 Fellow 和资深教授",
        "杰出中坚研究者，如重点实验室负责人、项目负责人和活跃学术带头人",
        "优秀青年与新兴方向领军学者，如青年 Fellow、重要青年奖项获得者",
        "跨机构、产业转化和国际合作中具有代表性的高质量专家",
        "此前轮次尚未覆盖的跨国家、跨区域高质量专家，优先核验当前任职和领域相关性",
        "对遗漏候选进行补充复核，优先具备权威主页、数据库或奖项名录证据的专家",
        "来自不同国家和区域重点科研机构的代表性专家，扩大地域与机构覆盖面",
        "与该领域直接相关的交叉学科专家，要求提供明确成果或项目证据",
        "权威人才名录、重要学术会议和专业协会中此前未覆盖的高质量专家",
    ]
    round_count = (max(0, int(target_count)) + experts_per_round - 1) // experts_per_round
    return [
        f"第 {index + 1} 轮：检索 {experts_per_round} 位"
        f"{focuses[index] if index < len(focuses) else '此前未覆盖、但具有直接领域证据的高质量专家'}；"
        "必须排除该细分领域此前轮次已经出现的专家"
        for index in range(round_count)
    ]


def _read_frame(path: str) -> pd.DataFrame:
    with pd.ExcelFile(path) as workbook:
        sheet = "评价与验证" if "评价与验证" in workbook.sheet_names else workbook.sheet_names[0]
        return remove_non_expert_rows(pd.read_excel(workbook, sheet_name=sheet))


def _new_names(path: str, excluded_keys: set[str]) -> list[str]:
    frame = _read_frame(path)
    if frame.empty or "专家姓名" not in frame.columns:
        return []
    result: list[str] = []
    for raw_name in frame["专家姓名"].fillna("").tolist():
        name = str(raw_name).strip()
        key = normalize_expert_name_for_dedupe(name)
        if name and key and key not in excluded_keys:
            excluded_keys.add(key)
            result.append(name)
    return result


def _names_for_subdomain(frame: pd.DataFrame, subdomain: str) -> list[str]:
    if frame.empty or "专家姓名" not in frame.columns or "细分领域" not in frame.columns:
        return []
    target = str(subdomain or "").strip()
    matches = frame[
        frame["细分领域"].fillna("").astype(str).apply(
            lambda value: target
            in [item.strip() for item in re.split(r"[；;、|/\n]+", value) if item.strip()]
        )
    ]
    return list(
        dict.fromkeys(
            str(name).strip()
            for name in matches["专家姓名"].fillna("").tolist()
            if str(name).strip()
        )
    )


def _select_balanced(frame: pd.DataFrame, subdomains: list[str], target_total: int) -> pd.DataFrame:
    if frame.empty or len(frame) <= target_total:
        return frame
    if "细分领域" not in frame.columns or not subdomains:
        return frame.head(target_total).copy()
    selected: list[int] = []
    base_quota, remainder = divmod(target_total, len(subdomains))
    for index, subdomain in enumerate(subdomains):
        quota = base_quota + (1 if index < remainder else 0)
        matches = frame[
            frame["细分领域"].fillna("").astype(str).apply(
                lambda value: subdomain
                in [item.strip() for item in re.split(r"[；;、|/\n]+", value) if item.strip()]
            )
        ]
        selected.extend(matches.head(quota).index.tolist())
    selected = list(dict.fromkeys(selected))
    if len(selected) < target_total:
        remaining = frame.loc[~frame.index.isin(selected)]
        selected.extend(remaining.head(target_total - len(selected)).index.tolist())
    result = frame.loc[selected[:target_total]]
    if "评价_TotalScore" in result.columns:
        result = result.sort_values("评价_TotalScore", ascending=False, na_position="last")
    return result


def _status_for(
    subdomain: str,
    actual: int,
    target: int,
    successful_attempts: int,
    failed_attempts: int,
    errors: list[str],
) -> dict[str, Any]:
    status = "成功" if actual >= target else ("部分成功" if actual else "失败")
    diagnostics = [diagnose_error(error) for error in errors[-3:]]

    def unique_values(key: str) -> str:
        return "；".join(
            dict.fromkeys(
                item[key]
                for item in diagnostics
                if str(item.get(key, "") or "").strip()
            )
        )

    return {
        "细分领域": subdomain,
        "状态": status,
        "实际人数": actual,
        "目标人数": target,
        "成功批次": successful_attempts,
        "失败尝试": failed_attempts,
        "错误来源": unique_values("错误来源"),
        "失败原因": unique_values("失败原因"),
        "建议操作": unique_values("建议操作"),
        "技术详情": diagnostics[-1]["技术详情"] if diagnostics else "",
        "最近错误": unique_values("错误摘要"),
    }


def _save_progress(checkpoint: dict[str, Any], message: str, **updates: Any) -> dict[str, Any]:
    safe_print(f"[后台检索] {message}")
    return mark_search_checkpoint(
        checkpoint,
        "running",
        progress_message=message,
        **updates,
    )


def _merge_output(checkpoint: dict[str, Any], statuses: list[dict[str, Any]], started_at: float) -> dict[str, Any]:
    requested_subdomains = list(checkpoint.get("requested_subdomains", []) or [])
    output_paths = [
        str(path)
        for path in checkpoint.get("batch_files", []) or []
        if os.path.exists(str(path))
    ]
    previous_final = str(checkpoint.get("final_file_path", "") or "")
    if previous_final and os.path.exists(previous_final):
        output_paths.append(previous_final)
    frames: list[pd.DataFrame] = []
    for path in dict.fromkeys(output_paths):
        try:
            frame = _read_frame(path)
        except Exception as error:
            safe_print(f"[后台检索] 跳过无法读取的批次 {path}: {error}")
            continue
        if not frame.empty:
            frames.append(frame)

    unfinished = retryable_subdomains(statuses)
    elapsed = int(time.time() - started_at)
    if not frames:
        no_output_error = "Excel 合并阶段未生成可合并的专家批次"
        return mark_search_checkpoint(
            checkpoint,
            "failed",
            subdomain_statuses=statuses,
            failed_subdomains=unfinished,
            elapsed_seconds=elapsed,
            progress_message="本轮未生成有效结果，已保留检查点供重试",
            **checkpoint_error_updates(no_output_error, stage="Excel 合并"),
        )

    main_domain = str(checkpoint.get("main_domain", "") or "").strip()
    include_chinese = bool(checkpoint.get("include_chinese_experts", False))
    target_total = int(checkpoint.get("requested_total_experts", 0) or 0)
    extra_urls = list(checkpoint.get("extra_data_source_urls", []) or [])
    document_names = list(checkpoint.get("supplemental_document_names", []) or [])
    checkpoint = _save_progress(
        checkpoint,
        "全部检索轮次已结束，正在合并、去重、补全并生成最终 Excel",
        subdomain_statuses=statuses,
        failed_subdomains=unfinished,
        elapsed_seconds=elapsed,
    )
    master = pd.concat(frames, ignore_index=True)
    query = f"{main_domain}领域下的{'、'.join(requested_subdomains)}方向顶级专家信息"
    master = finalize_merged_experts(
        master,
        include_chinese_experts=include_chinese,
        query=query,
    )
    master = _select_balanced(master, requested_subdomains, target_total)

    output_dir = Path(
        str(checkpoint.get("final_output_dir", "") or "Expert_Results")
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    file_name = f"{_safe_filename_part(main_domain)}_{timestamp}_批量专家总名单.xlsx"
    final_path = output_dir / file_name
    write_expert_excel(
        master,
        str(final_path),
        data_source_summary=data_source_summary(
            extra_urls,
            query=main_domain,
            supplemental_document_names=document_names,
        ),
        query=main_domain,
        extra_data_source_urls=extra_urls,
        supplemental_document_names=document_names,
        translate_delivery=True,
        final_delivery_only=True,
    )

    if previous_final and Path(previous_final).resolve() != final_path:
        try:
            Path(previous_final).unlink()
        except OSError:
            pass
    elapsed = int(time.time() - started_at)
    final_status = "partial" if unfinished else "completed"
    checkpoint = mark_search_checkpoint(
        checkpoint,
        final_status,
        subdomain_statuses=statuses,
        failed_subdomains=unfinished,
        elapsed_seconds=elapsed,
        expert_count=len(master),
        final_file_name=file_name,
        final_file_path=str(final_path),
        progress_message=(
            f"已生成阶段结果，仍有 {len(unfinished)} 个细分领域可继续重试"
            if unfinished
            else "检索完成，最终 Excel 已生成"
        ),
        last_error="",
        error_source="",
        error_reason="",
        error_action="",
        error_detail="",
    )
    if not unfinished:
        shutil.rmtree(str(checkpoint.get("batch_output_dir", "") or ""), ignore_errors=True)
    tavily_status = tavily_budget_status()
    safe_print(
        f"[Tavily任务汇总] 请求={tavily_status['requests']}次；"
        f"额度={tavily_status['used_credits']}/{tavily_status['limit_credits']} credits。"
    )
    return checkpoint


def run_search_job(job_id: str) -> dict[str, Any]:
    checkpoint = load_search_checkpoint(job_id)
    if not checkpoint:
        raise ValueError(f"未找到检索任务检查点: {job_id}")
    started_at = time.time()
    previous_elapsed = int(checkpoint.get("elapsed_seconds", 0) or 0)
    requested_subdomains = list(checkpoint.get("requested_subdomains", []) or [])
    run_subdomains = list(
        checkpoint.get("run_subdomains", [])
        or checkpoint.get("failed_subdomains", [])
        or requested_subdomains
    )
    targets = {
        str(key): int(value)
        for key, value in dict(checkpoint.get("subdomain_targets", {}) or {}).items()
    }
    requested_total = int(checkpoint.get("requested_total_experts", 0) or 0)
    budget_target = requested_total or sum(targets.values()) or DEFAULT_EXPERTS_PER_ROUND
    configure_tavily_budget(
        budget_target,
        usage_path=checkpoint_root() / job_id / "tavily_usage.json",
    )
    experts_per_round = max(
        1,
        int(checkpoint.get("experts_per_round", DEFAULT_EXPERTS_PER_ROUND) or DEFAULT_EXPERTS_PER_ROUND),
    )
    include_chinese = bool(checkpoint.get("include_chinese_experts", False))
    extra_urls = list(checkpoint.get("extra_data_source_urls", []) or [])
    document_context = str(checkpoint.get("supplemental_document_context", "") or "")
    document_names = list(checkpoint.get("supplemental_document_names", []) or [])
    batch_dir = str(checkpoint.get("batch_output_dir", "") or "")
    Path(batch_dir).mkdir(parents=True, exist_ok=True)

    batch_files = [
        str(path)
        for path in checkpoint.get("batch_files", []) or []
        if os.path.exists(str(path))
    ]
    baseline_paths = list(batch_files)
    previous_final = str(checkpoint.get("final_file_path", "") or "")
    if previous_final and os.path.exists(previous_final):
        baseline_paths.append(previous_final)
    baseline_frames: list[pd.DataFrame] = []
    for path in dict.fromkeys(baseline_paths):
        try:
            frame = _read_frame(path)
        except Exception as error:
            safe_print(f"[后台检索] 读取历史批次失败 {path}: {error}")
            continue
        if not frame.empty:
            baseline_frames.append(frame)
    baseline = pd.concat(baseline_frames, ignore_index=True) if baseline_frames else pd.DataFrame()

    previous_statuses = list(checkpoint.get("subdomain_statuses", []) or [])
    current_statuses: list[dict[str, Any]] = []
    tier_retries = max(0, int(os.environ.get("SUBDOMAIN_TIER_RECOVERY_ATTEMPTS", "2")))
    topup_attempts = max(0, int(os.environ.get("SUBDOMAIN_FINAL_TOPUP_ATTEMPTS", "3")))
    breaker_threshold = max(1, int(os.environ.get("SUBDOMAIN_LLM_CIRCUIT_BREAKER_THRESHOLD", "3")))
    consecutive_llm_failures = 0
    circuit_reason = ""

    checkpoint = _save_progress(
        checkpoint,
        f"后台任务已启动，准备处理 {len(run_subdomains)} 个细分领域",
        worker_pid=os.getpid(),
        worker_started_at=time.time(),
        run_subdomains=run_subdomains,
    )
    for subdomain_index, subdomain in enumerate(run_subdomains):
        target = int(targets.get(subdomain, experts_per_round))
        conditions = _round_conditions(target, experts_per_round)
        names = _names_for_subdomain(baseline, subdomain)
        name_keys = {
            key
            for key in (normalize_expert_name_for_dedupe(name) for name in names)
            if key
        }
        successes = 0
        failures = 0
        errors: list[str] = []
        checkpoint = _save_progress(
            checkpoint,
            f"正在检索“{subdomain}”，已有 {len(names)}/{target} 位",
            current_subdomain=subdomain,
            current_round=0,
            progress_current=subdomain_index,
            progress_total=len(run_subdomains),
            elapsed_seconds=previous_elapsed + int(time.time() - started_at),
        )

        for round_index, condition in enumerate(conditions):
            if len(names) >= target:
                break
            round_new_names: list[str] = []
            for attempt in range(tier_retries + 1):
                remaining = min(experts_per_round - len(round_new_names), target - len(names))
                if remaining <= 0:
                    break
                scope = (
                    "本次检索需要包含国内专家，中国大陆、香港、澳门、台湾的顶级专家均可进入候选名单。"
                    if include_chinese
                    else "本次检索不需要国内专家，请避开中国大陆、香港、澳门、台湾当前主要任职机构的专家。"
                )
                recovery = (
                    "" if attempt == 0 else f"这是第 {attempt} 次补位检索，请补充 {remaining} 位新候选。"
                )
                query = (
                    f"{checkpoint.get('main_domain', '')}领域下的{subdomain}方向顶级专家信息。{scope}"
                    f"当前检索目标：{condition}。{recovery}"
                    f"必须严格查出 {remaining} 个此前未出现的专家，并输出 Markdown 表格。"
                )
                checkpoint = _save_progress(
                    checkpoint,
                    f"“{subdomain}”第 {round_index + 1}/{len(conditions)} 轮，第 {attempt + 1} 次尝试",
                    current_subdomain=subdomain,
                    current_round=round_index + 1,
                    elapsed_seconds=previous_elapsed + int(time.time() - started_at),
                )
                path, error = run_agent_task(
                    query,
                    include_chinese_experts=include_chinese,
                    extra_data_source_urls=extra_urls,
                    supplemental_document_context=document_context,
                    supplemental_document_names=document_names,
                    excluded_expert_names=names,
                    target_expert_count=remaining,
                    output_dir=batch_dir,
                )
                if path and os.path.exists(path):
                    checkpoint = record_batch_file(
                        checkpoint,
                        path,
                        current_subdomain=subdomain,
                        current_round=round_index + 1,
                    )
                    batch_files.append(path)
                    successes += 1
                    consecutive_llm_failures = 0
                    try:
                        added = _new_names(path, name_keys)
                    except Exception as batch_error:
                        added = []
                        errors.append(f"批次读取失败: {batch_error}")
                    round_new_names.extend(added)
                    names.extend(added)
                    checkpoint = _save_progress(
                        checkpoint,
                        f"“{subdomain}”累计获得 {len(names)}/{target} 位专家",
                        elapsed_seconds=previous_elapsed + int(time.time() - started_at),
                    )
                else:
                    failures += 1
                    error_text = str(error or "后端未返回有效文件")
                    errors.append(error_text)
                    consecutive_llm_failures = (
                        consecutive_llm_failures + 1
                        if is_llm_connection_failure(error_text)
                        else 0
                    )
                    checkpoint = _save_progress(
                        checkpoint,
                        f"“{subdomain}”本次尝试失败，系统将按策略继续或保留断点",
                        elapsed_seconds=previous_elapsed + int(time.time() - started_at),
                        **checkpoint_error_updates(error_text),
                    )
                    if consecutive_llm_failures >= breaker_threshold:
                        circuit_reason = (
                            f"连续 {consecutive_llm_failures} 次 LLM/API 连接失败，"
                            "本轮已停止，已完成数据会生成阶段结果。"
                        )
                        break
            if circuit_reason:
                break

        if not circuit_reason:
            for topup_index in range(topup_attempts):
                remaining = target - len(names)
                if remaining <= 0:
                    break
                requested = min(experts_per_round, remaining)
                query = (
                    f"{checkpoint.get('main_domain', '')}领域下的{subdomain}方向顶级专家信息。"
                    "请寻找此前轮次尚未覆盖、但具有直接领域证据的高质量专家。"
                    f"必须严格补充 {requested} 个此前未出现的专家，并输出 Markdown 表格。"
                )
                checkpoint = _save_progress(
                    checkpoint,
                    f"“{subdomain}”最终补位第 {topup_index + 1} 次，尚缺 {remaining} 位",
                    elapsed_seconds=previous_elapsed + int(time.time() - started_at),
                )
                path, error = run_agent_task(
                    query,
                    include_chinese_experts=include_chinese,
                    extra_data_source_urls=extra_urls,
                    supplemental_document_context=document_context,
                    supplemental_document_names=document_names,
                    excluded_expert_names=names,
                    target_expert_count=requested,
                    output_dir=batch_dir,
                )
                if path and os.path.exists(path):
                    checkpoint = record_batch_file(
                        checkpoint,
                        path,
                        current_subdomain=subdomain,
                        current_round=len(conditions) + topup_index + 1,
                    )
                    batch_files.append(path)
                    successes += 1
                    consecutive_llm_failures = 0
                    try:
                        names.extend(_new_names(path, name_keys))
                    except Exception as batch_error:
                        errors.append(f"批次读取失败: {batch_error}")
                else:
                    failures += 1
                    error_text = str(error or "后端未返回有效文件")
                    errors.append(error_text)
                    consecutive_llm_failures = (
                        consecutive_llm_failures + 1
                        if is_llm_connection_failure(error_text)
                        else 0
                    )
                    checkpoint = _save_progress(
                        checkpoint,
                        f"“{subdomain}”补位尝试失败，系统将继续或保留断点",
                        elapsed_seconds=previous_elapsed + int(time.time() - started_at),
                        **checkpoint_error_updates(error_text),
                    )
                    if consecutive_llm_failures >= breaker_threshold:
                        circuit_reason = (
                            f"连续 {consecutive_llm_failures} 次 LLM/API 连接失败，"
                            "本轮已停止，已完成数据会生成阶段结果。"
                        )
                        break

        current_statuses.append(
            _status_for(subdomain, len(names), target, successes, failures, errors)
        )
        merged = merge_subdomain_statuses(
            requested_subdomains,
            previous_statuses,
            current_statuses,
        )
        checkpoint = _save_progress(
            checkpoint,
            f"“{subdomain}”处理结束，获得 {len(names)}/{target} 位专家",
            subdomain_statuses=merged,
            failed_subdomains=retryable_subdomains(merged),
            progress_current=subdomain_index + 1,
            elapsed_seconds=previous_elapsed + int(time.time() - started_at),
        )
        if circuit_reason:
            break

    merged_statuses = merge_subdomain_statuses(
        requested_subdomains,
        previous_statuses,
        current_statuses,
    )
    if circuit_reason:
        checkpoint = _save_progress(
            checkpoint,
            circuit_reason,
            subdomain_statuses=merged_statuses,
            failed_subdomains=retryable_subdomains(merged_statuses),
            **checkpoint_error_updates(circuit_reason, stage="LLM 中转服务"),
        )
    return _merge_output(checkpoint, merged_statuses, started_at - previous_elapsed)


def main() -> int:
    load_dotenv(encoding="utf-8-sig")
    if len(sys.argv) != 2:
        print("Usage: python -m expertsearch.search_worker <job_id>")
        return 2
    job_id = sys.argv[1]
    write_search_worker_pid(job_id, os.getpid())
    try:
        run_search_job(job_id)
        return 0
    except Exception as error:
        checkpoint = load_search_checkpoint(job_id)
        if checkpoint:
            statuses = list(checkpoint.get("subdomain_statuses", []) or [])
            mark_search_checkpoint(
                checkpoint,
                "interrupted",
                failed_subdomains=retryable_subdomains(statuses),
                progress_message="后台任务异常中断，已保存检查点，可继续重试",
                **checkpoint_error_updates(error, stage="后台任务进程"),
            )
        traceback.print_exc()
        return 1
    finally:
        clear_search_worker_pid(job_id, expected_pid=os.getpid())


if __name__ == "__main__":
    raise SystemExit(main())
