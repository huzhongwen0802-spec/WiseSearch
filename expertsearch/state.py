# -*- coding: utf-8 -*-

from typing import NotRequired, Optional, TypedDict

class AgentState(TypedDict):
    """
    定义图在各个节点之间传递的状态。
    """
    query: str
    include_chinese_experts: NotRequired[bool]
    extra_data_source_urls: NotRequired[list[str]]
    supplemental_document_context: NotRequired[str]
    supplemental_document_names: NotRequired[list[str]]
    compressed_supplemental_document_context: NotRequired[str]
    document_compression_status: NotRequired[str]
    excluded_expert_names: NotRequired[list[str]]
    target_expert_count: NotRequired[int]
    output_dir: NotRequired[str]
    research_data: NotRequired[Optional[str]]
    validation_feedback: NotRequired[Optional[str]]
    final_data: NotRequired[Optional[str]]
    excel_path: NotRequired[Optional[str]]

def require_state_value(state: AgentState, key: str) -> str:
    """
    读取必需的状态字段；如果上游节点没有产出，立即抛出清晰错误。
    """
    value = state.get(key)
    if not value:
        raise ValueError(f"LangGraph 状态缺少必需字段: {key}")
    return value
