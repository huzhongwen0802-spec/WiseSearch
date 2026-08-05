# ExpertSearch 上传 GitHub 完整指南

本指南面向第一次使用 Git 和 GitHub 的 Windows 用户。

项目目录：`C:\Users\胡中文\Documents\Codex\ExpertSearch`

GitHub 仓库：`https://github.com/huzhongwen0802-spec/WiseSearch`

## 一、先理解 Git、GitHub 和 Git Bash

Git 是安装在电脑上的版本管理工具，用来记录代码的修改历史。

GitHub 是在线代码托管平台，可以保存本地项目并与他人协作。

Git Bash 是随 Git 安装的命令行窗口。它不是 GitHub 网站，而是让你可以在电脑上输入 Git 命令的工具。PowerShell、CMD 和 Git Bash 都可以执行大部分 Git 命令。

上传过程可以理解为：

```text
本地项目 -> Git 保存版本 -> GitHub 远程仓库
```

## 二、上传前的安全原则

以下内容不能上传：

```text
.env
.venv/
Expert_Results/
*.xlsx
*.log
__pycache__/
```

- `.env` 保存真实 API 密钥；
- `.venv` 是本机虚拟环境；
- `Expert_Results` 可能包含业务结果和专家信息；
- Excel 和日志属于运行结果，不是源代码；
- `__pycache__` 是 Python 自动生成的缓存。

`.env.example` 可以上传，但只能保存变量名称，不能保存真实密钥：

```text
API_BASE_URL=
API_KEY=
TAVILY_API_KEY=
OPENALEX_API_KEY=
OPENALEX_MAILTO=
SEMANTIC_SCHOLAR_API_KEY=
```

如果密钥曾经被上传到 GitHub，应立即到对应服务控制台撤销旧密钥并生成新密钥。仅删除文件不能彻底消除 Git 历史中的密钥。

## 三、创建 GitHub 仓库

1. 打开 `https://github.com` 并登录；
2. 点击右上角 `+`；
3. 选择 `New repository`；
4. 仓库名称填写 `WiseSearch`；
5. 建议选择 `Private`；
6. 不要勾选 README、`.gitignore` 或 License；
7. 点击 `Create repository`。

记下仓库地址：

```text
https://github.com/huzhongwen0802-spec/WiseSearch.git
```

## 四、打开 Git Bash 并进入项目目录

从 Windows 开始菜单搜索并打开 `Git Bash`。

在 Git Bash 中执行：

```bash
cd "/c/Users/胡中文/Documents/Codex/ExpertSearch"
```

`cd` 是 `change directory` 的缩写，意思是切换文件夹。

检查当前目录：

```bash
pwd
```

应看到类似：

```text
/c/Users/胡中文/Documents/Codex/ExpertSearch
```

如果使用 PowerShell，路径写法为：

```powershell
cd "C:\Users\胡中文\Documents\Codex\ExpertSearch"
```

## 五、检查 Git 是否安装

```bash
git --version
```

作用：显示 Git 版本。看到类似 `git version 2.54.0.windows.1` 即表示安装成功。

如果显示 `command not found`，从 `https://git-scm.com/download/win` 安装 Git，然后重新打开 Git Bash。

## 六、初始化本地仓库

```bash
git init
```

作用：在当前项目中创建 `.git` 管理目录，让 Git 开始记录项目版本。这个命令不会删除项目文件。

成功时可能显示：

```text
Initialized empty Git repository
```

或者：

```text
Reinitialized existing Git repository
```

## 七、设置提交者身份

```bash
git config user.name "huzhongwen0802-spec"
git config user.email "你的GitHub邮箱"
```

把 `你的GitHub邮箱` 换成注册 GitHub 时使用的邮箱。查看设置：

```bash
git config user.name
git config user.email
```

这只是设置提交记录的作者，不会自动上传 API 密钥。

## 八、检查项目状态和忽略规则

```bash
git status
git status --short
```

`git status` 查看详细状态，`git status --short` 查看简短列表。

状态字母含义：

```text
A   新增文件
M   修改文件
D   删除文件
??  尚未被 Git 跟踪的文件
```

查看忽略规则：

```bash
cat .gitignore
```

重点确认包含：`.env`、`.venv/`、`Expert_Results/`、`*.xlsx`、`*.log` 和 `__pycache__/`。

## 九、将文件加入暂存区

```bash
git add .
```

作用：把没有被 `.gitignore` 排除的文件放入暂存区，表示准备放入下一次提交。此时还没有上传到 GitHub。

查看暂存文件：

```bash
git status --short
```

确认列表中没有 `.env`、`.venv`、`Expert_Results` 或 Excel 文件。

查看即将提交的文件名：

```bash
git diff --cached --name-only
```

误加入单个文件时：

```bash
git restore --staged 文件名
```

误加入整个目录时：

```bash
git restore --staged -- .
```

这些命令只取消暂存，不会删除电脑上的文件。

如果目录已经被 Git 跟踪、后来才加入 `.gitignore`，执行：

```bash
git rm -r --cached 目录名
git add .
```

`--cached` 表示只从 Git 记录中移除，不删除本地目录。

## 十、创建第一次本地提交

```bash
git commit -m "Initial commit: ExpertSearch"
```

作用：在本地保存一个版本。其中 `-m` 表示后面跟版本说明。

查看提交历史：

```bash
git log --oneline
```

退出日志页面按 `q`。提交仍然只在本地，尚未上传。

## 十一、设置主分支

```bash
git branch -M main
```

作用：把当前分支命名为 `main`。查看当前分支：

```bash
git branch
```

带 `*` 的分支是当前分支。

## 十二、连接 GitHub 远程仓库

```bash
git remote add origin https://github.com/huzhongwen0802-spec/WiseSearch.git
```

作用：把本地项目连接到 GitHub 仓库。`origin` 是远程仓库的本地简称。

检查连接：

```bash
git remote -v
```

正常应显示：

```text
origin  https://github.com/huzhongwen0802-spec/WiseSearch.git (fetch)
origin  https://github.com/huzhongwen0802-spec/WiseSearch.git (push)
```

如果提示 `remote origin already exists`，说明已经连接过，不要重复添加。地址错误时修改：

```bash
git remote set-url origin https://github.com/huzhongwen0802-spec/WiseSearch.git
```

## 十三、第一次上传到 GitHub

```bash
git push -u origin main
```

作用：把本地 `main` 分支上传到 GitHub 的 `origin` 仓库。`-u` 会建立本地分支和远程分支的跟踪关系。

第一次执行时可能打开浏览器登录 GitHub。登录并授权后回到命令窗口等待结束。

成功时通常会看到：

```text
Writing objects: 100% ... done.
* [new branch] main -> main
branch 'main' set up to track 'origin/main'
```

然后打开：

```text
https://github.com/huzhongwen0802-spec/WiseSearch
```

## 十四、日常修改后的上传流程

每次修改后执行：

```bash
git status
```

作用：查看哪些文件被修改。

```bash
git diff
```

作用：查看具体代码差异。

```bash
git add .
```

作用：准备要提交的文件。

```bash
git commit -m "说明本次修改"
```

作用：保存本地版本。

```bash
git push
```

作用：上传到 GitHub。第一次使用 `git push -u origin main` 后，日常上传通常只需要上述三条命令：`git add .`、`git commit`、`git push`。

## 十五、项目运行检查

修改核心 Python 文件后，在 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py main.py expertsearch tests
```

作用：检查 Python 语法错误。

启动系统：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

带日志稳定启动：

```powershell
.\start_streamlit_logged.cmd
```

建议先确认系统可以运行，再执行提交和上传。

## 十六、常见错误

### `fatal: not a git repository`

说明当前窗口不在项目目录，或尚未初始化 Git：

```bash
cd "/c/Users/胡中文/Documents/Codex/ExpertSearch"
git init
```

### `src refspec main does not match any`

说明还没有提交，或分支名称不正确：

```bash
git add .
git commit -m "Initial commit: ExpertSearch"
git branch -M main
git push -u origin main
```

### `rejected because the remote contains work`

说明 GitHub 仓库中已有 README 或其他文件。不要直接强制推送，应先检查远程内容并合并。

### `Permission denied`

检查是否登录了正确的 GitHub 账号、是否拥有仓库权限，并查看：

```bash
git remote -v
```

### `LF will be replaced by CRLF`

这是 Windows 换行符提示，不是错误，通常可以忽略。

### 中文文件名显示异常

```bash
git config core.quotepath false
```

作用：让 Git 尽量直接显示中文文件名。

## 十七、误上传 `.env` 的处理

如果 `.env` 已经进入 GitHub：

1. 立即在 API 服务控制台撤销旧密钥；
2. 生成新密钥；
3. 确认 `.gitignore` 包含 `.env`；
4. 执行：

```bash
git rm --cached .env
git commit -m "Remove local environment secrets"
git push
```

即使网页文件被删除，旧密钥仍可能存在于历史中，因此重新生成密钥最重要。

## 十八、命令速查

```text
git --version       查看 Git 是否安装
git init            初始化仓库
git status          查看项目状态
git add .           加入暂存区
git commit          保存本地版本
git log             查看历史版本
git branch          查看分支
git remote -v       查看远程仓库
git push            上传到 GitHub
git pull            下载 GitHub 更新
git diff            查看代码差异
```

最重要的安全原则：代码可以上传，真实密钥不能上传；配置模板可以上传，真实配置不能上传；源代码可以上传，业务结果和运行日志应谨慎上传。
