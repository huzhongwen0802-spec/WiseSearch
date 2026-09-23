# -*- coding: utf-8 -*-

from __future__ import annotations

import re
from typing import Any


def _clean_detail(error: Exception | str | None, limit: int = 600) -> str:
    text = " ".join(str(error or "未知错误").split())
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token)\s*[:=]\s*)[^\s,;]+", r"\1***", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", text)
    return text[:limit]


def _source(text: str, stage: str) -> str:
    combined = f"{stage} {text}".lower()
    # Stage labels are more trustworthy than generic provider lists embedded in
    # legacy messages such as "常见来源包括 LLM、Tavily...".
    if any(
        marker in combined
        for marker in (
            "llm",
            "模型中转",
            "研究员节点",
            "验证节点",
            "纠错节点",
            "文档压缩节点",
            "细分领域推荐节点",
        )
    ):
        return "LLM 中转服务"
    if "opencli" in combined:
        return "OpenCLI 浏览器工具"
    if any(marker in combined for marker in ("个人主页", "主页访问", "http 主页")):
        return "专家主页网站"
    if "tavily" in combined:
        return "Tavily 搜索服务"
    if "openalex" in combined:
        return "OpenAlex 数据库"
    if any(marker in combined for marker in ("semantic scholar", "semanticscholar")):
        return "Semantic Scholar 数据库"
    if any(marker in combined for marker in ("excel", "xlsx", "工作簿", "批次读取", "表格解析")):
        return "Excel 处理"
    if any(marker in combined for marker in ("后台进程", "后台检索进程", "worker", "process")):
        return "后台任务进程"
    return "系统或网络环境"


def diagnose_error(
    error: Exception | str | None,
    *,
    stage: str = "",
) -> dict[str, str]:
    """Convert raw provider exceptions into stable, user-facing diagnostics."""
    detail = _clean_detail(error)
    lower = detail.lower()
    source = _source(lower, stage)

    if any(marker in lower for marker in ("sensitive words", "local:sensitive_words", "敏感词")):
        reason = "中转服务的内容过滤规则误拦截了正常学术请求。"
        action = "系统会保留检查点；可重试该细分领域，若重复出现需扩充学术术语保护规则。"
    elif any(
        marker in lower
        for marker in (
            "not configured",
            "missing api key",
            "api key is missing",
            "缺少环境变量",
            "未配置",
        )
    ):
        reason = "运行环境缺少该服务所需的地址、API Key 或其他配置。"
        action = "检查 .env 中对应服务的配置，修改后完整重启 Streamlit。"
    elif any(marker in lower for marker in ("401", "invalid_api_key", "incorrect api key", "authentication")):
        reason = "身份验证失败，API Key 无效、过期，或与 API_BASE_URL 不属于同一平台。"
        action = "检查 .env 中对应服务的密钥和地址，修改后完整重启系统。"
    elif any(marker in lower for marker in ("winerror 10013", "socket access", "禁止的访问权限")):
        reason = "本机防火墙、安全软件或受限运行环境禁止了外部网络连接。"
        action = "使用 start_streamlit_logged.cmd 在正常本地终端启动，并检查防火墙网络权限。"
    elif "403" in lower or "forbidden" in lower:
        reason = (
            "目标主页拒绝自动访问，通常是反爬虫、地区限制或访问权限限制。"
            if source == "专家主页网站"
            else "服务拒绝访问，当前密钥、账户或来源地址没有所需权限。"
        )
        action = (
            "系统将尝试 OpenCLI 或其他公开来源；单个主页失败不会终止整批任务。"
            if source == "专家主页网站"
            else "检查账户权限、额度与服务控制台中的访问限制。"
        )
    elif "404" in lower or "not found" in lower:
        reason = "请求的资源不存在，可能是主页已迁移、接口路径错误或记录已删除。"
        action = "系统将改用其他公开来源；若为 API 地址，请核对服务提供方的接口路径。"
    elif any(marker in lower for marker in ("429", "rate limit", "too many requests", "限流", "冷却")):
        reason = "服务触发调用频率或账户额度限制。"
        action = "等待服务冷却后仅重试未完成领域，并检查账户剩余额度和并发限制。"
    elif any(marker in lower for marker in ("500", "502", "503", "504", "bad gateway", "service unavailable")):
        reason = "远端服务或中转网关返回服务器错误，当前服务暂时不可用。"
        action = "已完成批次不受影响；待服务恢复后仅重试未完成领域。"
    elif any(marker in lower for marker in ("context length", "maximum context", "too many tokens", "token limit")):
        reason = "发送给模型的上下文超过该模型或中转服务允许的长度。"
        action = "减少单批证据文本或上传材料长度，再重试当前批次。"
    elif any(marker in lower for marker in ("name resolution", "getaddrinfo", "dns", "nodename nor servname")):
        reason = "域名解析失败，当前网络无法把服务域名解析为可访问地址。"
        action = "检查本机 DNS、代理和网络连接，恢复后仅重试未完成领域。"
    elif any(marker in lower for marker in ("ssl", "certificate", "tls")):
        reason = "HTTPS 证书校验或 TLS 握手失败。"
        action = "检查系统时间、代理证书和证书链；主页失败时系统会改用其他来源。"
    elif any(marker in lower for marker in ("connection refused", "actively refused", "连接被拒绝")):
        reason = "目标服务端口拒绝连接，服务可能未启动或暂时不可用。"
        action = "确认对应服务或 OpenCLI Bridge 已启动，稍后重试未完成领域。"
    elif any(marker in lower for marker in ("connection reset", "remote disconnected", "connection aborted", "eof")):
        reason = "连接已建立但被远端服务提前断开。"
        action = "通常属于服务波动或目标站限制；系统会保留已完成批次，可稍后续跑。"
    elif any(marker in lower for marker in ("connection error", "connecterror", "api connection", "连接失败")):
        reason = "客户端未能与目标服务建立稳定连接。"
        action = "检查网络和对应服务状态；系统已保存断点，可稍后续跑。"
    elif any(marker in lower for marker in ("timeout", "timed out", "readtimeout", "connecttimeout", "超时")):
        reason = "服务端或网络长时间未返回响应。"
        action = "系统不会丢失已完成批次；待服务恢复后仅重试未完成领域。"
    elif any(marker in lower for marker in ("permission denied", "winerror 32", "文件正在使用", "另一个程序正在使用")):
        reason = "结果文件或运行目录被占用，系统无法读取或写入。"
        action = "关闭占用该 Excel 的程序，并确认当前用户对项目目录具有写入权限后重试。"
    elif any(marker in lower for marker in ("no space left", "disk full", "磁盘空间不足")):
        reason = "磁盘剩余空间不足，无法保存检查点或 Excel 文件。"
        action = "释放项目所在磁盘空间后，从已保存的检查点继续运行。"
    elif any(marker in lower for marker in ("no module named", "modulenotfounderror", "importerror")):
        reason = "运行环境缺少必要的 Python 依赖，或启动时使用了错误的虚拟环境。"
        action = "使用项目 .venv 启动，并根据 requirements.txt 补齐依赖。"
    elif any(marker in lower for marker in ("cancelled", "canceled", "keyboardinterrupt", "被取消")):
        reason = "任务被用户、系统重启或运行环境主动取消。"
        action = "已完成批次仍保留在检查点中，可点击仅重试未完成领域继续。"
    elif any(marker in lower for marker in ("json", "markdown", "解析", "未生成有效", "未返回有效")):
        reason = "服务返回了内容，但格式不符合系统要求，无法生成有效专家批次。"
        action = "重试该批次；若持续发生，需要检查模型输出格式和表格字段完整性。"
    else:
        reason = "执行过程中出现未归类异常。"
        action = "查看下方技术详情和 worker.log；已完成批次与检查点不会被删除。"

    summary = f"{source}：{reason}"
    return {
        "错误来源": source,
        "失败原因": reason,
        "建议操作": action,
        "技术详情": detail,
        "错误摘要": summary,
    }


def format_error_for_user(
    error: Exception | str | None,
    *,
    stage: str = "",
) -> str:
    diagnostic = diagnose_error(error, stage=stage)
    return (
        f"来源：{diagnostic['错误来源']}｜"
        f"原因：{diagnostic['失败原因']}｜"
        f"建议：{diagnostic['建议操作']}｜"
        f"技术详情：{diagnostic['技术详情']}"
    )


def checkpoint_error_updates(error: Exception | str | None, *, stage: str = "") -> dict[str, str]:
    diagnostic = diagnose_error(error, stage=stage)
    return {
        "last_error": diagnostic["错误摘要"],
        "error_source": diagnostic["错误来源"],
        "error_reason": diagnostic["失败原因"],
        "error_action": diagnostic["建议操作"],
        "error_detail": diagnostic["技术详情"],
    }
