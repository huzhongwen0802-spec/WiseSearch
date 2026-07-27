# ExpertSearch 项目结构

```text
ExpertSearch/
├─ app.py                      # Streamlit 前端入口
├─ main.py                     # 兼容旧导入的轻量入口
├─ start_streamlit_logged.cmd  # Windows 主启动命令
├─ expertsearch/               # 核心 Python 包
├─ tests/                      # 自动化测试
├─ scripts/windows/            # Windows 与 OpenCLI 辅助命令
├─ docs/                       # 项目说明和流程图
├─ tools/opencli/              # OpenCLI 扩展与本地运行时
├─ logs/                       # 当前和历史运行日志
├─ Expert_Results/             # 最终 Excel 结果
└─ outputs/                    # 分析图表和临时产物
```

## 稳定入口

正式运行仍在项目根目录执行：

```powershell
.\start_streamlit_logged.cmd
```

或：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

## 核心代码

- `expertsearch/main.py`：构建 LangGraph 并执行任务。
- `expertsearch/agents.py`：研究员、验证、纠错和文档压缩节点。
- `expertsearch/state.py`：状态字段契约。
- `expertsearch/utils.py`：清洗、增强、评分和 Excel 写出。
- `expertsearch/expert_enrichment.py`：个人主页和外部数据补全。
- `expertsearch/*_client.py`：OpenAlex、Semantic Scholar 和 OpenCLI 客户端。

根目录 `main.py` 仅用于兼容已有的 `from main import ...` 调用，新代码应优先从
`expertsearch.main` 导入。
