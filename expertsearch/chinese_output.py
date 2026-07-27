# -*- coding: utf-8 -*-

import json
import os
import re
import time
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .llm_safety import sanitize_for_llm
from .safe_logging import safe_print

load_dotenv()

TRANSLATABLE_COLUMNS = [
    "国籍",
    "所在工作机构",
    "职位",
    "学科领域",
    "细分领域",
    "工作经历",
    "主要成果",
    "国内合作学者",
    "国内合作单位",
    "入选依据",
    "研究兴趣",
    "教育背景",
    "领域关联依据",
    "主页访问失败原因",
    "姓名验证依据",
    "生存状态核验依据",
]

GLOSSARY_REPLACEMENTS = [
    (r"\bAssociate Professor\b", "副教授"),
    (r"\bAssistant Professor\b", "助理教授"),
    (r"\bEmeritus Professor\b", "荣休教授"),
    (r"\bVisiting Professor\b", "访问教授"),
    (r"\bProfessor\b", "教授"),
    (r"\bResearch Professor\b", "研究教授"),
    (r"\bPrincipal Investigator\b", "首席研究员"),
    (r"\bResearch Scientist\b", "研究科学家"),
    (r"\bSenior Scientist\b", "高级科学家"),
    (r"\bChief Scientist\b", "首席科学家"),
    (r"\bDirector\b", "主任"),
    (r"\bChair\b", "主任"),
    (r"\bDepartment of\b", "系"),
    (r"\bSchool of\b", "学院"),
    (r"\bCollege of\b", "学院"),
    (r"\bInstitute of\b", "研究所"),
    (r"\bResearch Center\b", "研究中心"),
    (r"\bResearch Centre\b", "研究中心"),
    (r"\bUniversity\b", "大学"),
    (r"\bNational Academy\b", "国家科学院"),
    (r"\bFellow\b", "会士"),
    (r"\baward\b", "奖项"),
    (r"\bawards\b", "奖项"),
    (r"\bresearch interests?\b", "研究方向"),
    (r"\bcollaboration\b", "合作"),
    (r"\bco-?author\b", "合著者"),
]

ALLOWED_ENGLISH_TOKENS = {
    "AI",
    "DNA",
    "RNA",
    "QTL",
    "CRISPR",
    "GIS",
    "H",
    "i10",
    "IEEE",
    "ACM",
    "AAAI",
    "ORCID",
    "OpenAlex",
}


def normalize_chinese_text(value: Any) -> str:
    text = str(value or "").strip()
    for pattern, replacement in GLOSSARY_REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*;\s*", "；", text)
    text = re.sub(r"\s*:\s*", "：", text)
    return text.strip()


def _text_for_english_check(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", " ", text)
    for token in ALLOWED_ENGLISH_TOKENS:
        text = re.sub(rf"\b{re.escape(token)}\b", " ", text, flags=re.IGNORECASE)
    return text


def english_residual_detail(value: Any) -> tuple[bool, int, str]:
    text = _text_for_english_check(value)
    words = re.findall(r"\b[A-Za-z][A-Za-z-]{2,}\b", text)
    latin_chars = len(re.findall(r"[A-Za-z]", text))
    meaningful_chars = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    ratio = latin_chars / meaningful_chars if meaningful_chars else 0.0
    has_residual = len(words) >= 4 or (len(words) >= 2 and ratio >= 0.35)
    return has_residual, len(words), " ".join(words[:12])


def _translation_llm() -> ChatOpenAI | None:
    api_key = os.environ.get("API_KEY", "")
    api_base = os.environ.get("API_BASE_URL", "")
    if not api_key or not api_base:
        return None
    return ChatOpenAI(
        model=os.environ.get("DELIVERY_TRANSLATION_MODEL", "gpt-5.5"),
        temperature=0,
        api_key=api_key,
        base_url=api_base,
        streaming=False,
    )


def _extract_json_array(content: str) -> list[dict[str, Any]]:
    text = str(content or "").strip().replace("```json", "").replace("```", "")
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        return []
    parsed = json.loads(text[start:end + 1])
    return parsed if isinstance(parsed, list) else []


def _translate_items(items: list[dict[str, str]], llm: ChatOpenAI) -> dict[str, str]:
    system_prompt = """
你是专业的中文科技信息翻译编辑。请把输入 JSON 数组中每个 text 字段的英文叙述翻译为准确、简洁、自然的中文。
规则：
1. 只输出 JSON 数组，每项严格包含 id 和 text。
2. 专家姓名、机构专名、论文名、奖项名可在中文后用括号保留原文。
3. URL、邮箱、数字、年份、H指数、i10、AI、DNA、RNA、QTL 等缩写保持不变。
4. 不新增事实，不删除事实，不解释，不总结。
"""
    payload = json.dumps(items, ensure_ascii=False)
    last_error = None
    for attempt in range(2):
        try:
            response = llm.invoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=sanitize_for_llm(payload)),
                ]
            )
            translated = _extract_json_array(response.content)
            return {
                str(item.get("id")): str(item.get("text", "")).strip()
                for item in translated
                if item.get("id") is not None
            }
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    safe_print(f"[中文交付翻译警告] 批量翻译失败: {last_error}")
    return {}


def translate_delivery_dataframe(
    df: pd.DataFrame,
    enable_llm: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    规范化最终交付主表，并对英文残留执行批量翻译和二次审计。
    """
    work_df = df.copy()
    for column in TRANSLATABLE_COLUMNS:
        if column in work_df.columns:
            work_df[column] = work_df[column].apply(normalize_chinese_text)

    candidates = []
    max_cells = int(os.environ.get("DELIVERY_TRANSLATION_MAX_CELLS", "240"))
    for row_index, row in work_df.iterrows():
        expert_name = str(row.get("姓名", "")).strip()
        for column in TRANSLATABLE_COLUMNS:
            if column not in work_df.columns:
                continue
            value = str(row.get(column, "") or "").strip()
            has_residual, _, _ = english_residual_detail(value)
            if has_residual and len(candidates) < max_cells:
                candidates.append(
                    {
                        "id": f"{row_index}:{column}",
                        "text": value,
                        "expert": expert_name,
                    }
                )

    llm_enabled = (
        enable_llm
        and os.environ.get("DELIVERY_ENABLE_LLM_TRANSLATION", "true").lower()
        in {"1", "true", "yes"}
    )
    llm = _translation_llm() if llm_enabled and candidates else None
    if llm is not None:
        batch_size = int(os.environ.get("DELIVERY_TRANSLATION_BATCH_CELLS", "18"))
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start:start + batch_size]
            translated = _translate_items(
                [{"id": item["id"], "text": item["text"]} for item in batch],
                llm,
            )
            for item in batch:
                translated_text = translated.get(item["id"], "").strip()
                if translated_text:
                    row_index_text, column = item["id"].split(":", 1)
                    work_df.at[int(row_index_text), column] = normalize_chinese_text(translated_text)

    audit_rows = []
    for row_index, row in work_df.iterrows():
        expert_name = str(row.get("姓名", "")).strip()
        for column in TRANSLATABLE_COLUMNS:
            if column not in work_df.columns:
                continue
            value = str(row.get(column, "") or "").strip()
            has_residual, word_count, snippet = english_residual_detail(value)
            if has_residual:
                audit_rows.append(
                    {
                        "姓名": expert_name,
                        "字段": column,
                        "检查结果": "存在英文残留",
                        "英文词数": word_count,
                        "残留片段": snippet,
                        "处理建议": "正式交付前人工确认专名是否需要保留，或补充中文翻译。",
                    }
                )

    audit_df = pd.DataFrame(
        audit_rows,
        columns=["姓名", "字段", "检查结果", "英文词数", "残留片段", "处理建议"],
    )
    if audit_df.empty:
        audit_df = pd.DataFrame(
            [["全部记录", "全部交付字段", "通过", 0, "", "未发现需要处理的明显英文叙述残留。"]],
            columns=["姓名", "字段", "检查结果", "英文词数", "残留片段", "处理建议"],
        )
    return work_df, audit_df
