# -*- coding: utf-8 -*-

import os
import ast
from typing import Optional, Tuple
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from .state import AgentState, require_state_value
from .agents import document_processor_node, researcher_node, validator_node, corrector_node, llm
from .utils import excel_converter_node
from .llm_safety import is_sensitive_word_error
from .safe_logging import safe_print

# 加载环境变量 (需要有 GOOGLE_API_KEY)
load_dotenv()

def build_graph():
    """
    构建并返回定义好的 StateGraph
    """
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node("document_processor", document_processor_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("validator", validator_node)
    workflow.add_node("corrector", corrector_node)
    workflow.add_node("excel_converter", excel_converter_node)

    # 定义边（按顺序连接）
    workflow.set_entry_point("document_processor")
    workflow.add_edge("document_processor", "researcher")
    workflow.add_edge("researcher", "validator")
    workflow.add_edge("validator", "corrector")
    workflow.add_edge("corrector", "excel_converter")
    workflow.add_edge("excel_converter", END)

    # 编译计算图
    app = workflow.compile()
    return app

app = build_graph()

from .agents import get_dynamic_recommendations as _get_dynamic_recommendations

def get_dynamic_recommendations(main_domain):
    return _get_dynamic_recommendations(main_domain, llm)

def is_connection_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in [
            "connection error",
            "connecterror",
            "connecttimeout",
            "readtimeout",
            "timed out",
            "timeout",
            "connection refused",
            "connection reset",
            "api connection",
            "ssl",
        ]
    )


def run_agent_task(
    query: str,
    include_chinese_experts: bool = False,
    extra_data_source_urls: Optional[list[str]] = None,
    supplemental_document_context: str = "",
    supplemental_document_names: Optional[list[str]] = None,
    excluded_expert_names: Optional[list[str]] = None,
    target_expert_count: Optional[int] = None,
    output_dir: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """供前端调用的检索接口"""
    inputs = {
        "query": query,
        "include_chinese_experts": bool(include_chinese_experts),
        "extra_data_source_urls": extra_data_source_urls or [],
        "supplemental_document_context": supplemental_document_context or "",
        "supplemental_document_names": supplemental_document_names or [],
        "excluded_expert_names": excluded_expert_names or [],
        "target_expert_count": target_expert_count
        or max(1, int(os.environ.get("RESEARCHER_TARGET_EXPERTS", "20"))),
        "output_dir": output_dir or "Expert_Results",
    }
    try:
        final_state = app.invoke(inputs)
        excel_path = require_state_value(final_state, "excel_path")
        return os.path.abspath(excel_path), None
    except Exception as e:
        if is_sensitive_word_error(e):
            error_message = (
                "任务异常: 模型中转服务误判了正常学术术语并拒绝请求。"
                "我已加入学术术语保护层；如果你刚修改过代码，请先重启 Streamlit 后再试。"
                f" 原始错误: {e}"
            )
        elif is_connection_error(e):
            error_message = (
                "任务异常: 外部服务连接失败或超时。"
                "常见来源包括 LLM/API 中转服务、Tavily 搜索服务、OpenAlex/Semantic Scholar 数据库或网络波动。"
                f" 定位信息: {e}"
            )
        else:
            error_message = f"任务异常: {e}"
        safe_print(error_message)
        return None, error_message

# 如果你在本地测试，可以保留这段，不影响前端引入
if __name__ == "__main__":
    # 简单的本地终端测试入口
    run_agent_task("测试领域专家查3个")
