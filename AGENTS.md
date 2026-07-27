# AGENTS.md

本文件是 ExpertSearch 项目的 coding agent 协作说明。请在修改代码前先阅读，优先遵守这里的项目约定。

## 项目目标

ExpertSearch 是一个基于 Streamlit + LangGraph 的全球专家检索系统。用户输入主要大领域和细分领域后，系统会批量检索专家信息，结合 OpenAlex、Tavily、Semantic Scholar、个人主页爬取和 LLM 整理，最终生成 Excel 专家名单。

核心输出是 `Expert_Results/` 下的 `.xlsx` 文件，最终汇总文件命名格式为：

```text
大领域_YYYYMMDD_HHMMSS_批量专家总名单.xlsx
```

五个梯队的 `Expert_Data_*.xlsx` 仅作为运行期临时文件，必须写入系统临时目录；
`Expert_Results/` 只保留最终汇总结果，不向前端展示或保留中间批次文件。

## 运行方式

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

如果前端已经打开但代码刚改过，优先完整重启 Streamlit，而不只是点 rerun：

```powershell
Ctrl + C
.\.venv\Scripts\python.exe -m streamlit run app.py
```

稳定带日志启动优先使用：

```powershell
.\start_streamlit_logged.cmd
```

该脚本会防止重复启动 8501 端口实例，并尝试启动 OpenCLI Browser Bridge；
OpenCLI 暂时不可用时只记录警告，主系统仍会继续启动并使用普通 HTTP 主页访问。

轻量语法检查：

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py agents.py main.py utils.py expert_enrichment.py openalex_client.py semantic_scholar_client.py llm_safety.py state.py
```

## 环境变量

密钥放在 `.env`，不要写入代码或文档。

常用变量：

```text
API_BASE_URL
API_KEY
TAVILY_API_KEY
OPENALEX_API_KEY
OPENALEX_MAILTO
SEMANTIC_SCHOLAR_API_KEY
```

可选开关：

```text
ENABLE_SEMANTIC_SCHOLAR_CONTEXT=false
SEMANTIC_SCHOLAR_MAX_ROWS=2
SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS=1.2
SEMANTIC_SCHOLAR_RETRY_ATTEMPTS=2
SEMANTIC_SCHOLAR_RETRY_DELAY_SECONDS=2
SEMANTIC_SCHOLAR_429_COOLDOWN_SECONDS=60
EXPERT_ENRICHMENT_MAX_OPENALEX_RETRY_ROWS=16
EXPERT_ENRICHMENT_MAX_S2_RETRY_ROWS=0
OPENALEX_SECOND_PASS_VARIANTS=3
FINAL_ENRICHMENT_MAX_S2_ROWS=20
EXPERT_ENRICHMENT_MAX_HOMEPAGE_ROWS=16
EXPERT_ENRICHMENT_MAX_TAVILY_ROWS=8
EXPERT_ENRICHMENT_TAVILY_RESULTS=5
SURVIVAL_VERIFICATION_MAX_ROWS=25
SURVIVAL_VERIFICATION_TAVILY_RESULTS=5
DELIVERY_ENABLE_LLM_TRANSLATION=true
DELIVERY_TRANSLATION_MODEL=gpt-5.5
DELIVERY_TRANSLATION_MAX_CELLS=240
DELIVERY_TRANSLATION_BATCH_CELLS=18
OPENCLI_HOMEPAGE_MAX_CALLS_PER_PROCESS=25
OPENCLI_HOMEPAGE_TIMEOUT_SECONDS=45
OPENCLI_HOMEPAGE_WAIT_SECONDS=3
OPENCLI_HOMEPAGE_MAX_CHARS=16000
OPENCLI_HEALTH_CACHE_SECONDS=120
OPENCLI_VALIDATE_DNS_PUBLIC_IP=true
OPENCLI_ALLOW_BENCHMARK_PROXY_IPS=true
OPENCLI_AUTO_RECOVER_BROWSER_BRIDGE=true
OPENCLI_AUTO_RECOVER_WAIT_SECONDS=5
SUPPLEMENTAL_DOCUMENT_MAX_FILES=10
SUPPLEMENTAL_DOCUMENT_MAX_FILE_MB=15
SUPPLEMENTAL_DOCUMENT_CHARS_PER_FILE=50000
SUPPLEMENTAL_DOCUMENT_TOTAL_CHARS=0
RESEARCHER_OPENALEX_CONTEXT_CHARS=6000
RESEARCHER_TAVILY_CONTEXT_CHARS=4000
RESEARCHER_DOCUMENT_CONTEXT_CHARS=7000
RESEARCHER_SUPPLEMENTAL_WEB_CONTEXT_CHARS=3500
RESEARCHER_S2_CONTEXT_CHARS=1500
RESEARCHER_TOTAL_EVIDENCE_CHARS=22000
RESEARCHER_CHUNK_SIZE=10
RESEARCHER_TARGET_EXPERTS=20
RESEARCHER_MAX_BATCH_ATTEMPTS=5
SUBDOMAIN_TIER_RECOVERY_ATTEMPTS=2
SUBDOMAIN_FINAL_TOPUP_ATTEMPTS=3
DOCUMENT_PROCESSOR_INPUT_CHARS=12000
DOCUMENT_PROCESSOR_OUTPUT_CHARS=6000
DOCUMENT_PROCESSOR_FAILURE_CACHE_SECONDS=300
```

`SUPPLEMENTAL_DOCUMENT_TOTAL_CHARS=0` 表示不设置跨文件总字符上限。所有合规上传文件都会先参与
Python 相关性筛选，再由文档压缩智能体压缩；研究员节点仍受
`DOCUMENT_PROCESSOR_INPUT_CHARS`、`DOCUMENT_PROCESSOR_OUTPUT_CHARS` 和
`RESEARCHER_DOCUMENT_CONTEXT_CHARS` 保护，不会直接接收全部原文。

Semantic Scholar 默认只作为按需补充，不作为主检索上下文。不要默认打开 `ENABLE_SEMANTIC_SCHOLAR_CONTEXT`，否则会增加耗时并受 1 request/sec 限速影响。

最终汇总阶段会对任何关键交付字段仍为空的专家执行按字段分流补全，而不只处理“严重缺失”记录。每轮结束后日志必须输出 `[专家补全汇总]` 和 `[最终定向补全]`，便于核对 OpenAlex、Semantic Scholar、主页、OpenCLI、Tavily 的实际尝试次数和改善数量。

## 主要文件

- `app.py`：Streamlit 前端；负责领域输入、细分领域选择、批量运行、下载按钮、最终总表合并。
- `main.py`：LangGraph 入口；暴露 `run_agent_task()` 和 `get_dynamic_recommendations()`。
- `state.py`：LangGraph 状态定义。关键字段为 `query -> research_data -> validation_feedback -> final_data -> excel_path`。
- `agents.py`：LLM 节点、Tavily 搜索上下文、细分领域推荐。当前 LLM 使用 OpenAI 兼容接口 `ChatOpenAI(model="gpt-5.5")`。
- `utils.py`：Markdown 表格解析、Excel 写出、OpenAlex/Semantic Scholar 指标增强、评分、近似去重、字段清洗。
- `expert_enrichment.py`：专家信息补全层；访问个人主页、补邮箱/研究兴趣/单位/职位/教育/主要成果/合作线索。
- `openalex_client.py`：OpenAlex 作者、作品、指标接口。
- `semantic_scholar_client.py`：Semantic Scholar 作者和论文接口，带 1 request/sec 限速。
- `llm_safety.py`：LLM 中转服务敏感词误杀保护，只改写发送给 LLM 的文本，不改前端显示和文件命名。
- `survival_verification.py`：独立生存状态核验层；通过网页证据识别讣告、逝世公告等明确死亡证据。
- `chinese_output.py`：最终甲方交付表中文规范化、批量翻译与英文残留检查。
- `opencli_homepage_client.py`：OpenCLI 真实浏览器主页访问回退层；限制公网 URL、调用次数和超时。

## 数据流

标准 LangGraph 生命周期：

```text
query + supplemental_document_context
-> document_processor_node: 输出 compressed_supplemental_document_context
-> researcher_node: 读取 excluded_expert_names + target_expert_count，输出 research_data
-> validator_node: 读取 research_data，输出 validation_feedback
-> corrector_node: 读取 research_data + validation_feedback，输出 final_data
-> excel_converter_node: 读取 final_data，输出 excel_path
```

`run_agent_task()` 返回：

```python
(excel_path, error_message)
```

成功时 `excel_path` 是绝对路径，失败时 `excel_path=None`。

## 检索与增强策略

当前架构不是 LLM tool-calling。Python 先调用外部服务，再把证据文本喂给 LLM 整理。

默认职责：

- 前端按每个细分领域维护独立的历史专家名单，后续梯队必须排除当前细分领域此前已发现的人选。
- 每个梯队目标为 20 位实际新增专家；不足时自动补位，五个梯队结束后继续执行细分领域级补位，尽量达到 100 位。
- 不同细分领域之间允许同一专家重复出现；最终合并时保留该专家的多个细分领域归属。

- OpenAlex：主学术数据库，优先用于 H 指数、i10、总被引、主题、作品证据。
- Tavily：网页搜索，补个人主页、邮箱、奖项、合作新闻。
- Semantic Scholar：按需补充代表论文和交叉验证，不在最终 Excel 展示 S2 技术字段。
- 个人主页爬取：如果成功访问主页，补 `邮箱/电话`、`研究兴趣`、`工作单位`、`职位`、`教育背景`、`主要成果`、`国内合作学者与单位`。
- OpenCLI Browser Bridge：普通 HTTP 主页访问失败或正文不足时，限量使用真实浏览器渲染后重试；扩展未连接时自动跳过，不中断主流程。调用预算按每个专家批次重新计数，默认一个 20 人梯队最多回退 25 次。

最终 Excel 不展示以下内部字段：

```text
S2匹配姓名
S2作者ID
S2主页
S2代表论文
```

## Excel 字段约定

基础 16 列来自 LLM 表格：

```text
专家姓名
国籍
个人主页
邮箱/电话
研究兴趣
工作单位
职位
工作经历
教育背景
H指数
主要成果
国内合作学者与单位
入选依据
领域关联依据
生存状态
信息来源
```

后处理增加字段：

```text
i10指数
总被引次数
OpenAlex匹配姓名
OpenAlex作者ID
OpenAlex主题
成功访问主页
主页访问方式
主页访问失败原因
评价_H指数
评价_总被引次数
评价_顶级头衔标识
评价_i10指数
评价_TotalScore
独立生存状态核验
生存状态核验依据
```

最终中文交付主表将 `国内合作学者与单位` 拆分为 `国内合作学者` 和
`国内合作单位`，并把 `入选依据` 紧跟在 `国内合作单位` 后面。
`入选依据` 应优先写明有公开证据支持的院士身份、重要奖项、
Fellow/行业协会会员身份、权威人才榜单或突出学术影响力指标。

独立生存状态核验规则：

- LLM 输出的“生存状态”仅作为待核验线索，不能单独触发剔除。
- 只有独立网页核验同时匹配专家姓名和明确死亡表述，并达到证据门槛时，才标记为 `确认已故` 并从最终专家名单剔除。
- 单一弱证据标记为 `疑似已故待复核`，不自动剔除。

最终 Excel 额外包含 `中文输出检查` 工作表。正式甲方主表会先执行术语规范化，并可通过 LLM 批量翻译英文叙述；无法自动消除的英文残留会进入该检查表。

`邮箱/电话` 目前只保留真实邮箱。不要写电话、邮编、日期、年份或猜测邮箱。

`成功访问主页` 取值：

```text
是
否
未尝试
```

`主页访问方式` 常见取值：

```text
HTTP
OpenCLI
HTTP失败，OpenCLI未成功
未尝试
```

OpenCLI 使用独立 Chrome 配置。首次使用时运行 `start_opencli_browser.cmd`，
在 `chrome://extensions/` 开启开发者模式并加载
`tools/opencli/extension`，然后运行 `check_opencli.cmd`，确认 Extension 和
Connectivity 均显示 `[OK]`。

OpenCLI 命令入口优先使用项目内 `tools/opencli/runtime`，避免依赖用户目录下的全局 npm 安装。缺失时运行：

```powershell
npm install --prefix tools/opencli/runtime @jackwener/opencli@latest
```

## 评分规则

用户设计的总分公式：

```text
TotalScore = H + (log10(Citations) * 15) + (i10 * 0.2) + TitleBonus
```

`TitleBonus` 来自院士、Fellow、诺奖、图灵奖等头衔线索。

注意：

- 不要让低可信数据库匹配覆盖高 H 或高引用原始信息。
- 如果 OpenAlex/S2 返回 H=1、H=3、极低引用，而原始表显示该专家为高影响力学者，应拒绝采用。
- `utils.is_metric_match_trusted()` 是通用可信度门槛，不再依赖手写领域词典。

## 去重与清洗

`utils.drop_near_duplicate_experts()` 会合并近似姓名，例如：

```text
Harald Pfeiffer
Harald P. Pfeiffer
```

保留 `评价_TotalScore` 更高、信息更完整的一条。

不要在前端合并阶段只依赖 `drop_duplicates(subset=["专家姓名"])`；最终还要经过 `add_ranking_metrics()` 做近似去重。

## LLM 敏感词误杀

部分中转服务会把正常学术词误判为敏感词，例如农业中的“种质资源”。相关保护在 `llm_safety.py`。

原则：

- 只改写发送给 LLM 的文本。
- 不改用户输入显示。
- 不改 Tavily/OpenAlex 检索原始 query。
- 不改 Excel 文件名。

如果遇到 `local:sensitive_words` 或 `sensitive words detected`，优先在 `llm_safety.py` 添加最小范围的学术术语替换。

## 错误定位

`agents.invoke_llm_with_stage()` 会给 LLM 调用增加阶段标签：

```text
研究员节点
验证节点
纠错节点
细分领域推荐节点
```

`main.run_agent_task()` 会把外部连接错误整理为用户可读错误。

常见错误：

- `LLM/API 中转服务连接失败或超时`：通常是模型中转服务、网络、并发或 prompt 过大。
- `Tavily 检索全部失败`：搜索服务连接失败或超时。
- `local:sensitive_words`：中转服务误杀，需要看 `llm_safety.py`。

## 前端约定

`app.py` 当前 UI 是居中窄版工作台。

细分领域有两种来源：

- 常用细分领域 / 智能体生成细分领域：默认收起。
- 自定义细分领域：默认收起，最多 10 个输入框。

用户可选择是否让智能体生成细分子领域。不要默认强制调用推荐 LLM。

## 修改注意事项

- 默认使用 UTF-8，文件顶部保留 `# -*- coding: utf-8 -*-`。
- 不要提交 `.env` 内容或 API key。
- 不要把 `.venv/`、`__pycache__/`、生成的 Excel 当成源码改动。
- 手动编辑文件优先使用 `apply_patch`。
- 新增依赖后同步更新 `requirements.txt`。
- 每次改完核心 Python 文件，至少运行 `py_compile`。
- 对会联网的验证要谨慎，当前环境可能限制网络；能用离线小测试先测离线逻辑。

## 当前建议测试流程

每次核心逻辑修改后，先小规模跑：

```text
1 个大领域
1 个细分领域
```

重点检查：

- 是否生成最终 Excel。
- 是否没有 S2 技术字段。
- `成功访问主页` 是否正常。
- 邮箱字段是否只含邮箱或 `暂无公开信息`。
- 是否有明显 OpenAlex 低 H 错配。
- 是否有近似重复姓名。
- 个人主页成功访问后是否补充了职位、教育背景、研究兴趣等字段。
- `主页访问方式` 是否能区分 HTTP 与 OpenCLI，OpenCLI 失败时是否记录了明确原因。
- 是否存在 `疑似已故待复核`，以及确认已故专家是否已从主表剔除。
- `中文输出检查` 是否仅保留必要专名，是否仍有需要人工翻译的英文叙述。
