# -*- coding: utf-8 -*-

import streamlit as st
import base64
import time
import os
import re
import shutil
from pathlib import Path
import pandas as pd
from expertsearch.batch_execution import (
    allocate_subdomain_workload,
    is_llm_connection_failure,
    merge_subdomain_statuses,
    retry_attempt_count,
    retryable_subdomains,
)
from expertsearch.error_diagnostics import checkpoint_error_updates, diagnose_error
from expertsearch.main import run_agent_task, get_dynamic_recommendations
from expertsearch.search_checkpoint import (
    checkpoint_task_result,
    create_search_checkpoint,
    end_and_clear_search_job,
    load_latest_search_checkpoint,
    load_search_checkpoint,
    mark_search_checkpoint,
    record_batch_file,
    register_search_job,
    search_job_is_running,
    stop_search_job_preserving_progress,
    unregister_search_job,
)
from expertsearch.search_job_process import launch_search_job
from expertsearch.supplemental_documents import extract_supplemental_documents
from expertsearch.table_completion import run_table_completion
from expertsearch.utils import (
    data_source_summary,
    finalize_merged_experts,
    normalize_expert_name_for_dedupe,
    remove_non_expert_rows,
    write_expert_excel,
)


def _diagnostic_from_record(record: dict | None) -> dict[str, str] | None:
    record = record or {}
    source = str(record.get("error_source", "") or record.get("错误来源", "")).strip()
    reason = str(record.get("error_reason", "") or record.get("失败原因", "")).strip()
    action = str(record.get("error_action", "") or record.get("建议操作", "")).strip()
    detail = str(record.get("error_detail", "") or record.get("技术详情", "")).strip()
    raw_error = str(record.get("last_error", "") or record.get("最近错误", "")).strip()
    if source and reason:
        return {
            "错误来源": source,
            "失败原因": reason,
            "建议操作": action,
            "技术详情": detail or raw_error,
        }
    if not raw_error or raw_error == "尚未执行":
        return None
    return diagnose_error(raw_error)


def _statuses_with_diagnostics(statuses: list[dict]) -> list[dict]:
    display_rows: list[dict] = []
    for status in statuses:
        row = dict(status)
        diagnostic = _diagnostic_from_record(row)
        if diagnostic and (
            int(row.get("失败尝试", 0) or 0) > 0
            or str(row.get("状态", "") or "") in {"失败", "部分成功"}
        ):
            row["错误来源"] = diagnostic["错误来源"]
            row["失败原因"] = diagnostic["失败原因"]
            row["建议操作"] = diagnostic["建议操作"]
            row.pop("技术详情", None)
            row["最近错误"] = f"{diagnostic['错误来源']}：{diagnostic['失败原因']}"
        display_rows.append(row)
    return display_rows


def _render_error_diagnostic(
    record: dict | None,
    *,
    title: str = "查看明确失败原因",
    expanded: bool = True,
) -> bool:
    diagnostic = _diagnostic_from_record(record)
    if not diagnostic:
        return False
    with st.expander(title, expanded=expanded):
        st.markdown(f"**错误来源：** {diagnostic['错误来源']}")
        st.markdown(f"**失败原因：** {diagnostic['失败原因']}")
        st.markdown(f"**建议操作：** {diagnostic['建议操作']}")
        if diagnostic["技术详情"]:
            st.caption("脱敏后的技术详情")
            st.code(diagnostic["技术详情"], language=None)
    return True

PRESET_SUB_DOMAINS = {
    "计算机": [
        "人工智能",
        "大语言模型与对齐技术",
        "自然语言处理",
        "机器学习与深度学习",
        "计算机视觉与多模态学习",
        "具身智能与强化学习",
        "知识图谱与信息检索",
        "网络空间安全",
        "数据库与数据管理",
        "分布式系统与云计算",
    ],
    "物理": [
        "量子计算与量子信息",
        "凝聚态物理",
        "拓扑量子物态",
        "高能粒子物理",
        "宇宙学与引力物理",
        "光学与光子学",
        "等离子体物理",
        "原子分子与光物理",
        "材料物理",
        "统计物理与复杂系统",
    ],
    "医学": [
        "肿瘤免疫与靶向治疗",
        "心血管疾病与精准医学",
        "神经退行性疾病",
        "临床流行病学",
        "医学影像与AI辅助诊断",
        "药物发现与转化医学",
        "基因组医学",
        "公共卫生与卫生政策",
        "再生医学与干细胞",
        "感染病与疫苗研发",
    ],
    "生物": [
        "基因组学与单细胞组学",
        "蛋白质结构与功能",
        "合成生物学",
        "系统生物学",
        "神经生物学",
        "免疫学",
        "发育生物学",
        "微生物组学",
        "生物信息学",
        "植物科学与作物遗传",
    ],
    "农业": [
        "高效种养殖技术",
        "现代农业信息技术",
        "作物遗传育种与种质资源",
        "农业工程与智能农机",
        "农业资源环境与可持续生产",
    ],
}

DEFAULT_SUB_DOMAINS = [
    "基础理论",
    "核心技术",
    "前沿应用",
    "交叉创新",
    "产业转化",
]

EXPERTS_PER_ROUND = 10
DEFAULT_TOTAL_EXPERTS = max(1, int(os.environ.get("TOTAL_TARGET_EXPERTS", "150")))
MAX_TOTAL_EXPERTS = max(
    DEFAULT_TOTAL_EXPERTS,
    int(os.environ.get("MAX_TOTAL_TARGET_EXPERTS", "5000")),
)
LLM_CIRCUIT_BREAKER_THRESHOLD = max(
    1,
    int(os.environ.get("SUBDOMAIN_LLM_CIRCUIT_BREAKER_THRESHOLD", "3")),
)


def build_search_round_conditions(target_expert_count: int) -> list[str]:
    """按细分领域目标人数构造分层检索轮次，每轮固定查询 10 位。"""
    round_focuses = [
        "顶尖权威学者，如院士、最高奖项获得者和领域奠基人",
        "高影响力学者，如高被引研究者、重要学术组织 Fellow 和资深教授",
        "杰出中坚研究者，如重点实验室负责人、项目负责人和活跃学术带头人",
        "优秀青年与新兴方向领军学者，如青年 Fellow、重要青年奖项获得者",
        "跨机构、产业转化和国际合作中具有代表性的高质量专家",
        "此前轮次尚未覆盖的跨国家、跨区域高质量专家，优先核验其当前任职和领域相关性",
        "对遗漏候选进行补充复核，重点寻找具备权威主页、学术数据库或奖项名录证据的专家",
        "来自不同国家和区域重点科研机构的代表性专家，扩大地域与机构覆盖面",
        "与该领域直接相关的交叉学科专家，要求提供明确成果或项目证据",
        "权威人才名录、重要学术会议和专业协会中此前未覆盖的高质量专家",
    ]
    round_count = (max(0, int(target_expert_count)) + EXPERTS_PER_ROUND - 1) // EXPERTS_PER_ROUND
    conditions = []
    for round_index in range(round_count):
        focus = (
            round_focuses[round_index]
            if round_index < len(round_focuses)
            else "此前轮次未覆盖、但具有直接领域证据的高质量专家"
        )
        conditions.append(
            f"第 {round_index + 1} 轮：检索 {EXPERTS_PER_ROUND} 位{focus}；"
            "必须排除该细分领域此前轮次已经出现的专家"
        )
    return conditions


def get_static_sub_domain_options(main_domain: str) -> list[str]:
    for keyword, options in PRESET_SUB_DOMAINS.items():
        if keyword in main_domain:
            return options
    return DEFAULT_SUB_DOMAINS

def safe_filename_part(value: str) -> str:
    """
    将用户输入的大领域转换为安全的 Windows 文件名片段。
    """
    safe_value = re.sub(r'[\\/:*?"<>|]+', "_", str(value).strip())
    safe_value = re.sub(r"\s+", "_", safe_value)
    return safe_value.strip("._") or "专家检索"


def cleanup_stale_batch_files(output_dir: str) -> int:
    """只清理历史中间批次文件，绝不删除最终汇总表。"""
    removed = 0
    if not os.path.isdir(output_dir):
        return removed
    for entry in os.scandir(output_dir):
        if not entry.is_file() or not re.fullmatch(r"Expert_Data_\d{8}_\d{6}\.xlsx", entry.name):
            continue
        try:
            os.remove(entry.path)
            removed += 1
        except OSError:
            # 文件可能正在被 Excel/WPS 占用；下次运行时再次清理。
            continue
    return removed


def parse_extra_data_source_urls(value: str) -> list[str]:
    urls = []
    for match in re.findall(r"https?://[^\s,，;；]+", str(value or "")):
        url = match.strip().rstrip("。)")
        if url and url not in urls:
            urls.append(url)
    return urls


def read_batch_expert_names(excel_path: str) -> list[str]:
    """读取一个临时批次中的真实专家姓名，供跨梯队排重和补位统计使用。"""
    with pd.ExcelFile(excel_path) as workbook:
        sheet_name = "评价与验证" if "评价与验证" in workbook.sheet_names else workbook.sheet_names[0]
        df = remove_non_expert_rows(pd.read_excel(workbook, sheet_name=sheet_name))
    if df.empty or "专家姓名" not in df.columns:
        return []
    return list(
        dict.fromkeys(
            str(name).strip()
            for name in df["专家姓名"].fillna("").tolist()
            if str(name).strip()
        )
    )


def new_batch_expert_names(excel_path: str, excluded_name_keys: set[str]) -> list[str]:
    """返回批次中相对于当前细分领域历史名单真正新增的专家。"""
    new_names = []
    for name in read_batch_expert_names(excel_path):
        key = normalize_expert_name_for_dedupe(name)
        if key and key not in excluded_name_keys:
            excluded_name_keys.add(key)
            new_names.append(name)
    return new_names


def expert_names_for_subdomain(df: pd.DataFrame, subdomain: str) -> list[str]:
    """从历史最终表中恢复某个细分领域已经获得的专家姓名。"""
    if df.empty or "专家姓名" not in df.columns or "细分领域" not in df.columns:
        return []
    target = str(subdomain or "").strip()
    if not target:
        return []
    matches = df[
        df["细分领域"].fillna("").astype(str).apply(
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


def select_balanced_top_experts(
    df: pd.DataFrame,
    sub_domains: list[str],
    target_total: int,
) -> pd.DataFrame:
    """
    最终总表最多输出 target_total 人，并尽量在已选细分领域之间均衡分配名额。
    调用方传入用户设置的本次任务专家总人数。
    """
    if df.empty or len(df) <= target_total:
        return df
    if "细分领域" not in df.columns or not sub_domains:
        return df.head(target_total).copy()

    selected_indices = []
    base_quota, remainder = divmod(target_total, len(sub_domains))
    for index, sub_domain in enumerate(sub_domains):
        quota = base_quota + (1 if index < remainder else 0)
        target_sub_domain = str(sub_domain).strip()
        matches = df[
            df["细分领域"].fillna("").astype(str).apply(
                lambda value: target_sub_domain
                in [item.strip() for item in re.split(r"[；;、|/\n]+", value) if item.strip()]
            )
        ]
        selected_indices.extend(matches.head(quota).index.tolist())

    selected_indices = list(dict.fromkeys(selected_indices))
    if len(selected_indices) < target_total:
        remaining = df.loc[~df.index.isin(selected_indices)]
        selected_indices.extend(remaining.head(target_total - len(selected_indices)).index.tolist())

    return df.loc[selected_indices[:target_total]].sort_values(
        "评价_TotalScore",
        ascending=False,
        na_position="last",
    )


def render_table_completion_mode() -> None:
    st.markdown("### 📋 已有专家表格信息补全")
    st.caption(
        "上传包含专家姓名且部分字段缺失的 Excel。系统会保留原有非空内容，"
        "调用公开学术数据库、网页与主页工具，再由补全研究员、验证员和纠错员完成回填。"
    )
    domain_hint = st.text_input(
        "表格所属领域（建议填写）",
        placeholder="例如：脑机接口、人工智能、作物遗传育种",
        key="completion_domain_hint",
        help="用于约束同名消歧、领域相关性和外部检索；留空时将使用上传文件名作为检索提示。",
    )
    uploaded_workbook = st.file_uploader(
        "上传待补全的 Excel 表格",
        type=["xlsx"],
        accept_multiple_files=False,
        key="completion_workbook",
        help="支持表头不在第一行、标题合并单元格及中英文姓名/单位。",
    )
    if uploaded_workbook is not None:
        st.caption(f"已选择：{uploaded_workbook.name}")

    start_completion = st.button(
        "✨ 启动专家信息补全",
        type="primary",
        use_container_width=True,
        key="start_table_completion",
    )
    if start_completion:
        if uploaded_workbook is None:
            st.warning("请先上传一份待补全的 .xlsx 文件。")
        else:
            status_box = st.status("正在准备表格信息补全...", expanded=True)

            def report_progress(message: str) -> None:
                status_box.write(message)

            output_path, error_message, summary = run_table_completion(
                uploaded_workbook.getvalue(),
                uploaded_workbook.name,
                domain_hint=domain_hint,
                output_dir="Expert_Results",
                progress_callback=report_progress,
            )
            if output_path and os.path.exists(output_path):
                status_box.update(label="专家信息补全完成", state="complete", expanded=False)
                st.session_state.table_completion_result = {
                    "path": output_path,
                    "summary": summary or {},
                }
            else:
                status_box.update(label="专家信息补全失败", state="error", expanded=True)
                completion_error = error_message or "Excel 补全流程未生成有效的结果文件"
                diagnostic = diagnose_error(completion_error, stage="Excel 信息补全")
                st.error(f"专家信息补全失败：{diagnostic['失败原因']}")
                _render_error_diagnostic(diagnostic, title="查看补全失败原因")

    result = st.session_state.get("table_completion_result")
    if result and os.path.exists(result.get("path", "")):
        summary = result.get("summary", {})
        st.success(
            f"已处理 {summary.get('total_rows', 0)} 位专家，"
            f"为 {summary.get('completed_rows', 0)} 行补全 "
            f"{summary.get('filled_cells', 0)} 个空白字段。"
        )
        with open(result["path"], "rb") as completed_file:
            st.download_button(
                "📥 下载补全后的 Excel 表格",
                data=completed_file,
                file_name=os.path.basename(result["path"]),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="download_completed_workbook",
            )

# 1. 页面基本设置
st.set_page_config(page_title="全球人才信息检索系统", page_icon="🌐", layout="centered")

# 2. 页面标题与说明
brand_icon_path = Path(__file__).resolve().parent / "docs" / "assets" / "wisesearch-talent-icon.png"
brand_icon_base64 = base64.b64encode(brand_icon_path.read_bytes()).decode("ascii")
brand_icon_html = (
    f'<img src="data:image/png;base64,{brand_icon_base64}" '
    'alt="全球人才信息检索系统图标">'
)

st.markdown(
    """
    <style>
        .block-container {
            max-width: 760px;
            padding-top: 6.4rem;
            padding-bottom: 4rem;
        }

        .app-title {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 14px;
            margin-bottom: 14px;
        }

        .app-title .title-icon {
            display: flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
        }

        .app-title .title-icon img {
            display: block;
            width: 76px;
            height: 58px;
            object-fit: contain;
        }

        .app-title .title-text {
            color: #1f2937;
            font-size: 30px;
            font-weight: 800;
            letter-spacing: 0;
            line-height: 1.15;
        }

        .app-subtitle {
            color: #111827;
            font-size: 16px;
            line-height: 1.65;
            text-align: center;
            margin: 0 0 48px 0;
        }

        hr {
            margin: 0 0 34px 0;
        }

        div[data-testid="stTextInput"] label,
        div[data-testid="stNumberInput"] label,
        div[data-testid="stCheckbox"] label {
            color: #111827;
            font-size: 15px;
        }

        div[data-testid="stExpander"] {
            border-radius: 7px;
        }

        div[data-testid="stExpander"] details summary {
            min-height: 38px;
        }

        div.stButton > button {
            height: 41px;
            border-radius: 7px;
            font-size: 16px;
            font-weight: 600;
            color: #ffffff;
            background-color: #2563eb;
            border: 1px solid #2563eb;
            box-shadow: 0 2px 6px rgba(37, 99, 235, 0.22);
        }

        div.stButton > button:hover {
            color: #ffffff;
            background-color: #1d4ed8;
            border-color: #1d4ed8;
        }

        div.stButton > button:focus:not(:active) {
            color: #ffffff;
            background-color: #2563eb;
            border-color: #2563eb;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.2);
        }

        div[data-testid="stHorizontalBlock"] {
            gap: 1rem;
        }
    </style>

    <div class="app-title">
        <div class="title-icon">__BRAND_ICON__</div>
        <div class="title-text">全球人才信息检索系统</div>
    </div>
    <p class="app-subtitle">
        请输入您想探索的学科领域，多智能体系统将为您深度挖掘并匹配该领域全球人才信息。
    </p>
    """.replace("__BRAND_ICON__", brand_icon_html),
    unsafe_allow_html=True,
)
st.divider()

feature_mode = st.segmented_control(
    "功能模式",
    options=["全球人才检索", "已有表格信息补全"],
    default="全球人才检索",
    selection_mode="single",
    label_visibility="collapsed",
    width="stretch",
    key="feature_mode",
)
if feature_mode == "已有表格信息补全":
    render_table_completion_mode()
    st.stop()

# 3. 矩阵式批量检索输入区
main_domain = st.text_input("📚 主要大领域", placeholder="例如：计算机科学、临床医学、物理学")
use_ai_recommendations = st.checkbox("让智能体为我生成细分子领域", value=False)
include_chinese_experts = st.checkbox(
    "是否需要国内专家",
    value=False,
    help="勾选后检索结果允许包含中国大陆、港澳台专家；不勾选时系统会尽量避开国内专家。",
)
requested_total_experts_input = int(
    st.number_input(
        "本次任务需要的专家总人数",
        min_value=1,
        max_value=MAX_TOTAL_EXPERTS,
        value=DEFAULT_TOTAL_EXPERTS,
        step=10,
        help=(
            "系统会把总人数尽量均匀分配到所有已选细分领域，"
            f"再按每轮固定 {EXPERTS_PER_ROUND} 位自动计算每个分支的查询轮次。"
        ),
    )
)
st.caption(
    f"每轮固定查询 {EXPERTS_PER_ROUND} 位；选择细分领域后将自动计算各分支人数和轮次。"
)
if "农业" in main_domain and not include_chinese_experts:
    st.warning("当前农业项目反馈要求结果中必须包含中国专家，请勾选“是否需要国内专家”。")

if "last_ai_domain" not in st.session_state:
    st.session_state.last_ai_domain = ""
if "ai_preset_options" not in st.session_state:
    st.session_state.ai_preset_options = []
if "last_task_result" not in st.session_state:
    st.session_state.last_task_result = None

if main_domain and use_ai_recommendations and main_domain != st.session_state.last_ai_domain:
    with st.spinner(f'🤖 智能体正在为您测算【{main_domain}】的细分子领域...'):
        st.session_state.ai_preset_options = get_dynamic_recommendations(main_domain)
        st.session_state.last_ai_domain = main_domain
elif not main_domain:
    st.session_state.ai_preset_options = []
    st.session_state.last_ai_domain = ""

preset_options = (
    st.session_state.ai_preset_options
    if main_domain and use_ai_recommendations and st.session_state.ai_preset_options
    else get_static_sub_domain_options(main_domain) if main_domain else []
)
preset_source_label = "智能体生成的细分领域" if main_domain and use_ai_recommendations and st.session_state.ai_preset_options else "常用细分领域"

st.markdown("### 🔬 细分领域设置 (可多选及手填)")
col1, col2 = st.columns(2)

with col1:
    selected_presets = []
    with st.expander(f"✅ {preset_source_label} (可多选)", expanded=False):
        if preset_options:
            for i, option in enumerate(preset_options):
                checkbox_key = f"preset_{preset_source_label}_{main_domain}_{i}_{option}"
                if st.checkbox(option, key=checkbox_key):
                    selected_presets.append(option)
        else:
            st.checkbox("(请先在上方输入主要领域)", disabled=True, key="preset_disabled")

with col2:
    custom_inputs = []
    # 使用折叠面板保持界面整洁
    with st.expander("✏️ 自定义细分领域 (最多可填写 10 个)", expanded=False):
        for i in range(1, 11):
            val = st.text_input(f"自定义细分方向 {i}", placeholder=f"细分方向 {i}...", key=f"custom_{i}")
            if val.strip():
                custom_inputs.append(val.strip())

preview_sub_domains = list(dict.fromkeys(selected_presets + custom_inputs))
if preview_sub_domains:
    try:
        preview_workload = allocate_subdomain_workload(
            preview_sub_domains,
            requested_total_experts_input,
            EXPERTS_PER_ROUND,
        )
    except ValueError as workload_error:
        st.warning(str(workload_error))
    else:
        st.info(
            f"已选择 {len(preview_sub_domains)} 个细分领域，"
            f"目标总计 {requested_total_experts_input} 位专家；"
            f"每轮固定 {EXPERTS_PER_ROUND} 位，系统将按下表自动执行。"
        )
        with st.expander("查看各细分领域人数与轮次分配", expanded=False):
            st.dataframe(pd.DataFrame(preview_workload), hide_index=True, width="stretch")

with st.expander("🔗 其他数据源补充", expanded=False):
    extra_data_source_text = st.text_area(
        "补充数据源网址",
        placeholder="每行填写一个公开网页 URL，例如：https://example.edu/experts",
        height=96,
        key="extra_data_source_text",
    )
    extra_data_source_urls = parse_extra_data_source_urls(extra_data_source_text)
    if extra_data_source_urls:
        st.caption(f"已识别 {len(extra_data_source_urls)} 个补充数据源网址。")
    uploaded_supplemental_files = st.file_uploader(
        "上传补充数据文件",
        type=["pdf", "docx"],
        accept_multiple_files=True,
        help=(
            "支持 PDF 和 Word DOCX；所有合规文件都会先参与相关性筛选，再由文档压缩智能体整理。"
            "关键事实仍会经过公开来源核验。"
        ),
        key="uploaded_supplemental_files",
    )
    if uploaded_supplemental_files:
        st.caption(f"已选择 {len(uploaded_supplemental_files)} 个补充文件。")

submit_button = st.button(
    "🚀 启动全网深度批量检索",
    type="primary",
    use_container_width=True,
)

cleared_notice = str(st.session_state.pop("search_cleared_notice", "") or "")
if cleared_notice:
    st.success(cleared_notice)
stopped_notice = str(st.session_state.pop("search_stopped_notice", "") or "")
if stopped_notice:
    st.success(stopped_notice)

previous_task_result = st.session_state.last_task_result or {}
recoverable_checkpoint = None
resume_from_checkpoint = False
latest_checkpoint = load_latest_search_checkpoint()
if latest_checkpoint and str(latest_checkpoint.get("status", "") or "") != "cleared":
    checkpoint_job_id = str(latest_checkpoint.get("job_id", "") or "")
    checkpoint_is_running = search_job_is_running(checkpoint_job_id)
    checkpoint_domain = str(latest_checkpoint.get("main_domain", "") or "未命名领域")
    with st.expander("检索任务管理", expanded=False):
        st.markdown(f"**当前记录：** {checkpoint_domain}")
        if checkpoint_is_running:
            st.warning(
                "该检索仍在后台运行。可先停止 worker 并保留全部检查点，"
                "随后使用“恢复未完成任务”继续执行。"
            )
            confirm_stop = st.checkbox(
                "我确认停止任务，但保留当前进度",
                key=f"confirm_stop_search_{checkpoint_job_id}",
            )
            stop_search_button = st.button(
                "停止任务并保留进度",
                use_container_width=True,
                disabled=not confirm_stop,
                key=f"stop_search_{checkpoint_job_id}",
            )
            if stop_search_button:
                with st.spinner("正在安全停止后台任务并保存检查点..."):
                    stopped, stop_message = stop_search_job_preserving_progress(
                        checkpoint_job_id
                    )
                if stopped:
                    st.session_state.search_stopped_notice = stop_message
                    st.rerun()
                else:
                    st.error(stop_message)
        else:
            st.info(
                "可清空上一次检索的恢复进度和临时批次，以便开始一轮全新的检索。"
            )
        st.caption("Expert_Results 中已经生成的最终 Excel 不会被删除。")
        st.divider()
        confirm_clear = st.checkbox(
            "我确认结束并清空这次检索",
            key=f"confirm_clear_search_{checkpoint_job_id}",
        )
        clear_search_button = st.button(
            "结束并清空旧检索，开始新一轮",
            use_container_width=True,
            disabled=not confirm_clear,
            key=f"clear_search_{checkpoint_job_id}",
        )
        if clear_search_button:
            with st.spinner("正在安全停止旧任务并清理临时进度..."):
                cleared, clear_message = end_and_clear_search_job(checkpoint_job_id)
            if cleared:
                st.session_state.last_task_result = None
                st.session_state.pop("active_search_job_id", None)
                st.session_state.pop("retry_failed_requested", None)
                st.session_state.search_cleared_notice = clear_message
                st.rerun()
            else:
                st.error(clear_message)

if latest_checkpoint:
    previous_task_result = checkpoint_task_result(latest_checkpoint)
    if latest_checkpoint.get("status") in {
        "queued", "running", "interrupted", "partial", "failed"
    }:
        recoverable_checkpoint = latest_checkpoint
        resume_from_checkpoint = True
    elif latest_checkpoint.get("status") == "completed":
        st.session_state.last_task_result = previous_task_result
previous_statuses = previous_task_result.get("subdomain_statuses", [])
retry_candidates = retryable_subdomains(previous_statuses)
previous_retry_attempt_count = retry_attempt_count(previous_task_result)
retry_failed_button = bool(st.session_state.pop("retry_failed_requested", False))
if retry_candidates:
    checkpoint_still_running = bool(
        resume_from_checkpoint
        and recoverable_checkpoint
        and search_job_is_running(recoverable_checkpoint.get("job_id", ""))
    )
    if resume_from_checkpoint:
        checkpoint_subdomain = str(
            recoverable_checkpoint.get("current_subdomain", "") or ""
        )
        checkpoint_hint = (
            f"，上次运行停在“{checkpoint_subdomain}”"
            if checkpoint_subdomain
            else ""
        )
        if checkpoint_still_running:
            st.info(
                "原检索任务仍在服务器后台执行"
                f"{checkpoint_hint}。请不要重复启动；稍后刷新页面即可查看检查点状态。"
            )
        else:
            st.warning(
                "检测到浏览器断线或页面刷新前留下的未完成检索任务"
                f"{checkpoint_hint}。已完成批次已保存，可从检查点继续。"
            )
    else:
        st.warning(
            "上一次任务仍有未完整完成的细分领域："
            + "、".join(retry_candidates)
            + f"。已续跑 {previous_retry_attempt_count} 次，可继续多轮重试。"
        )
    retry_label = (
        f"↻ 恢复未完成任务：继续 {len(retry_candidates)} 个细分领域"
        if resume_from_checkpoint
        else f"↻ 第 {previous_retry_attempt_count + 1} 次续跑："
        f"仅重试未完成的 {len(retry_candidates)} 个细分领域"
    )
    retry_failed_button = st.button(
        retry_label,
        use_container_width=True,
        key="retry_failed_subdomains",
        disabled=checkpoint_still_running,
    ) or retry_failed_button


def _start_background_search() -> bool:
    selected_sub_domains = list(dict.fromkeys(selected_presets + custom_inputs))
    if retry_failed_button:
        checkpoint = recoverable_checkpoint
        if not checkpoint:
            checkpoint_job_id = str(
                previous_task_result.get("checkpoint_job_id", "") or ""
            )
            checkpoint = load_search_checkpoint(checkpoint_job_id) if checkpoint_job_id else None
        if not checkpoint:
            st.error("未找到上一次任务的持久化检查点，无法续跑。")
            return False
        if search_job_is_running(checkpoint.get("job_id", "")):
            st.info("该任务已经在后台运行，请勿重复启动。")
            return False
        candidates = retryable_subdomains(
            list(checkpoint.get("subdomain_statuses", []) or [])
        )
        if not candidates:
            st.info("该任务已经没有需要重试的细分领域。")
            return False
        checkpoint = mark_search_checkpoint(
            checkpoint,
            "queued",
            run_subdomains=candidates,
            retry_attempt_count=int(checkpoint.get("retry_attempt_count", 0) or 0) + 1,
            progress_message=f"已提交续跑任务，等待处理 {len(candidates)} 个细分领域",
            last_error="",
            error_source="",
            error_reason="",
            error_action="",
            error_detail="",
        )
    else:
        if latest_checkpoint and search_job_is_running(latest_checkpoint.get("job_id", "")):
            st.warning("已有检索任务正在后台运行，请等待其结束后再创建新任务。")
            return False
        task_main_domain = main_domain.strip()
        if not task_main_domain:
            st.warning("⚠️ 请先填写主要大领域！")
            return False
        if not selected_sub_domains:
            st.warning("⚠️ 请至少选择或填写一个细分领域！")
            return False
        try:
            workload = allocate_subdomain_workload(
                selected_sub_domains,
                requested_total_experts_input,
                EXPERTS_PER_ROUND,
            )
        except ValueError as workload_error:
            st.warning(f"⚠️ {workload_error}！")
            return False
        targets = {
            str(item["细分领域"]): int(item["目标人数"])
            for item in workload
        }
        document_context, document_names, document_warnings = (
            extract_supplemental_documents(uploaded_supplemental_files)
        )
        for warning in document_warnings:
            st.warning(f"补充文件提示：{warning}")
        initial_statuses = [
            {
                "细分领域": subdomain,
                "状态": "待重试",
                "实际人数": 0,
                "目标人数": targets[subdomain],
                "成功批次": 0,
                "失败尝试": 0,
                "最近错误": "尚未执行",
            }
            for subdomain in selected_sub_domains
        ]
        checkpoint = create_search_checkpoint(
            {
                "main_domain": task_main_domain,
                "requested_subdomains": selected_sub_domains,
                "run_subdomains": selected_sub_domains,
                "requested_total_experts": requested_total_experts_input,
                "subdomain_targets": targets,
                "subdomain_statuses": initial_statuses,
                "failed_subdomains": selected_sub_domains,
                "retry_attempt_count": 0,
                "experts_per_round": EXPERTS_PER_ROUND,
                "final_output_dir": str(Path("Expert_Results").resolve()),
                "include_chinese_experts": include_chinese_experts,
                "extra_data_source_urls": extra_data_source_urls,
                "supplemental_document_context": document_context,
                "supplemental_document_names": document_names,
                "progress_message": "任务已创建，等待后台进程启动",
            }
        )

    pid, launch_error = launch_search_job(checkpoint["job_id"])
    if launch_error or not pid:
        launch_message = launch_error or "后台检索进程未返回进程编号"
        diagnostic = diagnose_error(launch_message, stage="后台检索进程启动")
        st.error(f"后台检索任务启动失败：{diagnostic['失败原因']}")
        _render_error_diagnostic(diagnostic, title="查看启动失败原因")
        return False
    st.session_state.last_task_result = None
    st.session_state.active_search_job_id = checkpoint["job_id"]
    st.success(
        f"后台检索任务已启动（任务编号 {checkpoint['job_id']}）。"
        "现在可以刷新页面或暂时关闭浏览器，任务不会因此停止。"
    )
    return True


launch_requested = bool(submit_button or retry_failed_button)
if launch_requested and _start_background_search():
    st.rerun()


@st.fragment(run_every=5)
def render_background_search_status() -> None:
    checkpoint = load_latest_search_checkpoint()
    if not checkpoint:
        return
    status = str(checkpoint.get("status", "") or "")
    job_id = str(checkpoint.get("job_id", "") or "")
    is_running = search_job_is_running(job_id)
    if status in {"queued", "running"} and is_running:
        message = str(checkpoint.get("progress_message", "") or "后台检索正在执行")
        st.info(f"后台任务运行中：{message}")
        current = int(checkpoint.get("progress_current", 0) or 0)
        total = max(1, int(checkpoint.get("progress_total", 0) or 0))
        st.progress(min(1.0, current / total))
        statuses = list(checkpoint.get("subdomain_statuses", []) or [])
        if statuses:
            st.dataframe(
                pd.DataFrame(_statuses_with_diagnostics(statuses)),
                hide_index=True,
                width="stretch",
            )
        if str(checkpoint.get("last_error", "") or "").strip():
            _render_error_diagnostic(
                checkpoint,
                title="最近一次失败尝试的明确原因",
                expanded=False,
            )
        st.caption(
            "任务运行在独立后台进程中。刷新页面、关闭当前标签页或浏览器短暂断线都不会终止任务。"
        )
        return

    if status in {"queued", "running"} and not is_running:
        interrupted_error = "后台任务进程意外结束，可能被系统终止、服务重启或运行环境关闭"
        checkpoint = mark_search_checkpoint(
            checkpoint,
            "interrupted",
            progress_message="后台进程意外结束，已保存检查点，可继续重试",
            **checkpoint_error_updates(interrupted_error, stage="后台任务进程"),
        )
        status = "interrupted"

    if status in {"completed", "partial", "failed", "interrupted"}:
        message = str(checkpoint.get("progress_message", "") or "任务执行已结束")
        if status == "completed":
            st.success(message)
        elif status == "partial":
            st.warning(message)
        else:
            st.error(message)
        statuses = list(checkpoint.get("subdomain_statuses", []) or [])
        if statuses:
            st.dataframe(
                pd.DataFrame(_statuses_with_diagnostics(statuses)),
                hide_index=True,
                width="stretch",
            )
        if status != "completed":
            diagnostic_rendered = _render_error_diagnostic(checkpoint)
            if not diagnostic_rendered:
                for item in reversed(statuses):
                    if _render_error_diagnostic(item):
                        break
        result_path = str(checkpoint.get("final_file_path", "") or "")
        if result_path and os.path.exists(result_path):
            with open(result_path, "rb") as result_file:
                st.download_button(
                    f"📥 下载最终 Excel（{int(checkpoint.get('expert_count', 0) or 0)} 位专家）",
                    data=result_file,
                    file_name=os.path.basename(result_path),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    key=f"background_download_{job_id}",
                )
        candidates = retryable_subdomains(statuses)
        if candidates and not is_running:
            if st.button(
                f"↻ 仅重试未完成的 {len(candidates)} 个细分领域",
                use_container_width=True,
                key=f"background_retry_{job_id}_{int(checkpoint.get('retry_attempt_count', 0) or 0)}",
            ):
                st.session_state.retry_failed_requested = True
                st.rerun()


render_background_search_status()

# 原页面线程内执行逻辑保留为迁移参考，但不再启用。长任务统一交给独立后台进程。
_legacy_inline_execution = False

# 4. 点击按钮后的核心逻辑
if _legacy_inline_execution and (submit_button or retry_failed_button):
    # 将用户勾选的预设 + 填写的自定义合并为一个总的任务清单
    selected_sub_domains = list(dict.fromkeys(selected_presets + custom_inputs))
    workload_error_message = ""
    if retry_failed_button:
        current_retry_attempt_count = previous_retry_attempt_count + 1
        final_sub_domains = retry_candidates
        requested_sub_domains = list(
            dict.fromkeys(
                previous_task_result.get("requested_subdomains", [])
                or selected_sub_domains
                or retry_candidates
            )
        )
        task_main_domain = str(
            previous_task_result.get("main_domain", "") or main_domain
        ).strip()
        resume_master_path = str(previous_task_result.get("file_path", "") or "")

        stored_targets = {
            str(key): int(value)
            for key, value in dict(
                previous_task_result.get("subdomain_targets", {}) or {}
            ).items()
            if str(key).strip() and int(value) > 0
        }
        if not stored_targets:
            stored_targets = {
                str(item.get("细分领域", "")).strip(): int(item.get("目标人数", 0) or 0)
                for item in previous_statuses
                if str(item.get("细分领域", "")).strip()
                and int(item.get("目标人数", 0) or 0) > 0
            }
        if all(subdomain in stored_targets for subdomain in requested_sub_domains):
            task_subdomain_workload = [
                {
                    "细分领域": subdomain,
                    "目标人数": stored_targets[subdomain],
                    "查询轮次": (
                        stored_targets[subdomain] + EXPERTS_PER_ROUND - 1
                    ) // EXPERTS_PER_ROUND,
                }
                for subdomain in requested_sub_domains
            ]
            task_total_experts = sum(
                stored_targets[subdomain] for subdomain in requested_sub_domains
            )
        else:
            task_total_experts = int(
                previous_task_result.get("requested_total_experts", 0)
                or requested_total_experts_input
            )
            try:
                task_subdomain_workload = allocate_subdomain_workload(
                    requested_sub_domains,
                    task_total_experts,
                    EXPERTS_PER_ROUND,
                )
            except ValueError as workload_error:
                task_subdomain_workload = []
                workload_error_message = str(workload_error)
    else:
        current_retry_attempt_count = 0
        final_sub_domains = selected_sub_domains
        requested_sub_domains = list(final_sub_domains)
        task_main_domain = main_domain.strip()
        resume_master_path = ""
        task_total_experts = requested_total_experts_input
        try:
            task_subdomain_workload = allocate_subdomain_workload(
                requested_sub_domains,
                task_total_experts,
                EXPERTS_PER_ROUND,
            )
        except ValueError as workload_error:
            task_subdomain_workload = []
            workload_error_message = str(workload_error)

    subdomain_targets = {
        str(item["细分领域"]): int(item["目标人数"])
        for item in task_subdomain_workload
    }
    active_checkpoint = (
        recoverable_checkpoint
        if retry_failed_button and resume_from_checkpoint and recoverable_checkpoint
        else None
    )
    
    if not task_main_domain:
        st.warning("⚠️ 请先填写主要大领域！")
    elif len(final_sub_domains) == 0:
        st.warning("⚠️ 请至少勾选一个常用细分领域，或在右侧填写至少一个自定义领域！")
    elif workload_error_message:
        st.warning(f"⚠️ {workload_error_message}！")
    else:
        if not retry_failed_button:
            st.session_state.last_task_result = None
        if active_checkpoint:
            include_chinese_experts = bool(
                active_checkpoint.get("include_chinese_experts", False)
            )
            extra_data_source_urls = list(
                active_checkpoint.get("extra_data_source_urls", []) or []
            )
            supplemental_document_context = str(
                active_checkpoint.get("supplemental_document_context", "") or ""
            )
            supplemental_document_names = list(
                active_checkpoint.get("supplemental_document_names", []) or []
            )
            document_warnings = []
            active_checkpoint = mark_search_checkpoint(
                active_checkpoint,
                "running",
                retry_attempt_count=current_retry_attempt_count,
            )
        else:
            supplemental_document_context, supplemental_document_names, document_warnings = (
                extract_supplemental_documents(uploaded_supplemental_files)
            )
            initial_statuses = [
                {
                    "细分领域": subdomain,
                    "状态": "待重试",
                    "实际人数": 0,
                    "目标人数": subdomain_targets[subdomain],
                    "成功批次": 0,
                    "失败尝试": 0,
                    "最近错误": "尚未执行",
                }
                for subdomain in requested_sub_domains
            ]
            active_checkpoint = create_search_checkpoint(
                {
                    "main_domain": task_main_domain,
                    "requested_subdomains": requested_sub_domains,
                    "requested_total_experts": task_total_experts,
                    "subdomain_targets": subdomain_targets,
                    "subdomain_statuses": initial_statuses,
                    "failed_subdomains": requested_sub_domains,
                    "retry_attempt_count": current_retry_attempt_count,
                    "experts_per_round": EXPERTS_PER_ROUND,
                    "include_chinese_experts": include_chinese_experts,
                    "extra_data_source_urls": extra_data_source_urls,
                    "supplemental_document_context": supplemental_document_context,
                    "supplemental_document_names": supplemental_document_names,
                }
            )
        register_search_job(active_checkpoint["job_id"])
        for warning in document_warnings:
            st.warning(f"补充文件提示：{warning}")
        if supplemental_document_names:
            st.success(
                "已提取补充文件正文，将由文档压缩智能体整理后交给研究员节点："
                + "、".join(supplemental_document_names)
            )
        expert_scope_label = "包含国内专家" if include_chinese_experts else "避开国内专家"
        st.info(
            f"🧠 **批量指令已下达**：即将对【{task_main_domain}】下的 "
            f"{len(final_sub_domains)} 个细分方向进行深度挖掘！"
            f"原任务总目标 {task_total_experts} 位、每轮固定 {EXPERTS_PER_ROUND} 位，"
            f"检索范围：{expert_scope_label}"
        )
        st.write("**检索任务清单：**", "、".join(final_sub_domains))
        current_workload = [
            item
            for item in task_subdomain_workload
            if item["细分领域"] in final_sub_domains
        ]
        st.dataframe(pd.DataFrame(current_workload), hide_index=True, width="stretch")
        
        start_time = time.time()
        with st.spinner('🤖 多智能体系统正在并行检索中，请稍候...（批量检索可能需要较长时间）'):
            try:
                # ==========================================
                # 【核心接入区】：
                
                # 最终总表写入 Expert_Results；各梯队文件只写入系统临时目录。
                output_dir = "Expert_Results"
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                cleanup_stale_batch_files(output_dir)
                batch_output_dir = str(active_checkpoint["batch_output_dir"])
                os.makedirs(batch_output_dir, exist_ok=True)
                batch_files = [
                    str(path)
                    for path in active_checkpoint.get("batch_files", []) or []
                    if os.path.exists(str(path))
                ]
                resume_df = pd.DataFrame()
                resume_paths = list(batch_files)
                if retry_failed_button and resume_master_path and os.path.exists(resume_master_path):
                    if resume_master_path not in batch_files:
                        batch_files.append(resume_master_path)
                    resume_paths.append(resume_master_path)
                if resume_paths:
                    resume_frames = []
                    for resume_path in dict.fromkeys(resume_paths):
                        try:
                            with pd.ExcelFile(resume_path) as workbook:
                                resume_sheet = (
                                    "评价与验证"
                                    if "评价与验证" in workbook.sheet_names
                                    else workbook.sheet_names[0]
                                )
                                resume_frame = remove_non_expert_rows(
                                    pd.read_excel(workbook, sheet_name=resume_sheet)
                                )
                            if not resume_frame.empty:
                                resume_frames.append(resume_frame)
                        except Exception as resume_error:
                            st.warning(
                                f"读取检查点批次作为续跑基线失败：{resume_error}"
                            )
                    if resume_frames:
                        resume_df = pd.concat(resume_frames, ignore_index=True)

                progress_bar = st.progress(0)
                total_sub_domains = len(final_sub_domains)
                tier_recovery_attempts = max(
                    0, int(os.environ.get("SUBDOMAIN_TIER_RECOVERY_ATTEMPTS", "2"))
                )
                final_topup_attempts = max(
                    0, int(os.environ.get("SUBDOMAIN_FINAL_TOPUP_ATTEMPTS", "3"))
                )
                subdomain_statuses = []
                consecutive_llm_failures = 0
                circuit_open = False
                circuit_reason = ""

                for i, sub in enumerate(final_sub_domains):
                    experts_per_subdomain = subdomain_targets[sub]
                    batch_conditions = build_search_round_conditions(experts_per_subdomain)
                    active_checkpoint = mark_search_checkpoint(
                        active_checkpoint,
                        "running",
                        current_subdomain=sub,
                        current_round=0,
                    )
                    st.toast(
                        f"正在启动 {sub} 方向的 {len(batch_conditions)} 轮分层检索，"
                        f"目标共 {experts_per_subdomain} 位、每轮固定 {EXPERTS_PER_ROUND} 位...",
                        icon="🚀",
                    )
                    subdomain_names = expert_names_for_subdomain(resume_df, sub)
                    subdomain_name_keys = {
                        normalize_expert_name_for_dedupe(name)
                        for name in subdomain_names
                        if normalize_expert_name_for_dedupe(name)
                    }
                    successful_attempts = 0
                    failed_attempts = 0
                    error_messages = []

                    # 针对当前细分领域逐轮检索；每轮不足目标人数时自动补位。
                    for batch_idx, condition in enumerate(batch_conditions):
                        if len(subdomain_names) >= experts_per_subdomain:
                            break
                        st.info(f"🔎 正在挖掘：【{sub}】 - {condition}")
                        tier_new_names = []
                        round_target = EXPERTS_PER_ROUND

                        for attempt in range(tier_recovery_attempts + 1):
                            remaining = round_target - len(tier_new_names)
                            if remaining <= 0:
                                break

                            scope_instruction = (
                                "本次检索需要包含国内专家，中国大陆、香港、澳门、台湾的顶级专家均可进入候选名单。"
                                if include_chinese_experts
                                else "本次检索不需要国内专家，请避开中国大陆、香港、澳门、台湾当前主要任职机构的专家。"
                            )
                            recovery_instruction = (
                                ""
                                if attempt == 0
                                else f"这是第 {attempt} 次补位检索，前次有效新增不足，请补充 {remaining} 位此前未出现的人选。"
                            )
                            query = (
                                f"{task_main_domain}领域下的{sub}方向顶级专家信息。{scope_instruction}"
                                f"当前检索目标：{condition}。{recovery_instruction}"
                                f"必须严格查出 {remaining} 个此前未出现的专家，并输出 Markdown 表格。"
                            )

                            excel_path, task_error = run_agent_task(
                                query,
                                include_chinese_experts=include_chinese_experts,
                                extra_data_source_urls=extra_data_source_urls,
                                supplemental_document_context=supplemental_document_context,
                                supplemental_document_names=supplemental_document_names,
                                excluded_expert_names=subdomain_names,
                                target_expert_count=remaining,
                                output_dir=batch_output_dir,
                            )
                            if excel_path and os.path.exists(excel_path):
                                batch_files.append(excel_path)
                                active_checkpoint = record_batch_file(
                                    active_checkpoint,
                                    excel_path,
                                    current_subdomain=sub,
                                    current_round=batch_idx + 1,
                                )
                                successful_attempts += 1
                                consecutive_llm_failures = 0
                                try:
                                    added_names = new_batch_expert_names(
                                        excel_path, subdomain_name_keys
                                    )
                                except Exception as batch_error:
                                    added_names = []
                                    st.warning(
                                        f"⚠️ 【{sub}】第 {batch_idx + 1} 检索轮次批次已生成，"
                                        f"但新增人数统计失败：{batch_error}"
                                    )
                                tier_new_names.extend(added_names)
                                subdomain_names.extend(added_names)
                                st.success(
                                    f"✅ 【{sub}】第 {batch_idx + 1} 检索轮次第 {attempt + 1} 次尝试"
                                    f"新增 {len(added_names)} 位，本轮累计 "
                                    f"{len(tier_new_names)}/{round_target} 位，"
                                    f"该细分领域累计 {len(subdomain_names)}/{experts_per_subdomain} 位。"
                                )
                            else:
                                error_detail = (
                                    f"原因：{task_error}"
                                    if task_error
                                    else "原因：后端未返回有效文件路径。"
                                )
                                st.error(
                                    f"⚠️ 【{sub}】第 {batch_idx + 1} 检索轮次第 {attempt + 1} 次尝试失败。"
                                    f"{error_detail}"
                                )
                                failed_attempts += 1
                                error_messages.append(task_error or "后端未返回有效文件路径")
                                if is_llm_connection_failure(task_error):
                                    consecutive_llm_failures += 1
                                else:
                                    consecutive_llm_failures = 0
                                if consecutive_llm_failures >= LLM_CIRCUIT_BREAKER_THRESHOLD:
                                    circuit_open = True
                                    circuit_reason = (
                                        f"连续 {consecutive_llm_failures} 次 LLM/API 连接失败，"
                                        "已暂停后续细分领域，避免继续消耗调用。"
                                    )
                                    st.error(f"⏸️ {circuit_reason}")
                                    break

                        if circuit_open:
                            break

                        if len(tier_new_names) < round_target:
                            st.warning(
                                f"【{sub}】本轮检索在补位后实际新增 "
                                f"{len(tier_new_names)}/{round_target} 位，"
                                "稍后将执行细分领域最终补位。"
                            )
                        
                        # 细粒度更新进度条 (当前细分领域进度 + 当前梯队进度)
                        current_progress = (i + (batch_idx + 1) / len(batch_conditions)) / total_sub_domains
                        progress_bar.progress(min(current_progress, 1.0))

                    # 综合检索结束后，对整个细分领域继续补齐到目标人数。
                    for topup_attempt in range(final_topup_attempts):
                        if circuit_open:
                            break
                        remaining = experts_per_subdomain - len(subdomain_names)
                        if remaining <= 0:
                            break
                        requested = EXPERTS_PER_ROUND
                        st.info(
                            f"🧩 【{sub}】正在执行最终补位第 {topup_attempt + 1} 次，"
                            f"还缺 {remaining} 位，本次目标 {requested} 位。"
                        )
                        query = (
                            f"{task_main_domain}领域下的{sub}方向顶级专家信息。"
                            "请寻找此前检索轮次尚未覆盖、但与该细分领域有直接证据关联的高质量专家。"
                            f"必须严格补充 {requested} 个此前未出现的专家，并输出 Markdown 表格。"
                        )
                        excel_path, task_error = run_agent_task(
                            query,
                            include_chinese_experts=include_chinese_experts,
                            extra_data_source_urls=extra_data_source_urls,
                            supplemental_document_context=supplemental_document_context,
                            supplemental_document_names=supplemental_document_names,
                            excluded_expert_names=subdomain_names,
                            target_expert_count=requested,
                            output_dir=batch_output_dir,
                        )
                        if not excel_path or not os.path.exists(excel_path):
                            st.error(
                                f"⚠️ 【{sub}】最终补位第 {topup_attempt + 1} 次失败。"
                                f"原因：{task_error or '后端未返回有效文件路径。'}"
                            )
                            failed_attempts += 1
                            error_messages.append(task_error or "后端未返回有效文件路径")
                            if is_llm_connection_failure(task_error):
                                consecutive_llm_failures += 1
                            else:
                                consecutive_llm_failures = 0
                            if consecutive_llm_failures >= LLM_CIRCUIT_BREAKER_THRESHOLD:
                                circuit_open = True
                                circuit_reason = (
                                    f"连续 {consecutive_llm_failures} 次 LLM/API 连接失败，"
                                    "已暂停后续细分领域，避免继续消耗调用。"
                                )
                                st.error(f"⏸️ {circuit_reason}")
                                break
                            continue
                        batch_files.append(excel_path)
                        active_checkpoint = record_batch_file(
                            active_checkpoint,
                            excel_path,
                            current_subdomain=sub,
                            current_round=len(batch_conditions) + topup_attempt + 1,
                        )
                        successful_attempts += 1
                        consecutive_llm_failures = 0
                        try:
                            added_names = new_batch_expert_names(
                                excel_path, subdomain_name_keys
                            )
                        except Exception as batch_error:
                            added_names = []
                            st.warning(f"⚠️ 【{sub}】最终补位新增人数统计失败：{batch_error}")
                        subdomain_names.extend(added_names)
                        st.success(
                            f"✅ 【{sub}】最终补位新增 {len(added_names)} 位，"
                            f"当前累计 {len(subdomain_names)}/{experts_per_subdomain} 位。"
                        )

                    if len(subdomain_names) < experts_per_subdomain:
                        st.warning(
                            f"【{sub}】完成所有补位后获得 "
                            f"{len(subdomain_names)}/{experts_per_subdomain} 位互不重复专家。"
                            "这通常表示可验证候选不足或外部服务持续失败，最终表将保留已核验数据。"
                        )

                    if len(subdomain_names) >= experts_per_subdomain:
                        subdomain_status = "成功"
                    elif subdomain_names:
                        subdomain_status = "部分成功"
                    else:
                        subdomain_status = "失败"
                    subdomain_statuses.append(
                        {
                            "细分领域": sub,
                            "状态": subdomain_status,
                            "实际人数": len(subdomain_names),
                            "目标人数": experts_per_subdomain,
                            "成功批次": successful_attempts,
                            "失败尝试": failed_attempts,
                            "最近错误": "；".join(dict.fromkeys(error_messages[-3:])),
                        }
                    )
                    if circuit_open:
                        break

                completed_subdomains = {
                    item["细分领域"] for item in subdomain_statuses
                }
                for sub in final_sub_domains:
                    if sub in completed_subdomains:
                        continue
                    existing_count = len(expert_names_for_subdomain(resume_df, sub))
                    subdomain_statuses.append(
                        {
                            "细分领域": sub,
                            "状态": "待重试",
                            "实际人数": existing_count,
                            "目标人数": subdomain_targets[sub],
                            "成功批次": 0,
                            "失败尝试": 0,
                            "最近错误": circuit_reason or "尚未执行",
                        }
                    )

                merged_statuses = merge_subdomain_statuses(
                    requested_sub_domains,
                    previous_statuses if retry_failed_button else [],
                    subdomain_statuses,
                )
                active_checkpoint = mark_search_checkpoint(
                    active_checkpoint,
                    "running",
                    subdomain_statuses=merged_statuses,
                    failed_subdomains=retryable_subdomains(merged_statuses),
                    elapsed_seconds=int(time.time() - start_time),
                )
                st.markdown("#### 本次细分领域执行状态")
                st.dataframe(pd.DataFrame(merged_statuses), hide_index=True, use_container_width=True)

                # 2. 检索全部完成后，进行 Excel 文件的大合并
                new_files = set(batch_files)

                if new_files:
                    unfinished_subdomains = retryable_subdomains(merged_statuses)
                    if unfinished_subdomains:
                        st.warning(
                            f"本次任务部分完成：{len(unfinished_subdomains)} 个细分领域仍需重试。"
                            "正在整合目前已核验的结果。"
                        )
                    else:
                        st.success("🎉 所有领域的智能检索与交叉验证圆满完成！正在为您整合最终报表...")
                    
                    # 使用 Pandas 将新生成的独立表格纵向合并
                    df_list = []
                    for f in new_files:
                        try:
                            with pd.ExcelFile(f) as workbook:
                                sheet_name = "评价与验证" if "评价与验证" in workbook.sheet_names else workbook.sheet_names[0]
                                df = remove_non_expert_rows(pd.read_excel(workbook, sheet_name=sheet_name))
                            if not df.empty:
                                df_list.append(df)
                        except Exception as e:
                            pass
                    
                    if df_list:
                        # 初步合并所有数据
                        master_df = pd.concat(df_list, ignore_index=True)
                        original_count = len(master_df)
                        final_query = (
                            f"{task_main_domain}领域下的{'、'.join(requested_sub_domains)}方向顶级专家信息"
                        )
                        
                        # 先合并各批次的互补信息，再对所有信息严重缺失专家进行定向补全。
                        master_df = finalize_merged_experts(
                            master_df,
                            include_chinese_experts=include_chinese_experts,
                            query=final_query,
                        )
                        master_df = select_balanced_top_experts(
                            master_df,
                            requested_sub_domains,
                            target_total=task_total_experts,
                        )
                        
                        final_count = len(master_df)
                        duplicate_removed = original_count - final_count
                        
                        if duplicate_removed > 0:
                            st.warning(
                                f"🧹 触发最终整理：已通过范围筛选、近似去重与容量控制"
                                f"移除 {duplicate_removed} 条候选记录。"
                            )
                            
                        # 保存最终去重后的干净总表，命名为“大领域_日期_时间_批量专家总名单.xlsx”
                        result_timestamp = time.strftime("%Y%m%d_%H%M%S")
                        master_file_name = f"{safe_filename_part(task_main_domain)}_{result_timestamp}_批量专家总名单.xlsx"
                        master_file_path = os.path.join(output_dir, master_file_name)
                        write_expert_excel(
                            master_df,
                            master_file_path,
                            data_source_summary=data_source_summary(
                                extra_data_source_urls,
                                query=task_main_domain,
                                supplemental_document_names=supplemental_document_names,
                            ),
                            query=task_main_domain,
                            extra_data_source_urls=extra_data_source_urls,
                            supplemental_document_names=supplemental_document_names,
                            translate_delivery=True,
                            final_delivery_only=True,
                        )
                        
                        # 结束计时
                        end_time = time.time()
                        elapsed_seconds = int(end_time - start_time)
                        st.session_state.last_task_result = {
                            "elapsed_seconds": elapsed_seconds,
                            "expert_count": final_count,
                            "file_name": master_file_name,
                            "file_path": os.path.abspath(master_file_path),
                            "main_domain": task_main_domain,
                            "requested_subdomains": requested_sub_domains,
                            "subdomain_statuses": merged_statuses,
                            "failed_subdomains": retryable_subdomains(merged_statuses),
                            "retry_attempt_count": current_retry_attempt_count,
                            "requested_total_experts": task_total_experts,
                            "subdomain_targets": subdomain_targets,
                            "experts_per_round": EXPERTS_PER_ROUND,
                        }
                        active_checkpoint = mark_search_checkpoint(
                            active_checkpoint,
                            "partial" if unfinished_subdomains else "completed",
                            subdomain_statuses=merged_statuses,
                            failed_subdomains=unfinished_subdomains,
                            elapsed_seconds=elapsed_seconds,
                            expert_count=final_count,
                            final_file_name=master_file_name,
                            final_file_path=os.path.abspath(master_file_path),
                        )

                        # 完整完成后才清理批次；部分完成任务保留批次供断线续跑。
                        if not unfinished_subdomains:
                            shutil.rmtree(batch_output_dir, ignore_errors=True)
                    else:
                        elapsed_seconds = int(time.time() - start_time)
                        st.session_state.last_task_result = {
                            "elapsed_seconds": elapsed_seconds,
                            "expert_count": 0,
                            "file_name": "",
                            "file_path": "",
                            "main_domain": task_main_domain,
                            "requested_subdomains": requested_sub_domains,
                            "subdomain_statuses": merged_statuses,
                            "failed_subdomains": retryable_subdomains(merged_statuses),
                            "retry_attempt_count": current_retry_attempt_count,
                            "requested_total_experts": task_total_experts,
                            "subdomain_targets": subdomain_targets,
                            "experts_per_round": EXPERTS_PER_ROUND,
                        }
                        active_checkpoint = mark_search_checkpoint(
                            active_checkpoint,
                            "failed",
                            subdomain_statuses=merged_statuses,
                            failed_subdomains=retryable_subdomains(merged_statuses),
                            elapsed_seconds=elapsed_seconds,
                            last_error="中间批次未包含可合并的专家数据",
                        )
                        st.error("⚠️ 中间批次未包含可合并的专家数据，未生成最终结果表。")
                else:
                    elapsed_seconds = int(time.time() - start_time)
                    st.session_state.last_task_result = {
                        "elapsed_seconds": elapsed_seconds,
                        "expert_count": 0,
                        "file_name": "",
                        "file_path": "",
                        "main_domain": task_main_domain,
                        "requested_subdomains": requested_sub_domains,
                        "subdomain_statuses": merged_statuses,
                        "failed_subdomains": retryable_subdomains(merged_statuses),
                        "retry_attempt_count": current_retry_attempt_count,
                        "requested_total_experts": task_total_experts,
                        "subdomain_targets": subdomain_targets,
                        "experts_per_round": EXPERTS_PER_ROUND,
                    }
                    active_checkpoint = mark_search_checkpoint(
                        active_checkpoint,
                        "failed",
                        subdomain_statuses=merged_statuses,
                        failed_subdomains=retryable_subdomains(merged_statuses),
                        elapsed_seconds=elapsed_seconds,
                        last_error="未生成有效批次文件",
                    )
                    st.error("⚠️ 检索完成，但未能成功生成有效的数据文件，请检查大模型 API 或网络状态。")
            except Exception as e:
                if "merged_statuses" in locals():
                    elapsed_seconds = int(time.time() - start_time)
                    st.session_state.last_task_result = {
                        "elapsed_seconds": elapsed_seconds,
                        "expert_count": 0,
                        "file_name": "",
                        "file_path": "",
                        "main_domain": task_main_domain,
                        "requested_subdomains": requested_sub_domains,
                        "subdomain_statuses": merged_statuses,
                        "failed_subdomains": retryable_subdomains(merged_statuses),
                        "retry_attempt_count": current_retry_attempt_count,
                        "requested_total_experts": task_total_experts,
                        "subdomain_targets": subdomain_targets,
                        "experts_per_round": EXPERTS_PER_ROUND,
                    }
                if "active_checkpoint" in locals() and active_checkpoint:
                    active_checkpoint = mark_search_checkpoint(
                        active_checkpoint,
                        "interrupted",
                        subdomain_statuses=(
                            merged_statuses
                            if "merged_statuses" in locals()
                            else active_checkpoint.get("subdomain_statuses", [])
                        ),
                        failed_subdomains=(
                            retryable_subdomains(merged_statuses)
                            if "merged_statuses" in locals()
                            else active_checkpoint.get("failed_subdomains", [])
                        ),
                        elapsed_seconds=int(time.time() - start_time),
                        last_error=str(e),
                    )
                st.error(f"❌ 检索过程中出现底层异常: {e}")
            finally:
                if "active_checkpoint" in locals() and active_checkpoint:
                    unregister_search_job(active_checkpoint.get("job_id", ""))

last_task_result = st.session_state.last_task_result
if last_task_result:
    elapsed_seconds = int(last_task_result["elapsed_seconds"])
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    elapsed_parts = []
    if hours:
        elapsed_parts.append(f"{hours} 小时")
    if minutes or hours:
        elapsed_parts.append(f"{minutes} 分")
    elapsed_parts.append(f"{seconds} 秒")

    persisted_retry_subdomains = retryable_subdomains(
        last_task_result.get("subdomain_statuses", [])
    )
    persisted_retry_attempt_count = retry_attempt_count(last_task_result)
    if persisted_retry_subdomains:
        st.warning(
            f"⏱️ 本次执行已结束，用时 {' '.join(elapsed_parts)}；"
            f"仍有 {len(persisted_retry_subdomains)} 个细分领域未完整完成。"
            f"已续跑 {persisted_retry_attempt_count} 次。"
        )
    else:
        st.success(f"⏱️ 智能体检索完毕！本次任务执行总耗时：{' '.join(elapsed_parts)}")

    if last_task_result.get("subdomain_statuses") and not (
        submit_button or retry_failed_button
    ):
        st.markdown("#### 最近一次细分领域执行状态")
        st.dataframe(
            pd.DataFrame(last_task_result["subdomain_statuses"]),
            hide_index=True,
            use_container_width=True,
        )

    result_file_path = last_task_result.get("file_path", "")
    if os.path.exists(result_file_path):
        with open(result_file_path, "rb") as file:
            st.download_button(
                label=(
                    "📥 点击下载【全局大合并】Excel 报表 "
                    f"(共 {last_task_result['expert_count']} 位独立专家)"
                ),
                data=file,
                file_name=last_task_result["file_name"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
    elif result_file_path:
        st.warning("最近一次任务的结果文件已被移动或删除，暂时无法下载。")
    else:
        st.info("本次尚未生成最终 Excel，已保留失败细分领域清单。")

    if persisted_retry_subdomains and (submit_button or retry_failed_button):
        if st.button(
            f"↻ 第 {persisted_retry_attempt_count + 1} 次续跑："
            f"仅重试未完成的 {len(persisted_retry_subdomains)} 个细分领域",
            use_container_width=True,
            key="retry_failed_subdomains_after_run",
        ):
            st.session_state.retry_failed_requested = True
            st.rerun()
