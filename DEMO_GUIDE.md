# ExpertSearch 10 人现场演示版

此分支用于一次性现场演示。演示配置只在启动脚本进程内生效，不修改 `.env`。

## 启动

先停止已经运行的 Streamlit，然后在项目目录执行：

```powershell
.\start_demo_10_experts.cmd
```

浏览器访问：

```text
http://localhost:8501
```

演示模式只运行用户选择的第一个细分领域，最终最多输出 10 位专家，并保留
OpenAlex、Tavily、Semantic Scholar 按需补充、个人主页、OpenCLI、生存状态核验、
领域错配清洗和最终 Excel 下载流程。

## 演示建议

选择一个边界清晰、公开资料较丰富的细分领域。现场演示时不要同时上传大型补充文件，
也不要启用智能体生成细分领域，以减少额外 LLM 调用和等待时间。

## 演示结束后回到开发版

先在运行终端按 `Ctrl + C` 停止演示系统，然后执行：

```powershell
git switch main
.\start_streamlit_logged.cmd
```
