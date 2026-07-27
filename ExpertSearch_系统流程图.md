本文件使用语雀兼容的基础 Mermaid 语法，将完整系统拆分为三张纵向流程图，避免图形过宽或渲染接口返回 Bad Request。

## 一、系统总体流程

```mermaid
graph TD
    A[用户打开 Streamlit 前端] --> B[输入大领域]
    B --> C{是否使用智能体推荐细分领域}
    C -->|是| D[智能体生成细分领域]
    C -->|否| E[选择常用或自定义细分领域]
    D --> F[用户勾选细分领域]
    E --> F
    F --> G[设置国内专家选项]
    G --> H[填写补充网页]
    H --> I[上传 PDF 或 Word 文件]
    I --> J[Python 提取文件正文]
    J --> K[逐个处理细分领域]
    K --> L[运行 LangGraph 专家检索任务]
    L --> M{本细分领域是否达到15位}
    M -->|否| N[排除已有专家并自动补位]
    N --> L
    M -->|是| O{是否还有其他细分领域}
    O -->|是| K
    O -->|否| P[合并所有临时批次]
    P --> Q[最终清洗和信息补全]
    Q --> R[生成最终 Excel]
    R --> S[前端提供下载]
    R --> T[删除临时批次文件]
```

## 二、单次 LangGraph 任务流程

```mermaid
graph TD
    A[初始化 AgentState] --> B[文档压缩节点]
    B --> C[研究员节点]
    D[OpenAlex 学术证据] --> C
    E[Tavily 网页证据] --> C
    F[Semantic Scholar 按需证据] --> C
    G[补充网页和上传文件] --> C
    C --> H[输出 research_data]
    H --> I[验证节点]
    I --> J[输出 validation_feedback]
    H --> K[纠错节点]
    J --> K
    K --> L[输出 final_data]
    L --> M[Excel 转换节点]
    M --> N[输出 excel_path]
    N --> O[返回 Streamlit 前端]
```

## 三、最终清洗与交付流程

```mermaid
graph TD
    A[合并全部专家记录] --> B[字段级合并]
    B --> C[近似姓名去重]
    C --> D[明显领域错配清洗]
    D --> E[关键字段定向补全]
    E --> F[OpenAlex 指标补全]
    E --> G[Semantic Scholar 按需补全]
    E --> H[HTTP 访问个人主页]
    H -->|失败或正文不足| I[OpenCLI 浏览器回退]
    E --> J[Tavily 定向搜索]
    F --> K[姓名和身份交叉验证]
    G --> K
    H --> K
    I --> K
    J --> K
    K --> L[独立生存状态核验]
    L --> M{是否确认已故}
    M -->|是| N[从最终专家库剔除]
    M -->|否| O[计算评价总分]
    O --> P[按总分排序]
    P --> Q[按细分领域均衡控制容量]
    Q --> R[中文规范化和翻译]
    R --> S[英文残留检查]
    S --> T[生成最终汇总 Excel]
```

## 四、关键状态字段

| 节点 | 读取字段 | 输出字段 |
| --- | --- | --- |
| 文档压缩节点 | supplemental_document_context | compressed_supplemental_document_context |
| 研究员节点 | query、排除名单、目标人数、压缩文档 | research_data |
| 验证节点 | research_data | validation_feedback |
| 纠错节点 | research_data、validation_feedback | final_data |
| Excel 转换节点 | final_data | excel_path |

## 五、架构说明

- 外部工具不是由 LLM 自主调用，而是 Python 先调用 OpenAlex、Tavily、Semantic Scholar、主页访问和 OpenCLI，再将证据交给 LLM 整理。
- 每个细分领域独立维护专家排除名单，当前目标为最多 15 位专家。
- 不同细分领域可以出现同一位专家，最终合并时保留其多个真实细分领域归属。
- 中间批次文件写入系统临时目录，`Expert_Results` 只保留最终汇总结果。
- 明显领域错配、确认已故、身份错配和近似重复记录会在最终交付前清洗。
- 单个外部服务失败时记录日志并降级，不中断整个检索流程。
