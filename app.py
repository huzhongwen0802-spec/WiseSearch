# -*- coding: utf-8 -*-

import streamlit as st
import time
import os
import re
import shutil
import tempfile
import pandas as pd
from main import run_agent_task, get_dynamic_recommendations
from supplemental_documents import extract_supplemental_documents
from utils import (
    data_source_summary,
    finalize_merged_experts,
    normalize_expert_name_for_dedupe,
    remove_non_expert_rows,
    write_expert_excel,
)

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

DEMO_MODE = os.environ.get("EXPERTSEARCH_DEMO_MODE", "").strip().lower() in {
    "1", "true", "yes", "on",
}
DEMO_TOTAL_EXPERTS = max(
    1, int(os.environ.get("EXPERTSEARCH_DEMO_TOTAL_EXPERTS", "10"))
)
EXPERTS_PER_SUBDOMAIN = max(
    1,
    DEMO_TOTAL_EXPERTS
    if DEMO_MODE
    else int(os.environ.get("SUBDOMAIN_TARGET_EXPERTS", "15")),
)


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


def select_balanced_top_experts(
    df: pd.DataFrame,
    sub_domains: list[str],
    target_total: int = EXPERTS_PER_SUBDOMAIN,
) -> pd.DataFrame:
    """
    最终总表最多输出 target_total 人，并尽量在已选细分领域之间均衡分配名额。
    调用方按每个细分领域最多 EXPERTS_PER_SUBDOMAIN 人计算总容量。
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

# 1. 页面基本设置
st.set_page_config(page_title="全球顶尖专家检索引擎", page_icon="🌐", layout="centered")

# 2. 页面标题与说明
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
            gap: 14px;
            margin-bottom: 14px;
        }

        .app-title .title-icon {
            font-size: 42px;
            line-height: 1;
        }

        .app-title .title-text {
            color: #1f2937;
            font-size: 44px;
            font-weight: 800;
            letter-spacing: 0;
            line-height: 1.15;
        }

        .app-subtitle {
            color: #111827;
            font-size: 16px;
            line-height: 1.65;
            margin: 0 0 48px 0;
        }

        hr {
            margin: 0 0 34px 0;
        }

        div[data-testid="stTextInput"] label,
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
            font-weight: 500;
        }

        div[data-testid="stHorizontalBlock"] {
            gap: 1rem;
        }
    </style>

    <div class="app-title">
        <div class="title-icon">🌐</div>
        <div class="title-text">全球顶尖专家检索引擎</div>
    </div>
    <p class="app-subtitle">
        请输入您想探索的学科领域，系统将驱动多智能体深入全网，为您挖掘并交叉验证
        每个细分领域 <strong>__TARGET__ 位</strong> 顶尖专家的核心学术履历。
    </p>
    """.replace("__TARGET__", str(EXPERTS_PER_SUBDOMAIN)),
    unsafe_allow_html=True,
)
st.divider()
if DEMO_MODE:
    st.info(
        f"现场演示模式：本次仅运行 1 个细分领域，最终最多输出 "
        f"{DEMO_TOTAL_EXPERTS} 位专家。"
    )

# 3. 矩阵式批量检索输入区
main_domain = st.text_input("📚 主要大领域", placeholder="例如：计算机科学、临床医学、物理学")
use_ai_recommendations = st.checkbox("让智能体为我生成细分子领域", value=False)
include_chinese_experts = st.checkbox(
    "是否需要国内专家",
    value=False,
    help="勾选后检索结果允许包含中国大陆、港澳台专家；不勾选时系统会尽量避开国内专家。",
)
if "农业" in main_domain and not include_chinese_experts:
    st.warning("当前农业项目反馈要求结果中必须包含中国专家，请勾选“是否需要国内专家”。")

if "last_ai_domain" not in st.session_state:
    st.session_state.last_ai_domain = ""
if "ai_preset_options" not in st.session_state:
    st.session_state.ai_preset_options = []

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

submit_button = st.button("🚀 启动全网深度批量检索", use_container_width=True)

# 4. 点击按钮后的核心逻辑
if submit_button:
    # 将用户勾选的预设 + 填写的自定义合并为一个总的任务清单
    final_sub_domains = list(dict.fromkeys(selected_presets + custom_inputs))
    if DEMO_MODE and len(final_sub_domains) > 1:
        selected_demo_subdomain = final_sub_domains[0]
        final_sub_domains = [selected_demo_subdomain]
        st.info(
            f"现场演示版仅运行一个细分领域，本次将检索：{selected_demo_subdomain}"
        )
    
    if not main_domain:
        st.warning("⚠️ 请先填写主要大领域！")
    elif len(final_sub_domains) == 0:
        st.warning("⚠️ 请至少勾选一个常用细分领域，或在右侧填写至少一个自定义领域！")
    else:
        supplemental_document_context, supplemental_document_names, document_warnings = (
            extract_supplemental_documents(uploaded_supplemental_files)
        )
        for warning in document_warnings:
            st.warning(f"补充文件提示：{warning}")
        if supplemental_document_names:
            st.success(
                "已提取补充文件正文，将由文档压缩智能体整理后交给研究员节点："
                + "、".join(supplemental_document_names)
            )
        expert_scope_label = "包含国内专家" if include_chinese_experts else "避开国内专家"
        st.info(f"🧠 **批量指令已下达**：即将对【{main_domain}】下的 {len(final_sub_domains)} 个细分方向进行深度挖掘！检索范围：{expert_scope_label}")
        st.write("**检索任务清单：**", "、".join(final_sub_domains))
        
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
                batch_output_dir = tempfile.mkdtemp(prefix="expertsearch_batches_")
                batch_files = []

                # 每个细分领域先执行一次综合检索，不足目标人数时再自动补位。
                batch_conditions = [
                    (
                        f"综合筛选该细分领域最具代表性的前 {EXPERTS_PER_SUBDOMAIN} 位专家，"
                        "兼顾顶尖权威学者、高影响力研究者、重要奖项或学术组织成员，"
                        "并优先保证领域关联证据和身份信息真实完整"
                    ),
                ]

                progress_bar = st.progress(0)
                total_sub_domains = len(final_sub_domains)
                tier_recovery_attempts = max(
                    0, int(os.environ.get("SUBDOMAIN_TIER_RECOVERY_ATTEMPTS", "2"))
                )
                final_topup_attempts = max(
                    0, int(os.environ.get("SUBDOMAIN_FINAL_TOPUP_ATTEMPTS", "3"))
                )
                
                for i, sub in enumerate(final_sub_domains):
                    st.toast(
                        f"正在启动 {sub} 方向的 {EXPERTS_PER_SUBDOMAIN} 人精确检索...",
                        icon="🚀",
                    )
                    subdomain_names = []
                    subdomain_name_keys = set()

                    # 针对当前细分领域发起综合检索；不足目标人数时自动补位。
                    for batch_idx, condition in enumerate(batch_conditions):
                        st.info(f"🔎 正在挖掘：【{sub}】 - {condition}")
                        tier_new_names = []

                        for attempt in range(tier_recovery_attempts + 1):
                            remaining = EXPERTS_PER_SUBDOMAIN - len(tier_new_names)
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
                                f"{main_domain}领域下的{sub}方向顶级专家信息。{scope_instruction}"
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
                                    f"{len(tier_new_names)}/{EXPERTS_PER_SUBDOMAIN} 位，"
                                    f"该细分领域累计 {len(subdomain_names)}/{EXPERTS_PER_SUBDOMAIN} 位。"
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

                        if len(tier_new_names) < EXPERTS_PER_SUBDOMAIN:
                            st.warning(
                                f"【{sub}】本轮检索在补位后实际新增 "
                                f"{len(tier_new_names)}/{EXPERTS_PER_SUBDOMAIN} 位，"
                                "稍后将执行细分领域最终补位。"
                            )
                        
                        # 细粒度更新进度条 (当前细分领域进度 + 当前梯队进度)
                        current_progress = (i + (batch_idx + 1) / len(batch_conditions)) / total_sub_domains
                        progress_bar.progress(min(current_progress, 1.0))

                    # 综合检索结束后，对整个细分领域继续补齐到目标人数。
                    for topup_attempt in range(final_topup_attempts):
                        remaining = EXPERTS_PER_SUBDOMAIN - len(subdomain_names)
                        if remaining <= 0:
                            break
                        requested = min(EXPERTS_PER_SUBDOMAIN, remaining)
                        st.info(
                            f"🧩 【{sub}】正在执行最终补位第 {topup_attempt + 1} 次，"
                            f"还缺 {remaining} 位，本次目标 {requested} 位。"
                        )
                        query = (
                            f"{main_domain}领域下的{sub}方向顶级专家信息。"
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
                            continue
                        batch_files.append(excel_path)
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
                            f"当前累计 {len(subdomain_names)}/{EXPERTS_PER_SUBDOMAIN} 位。"
                        )

                    if len(subdomain_names) < EXPERTS_PER_SUBDOMAIN:
                        st.warning(
                            f"【{sub}】完成所有补位后获得 "
                            f"{len(subdomain_names)}/{EXPERTS_PER_SUBDOMAIN} 位互不重复专家。"
                            "这通常表示可验证候选不足或外部服务持续失败，最终表将保留已核验数据。"
                        )
                
                # 2. 检索全部完成后，进行 Excel 文件的大合并
                new_files = set(batch_files)
                
                if new_files:
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
                            f"{main_domain}领域下的{'、'.join(final_sub_domains)}方向顶级专家信息"
                        )
                        
                        # 先合并各批次的互补信息，再对所有信息严重缺失专家进行定向补全。
                        master_df = finalize_merged_experts(
                            master_df,
                            include_chinese_experts=include_chinese_experts,
                            query=final_query,
                        )
                        master_df = select_balanced_top_experts(
                            master_df,
                            final_sub_domains,
                            target_total=EXPERTS_PER_SUBDOMAIN * len(final_sub_domains),
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
                        master_file_name = f"{safe_filename_part(main_domain)}_{result_timestamp}_批量专家总名单.xlsx"
                        master_file_path = os.path.join(output_dir, master_file_name)
                        write_expert_excel(
                            master_df,
                            master_file_path,
                            data_source_summary=data_source_summary(
                                extra_data_source_urls,
                                query=main_domain,
                                supplemental_document_names=supplemental_document_names,
                            ),
                            query=main_domain,
                            extra_data_source_urls=extra_data_source_urls,
                            supplemental_document_names=supplemental_document_names,
                            translate_delivery=True,
                            final_delivery_only=True,
                        )
                        
                        # 结束计时
                        end_time = time.time()
                        elapsed_seconds = int(end_time - start_time)
                        
                        # 转化成分秒并用最普通的文本显示在前端
                        minutes = elapsed_seconds // 60
                        seconds = elapsed_seconds % 60
                        st.write(f"⏱️ 智能体检索完毕！本次任务执行总耗时：{minutes} 分 {seconds} 秒")
                        
                        # 【新增逻辑】：清理中间过程产生的碎片文件
                        shutil.rmtree(batch_output_dir, ignore_errors=True)
                        
                        # 3. 提供总表下载按钮
                        with open(master_file_path, "rb") as file:
                            st.download_button(
                                label=f"📥 点击下载【全局大合并】Excel 报表 (共 {final_count} 位独立专家)",
                                data=file,
                                file_name=master_file_name,
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True
                            )
                    else:
                        shutil.rmtree(batch_output_dir, ignore_errors=True)
                        st.error("⚠️ 中间批次未包含可合并的专家数据，未生成最终结果表。")
                else:
                    shutil.rmtree(batch_output_dir, ignore_errors=True)
                    st.error("⚠️ 检索完成，但未能成功生成有效的数据文件，请检查大模型 API 或网络状态。")
            except Exception as e:
                if "batch_output_dir" in locals():
                    shutil.rmtree(batch_output_dir, ignore_errors=True)
                st.error(f"❌ 检索过程中出现底层异常: {e}")
