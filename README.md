# PlotPilot（墨枢）

> 面向长篇小说与剧本创作的本地 AI 工作台：规划、写作、记忆、知识图谱和自动驾驶都围绕同一部作品持续保存。

<p align="center">
  <a href="https://github.com/ogr34907-dot/pp"><img src="https://img.shields.io/badge/GitHub-ogr34907--dot%2Fpp-181717?style=flat&logo=github" alt="GitHub repository"></a>
  <img src="https://img.shields.io/badge/Python-3.14-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.14">
  <img src="https://img.shields.io/badge/Vue-3.5-4FC08D?style=flat&logo=vuedotjs&logoColor=white" alt="Vue 3">
  <img src="https://img.shields.io/badge/FastAPI-local%20API-009688?style=flat&logo=fastapi&logoColor=white" alt="FastAPI">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0%20%2B%20Commons%20Clause-D22128?style=flat&logo=apache&logoColor=white" alt="License"></a>
</p>

<p align="center">
  <img src="docs/screenshots/workbench-writing.png" alt="PlotPilot 写作工作台" width="49%">
  <img src="docs/screenshots/workbench-dag.png" alt="PlotPilot 故事线与知识图谱工作台" width="49%">
</p>

## 这是什么

PlotPilot 不是单次续写工具。它把一本书的设定、章节、故事线、伏笔、摘要、人物关系和生成过程保存为可检查的本地状态，让 AI 在后续章节中有东西可查、有规则可守。

当前仓库提供完整的本地工作台与 REST API，适合希望在自己电脑上写长篇、剧本或持续迭代故事设定的作者。默认数据保存在本地，不需要把作品正文或 API Key 交给本仓库。

## 当前可用能力

- **作者工作台**：暖纸主题、响应式布局，覆盖新书设定、写作、章节浏览、人物与地点关系图。
- **结构化创作**：宏观结构、章节节拍、人物设定、世界观、故事线和伏笔都可以围绕同一作品维护。
- **持续记忆**：章节完成后沉淀摘要、事件、三元组、伏笔和向量索引，为后续生成提供可追溯的上下文。
- **知识图谱**：从作品设定和章节事实中维护实体关系，支持人物、地点和故事关系的可视化查看。
- **自动驾驶**：按章节规划、生成、审阅和提交流程推进；进度、暂停原因和恢复状态会实时显示在工作台中。
- **恢复保护**：章节的规范章后记忆未提交时，自动驾驶会暂停，而不是带着不完整记忆继续写。
- **本地优先**：FastAPI、SQLite、向量索引和前端工作台均可在本机运行；LLM 与嵌入模型使用你自己的服务凭证。

## 快速开始

以下是 Windows 源码本地部署的推荐方式。首次准备一次，之后使用启动器即可。

### 1. 准备环境

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（启动器为 Windows 批处理脚本） |
| Python | `3.14.x`，推荐 `3.14.5` |
| Node.js | `20.19+` 或 `22.12+` |
| Git | 用于克隆和同步仓库 |
| LLM 凭证 | 至少准备一个可用的模型服务凭证 |

### 2. 克隆并安装依赖

在 PowerShell 中执行：

```powershell
git clone https://github.com/ogr34907-dot/pp.git
Set-Location pp

Copy-Item .env.example .env
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Push-Location frontend
npm install
npm run build
Pop-Location
```

需要使用本地嵌入模型时，再执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
```

### 3. 配置模型服务

编辑根目录的 `.env`，填入自己可用的模型服务配置。`.env.example` 提供了方舟与 Anthropic 的示例；项目也支持 OpenAI 与 Gemini 兼容配置。

至少完成一组 LLM 配置，例如：

```dotenv
ARK_API_KEY=your-key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions
ARK_MODEL=your-model
```

默认语义检索使用云端嵌入服务。可填写 `EMBEDDING_API_KEY`，或复用 `OPENAI_API_KEY`；若改为本地模型，请设置 `EMBEDDING_SERVICE=local` 并安装 `requirements-local.txt`。

不要提交 `.env`、作品数据、日志或向量索引。它们都属于本地私有内容。

### 4. 启动工作台

日常本机写小说时，双击 `tools\start-local.vbs`。它会隐藏命令行窗口和后台服务进程，等待 FastAPI 就绪后打开浏览器。

需要从终端观察真实启动错误时，运行诊断入口 `tools\start-dev.bat`：

```powershell
.\tools\start-dev.bat
```

日常启动只拉起 `8005` 上的 FastAPI；FastAPI 直接托管已构建的 `frontend\dist`，不会默认启动 Vite 或 `3000`。

服务地址如下：

| 地址 | 用途 |
| --- | --- |
| [http://127.0.0.1:8005/](http://127.0.0.1:8005/) | PlotPilot 作者工作台 |
| [http://127.0.0.1:8005/docs](http://127.0.0.1:8005/docs) | FastAPI OpenAPI 文档 |

结束日常服务时双击 `tools\stop-local.vbs`，或从终端运行 `tools\stop-dev.bat`。两者只处理 `8005`，不会误杀 `3000` 上的其他项目。

### 启动器说明

| 文件 | 适用场景 |
| --- | --- |
| `tools\start-local.vbs` | 日常本机写作入口；无终端启动 `8005`，并打开 `http://127.0.0.1:8005/`。 |
| `tools\stop-local.vbs` | 日常本机写作的无终端停止入口；只处理 `8005`。 |
| `tools\start-dev.bat` | 从终端运行的诊断入口；行为与日常启动相同，但保留真实失败退出码。 |
| `tools\stop-dev.bat` | 从终端停止日常服务；只处理 `8005`。 |
| `tools\start-frontend-dev.bat` | 显式启动 Vue/Vite 源码开发服务器 `3000`，不会替代日常启动。 |
| `tools\stop-frontend-dev.bat` | 停止显式启动的 Vite `3000` 开发服务器。 |
| `tools\plotpilot.bat` | 便携式 GUI 启动器。它要求 Python `3.14.x`，并在没有系统 Python 时查找 `tools\python-3.14.5-embed-amd64.zip`；适合准备好该运行时的分发环境，不是源码开发的推荐入口。 |

## 第一次写一本书

1. 打开工作台，创建或选择一本作品。
2. 在新书设定中补全题材、核心设定、人物、世界观和写作目标。
3. 生成并确认宏观结构与章节规划。结构未确认前，自动驾驶不会把它当作可执行写作计划。
4. 在工作台生成、编辑并保存章节；侧边栏可检查人物、地点、伏笔、关系和章节状态。
5. 准备好连续产出后启动自动驾驶。它会在需要审阅、生成失败或状态不完整时暂停，等待你决定下一步。

建议先以少量章节验证模型、篇幅、文风和嵌入服务，再增加自动驾驶的目标章节数。这样可以在较小范围内发现提示词、模型或设定问题。

## 自动驾驶与规范记忆恢复

每个章节被确认后，PlotPilot 需要把正文转换为后续写作依赖的规范资产，例如章节摘要、叙事事件、知识图谱候选、伏笔状态和向量索引。自动驾驶只会在这些资产提交完成后继续下一章。

如果工作台显示 **“规范章后同步失败”** 或错误码 `canonical_aftermath_not_ready`：

1. 先确认当前章节正文与必要的审阅结果已经保存。
2. 对单章问题，使用工作台中的 **“重新同步本章”**，完成后再核对状态。
3. 如果多个章节的历史记忆都需要重建，使用 **“从第 1 章开始全流程同步”**。该任务会从第 1 章处理到当前章，并显示当前进度与失败原因。
4. 全章重同步完成后，自动驾驶会保持暂停状态。请在工作台检查结果，然后由作者手动继续或重新启动自动驾驶。

不要通过强行刷新、跳过审阅或直接修改数据库来绕过这个暂停。它的作用正是避免下一章在缺失前文规范记忆的情况下继续生成。

## 手动启动与本地部署

不使用 Windows 启动器时，日常本机模式只需启动 FastAPI。运行前请确认已经在 `frontend` 目录执行过 `npm run build`，因为 `8005` 会直接托管构建产物：

```powershell
.\.venv\Scripts\python.exe -m uvicorn interfaces.main:app --host 127.0.0.1 --port 8005
```

然后访问 [http://127.0.0.1:8005/](http://127.0.0.1:8005/)；OpenAPI 文档仍在 [http://127.0.0.1:8005/docs](http://127.0.0.1:8005/docs)。

### Vue 源码开发（可选）

只有需要修改 Vue 源码时才使用 `3000`。先让 `8005` 后端保持运行，再双击 `tools\start-frontend-dev.bat`，或在 `frontend` 目录执行：

```powershell
Set-Location frontend
npm run dev -- --host 127.0.0.1 --port 3000
```

此时工作台地址才是 [http://127.0.0.1:3000/](http://127.0.0.1:3000/)。Vite 的 API 代理仍指向已运行的 `8005`；停止时使用 `tools\stop-frontend-dev.bat`，不要用日常的 `stop-dev.bat` 代替。

Linux 或 macOS 可以使用同样的后端命令流程，将 Windows 虚拟环境路径替换为 `.venv/bin/python`，并使用 `source .venv/bin/activate`。生产构建、Tauri 桌面安装包与维护者打包说明见 [docs/BUILD_INSTALLER.md](docs/BUILD_INSTALLER.md)。

## 常见问题

### `py -3.14` 找不到 Python

安装 Python `3.14.x`，并确认 `py -3.14 --version` 能返回版本号。项目的 Python 版本约束是 `>=3.14,<3.15`，不要用 `3.11` 或 `3.12` 替代。

### 启动器打开后无法访问工作台

先检查 [http://127.0.0.1:8005/docs](http://127.0.0.1:8005/docs)。如果后端未就绪，检查 `.env` 中的配置和 `8005` 端口占用；日常模式不启动 `3000`。只有 Vue 源码开发时才需要检查 `3000`，此时在 `frontend` 目录确认已执行 `npm install`。

### 生成时报 API Key 或模型错误

确认 `.env` 内配置了对应提供方的 API Key、Base URL 和模型名。修改 `.env` 后重启后端。模型服务、网关地址和可用模型由你的服务商账户决定。

### 向量检索或知识图谱初始化失败

检查嵌入服务配置：云端模式需要有效的 `EMBEDDING_API_KEY` 或 `OPENAI_API_KEY`；本地模式需要安装 `requirements-local.txt`，并能访问 `EMBEDDING_MODEL_PATH` 指向的模型目录。

### 如何备份作品

在停止服务后备份本地 `data/` 目录，并妥善保管 `.env`。作品正文、SQLite 数据库和向量索引都可能位于本地数据目录，不应提交到 Git。

## 技术概览

| 层 | 组成 |
| --- | --- |
| 作者界面 | Vue 3、TypeScript、Vite、Naive UI、ECharts |
| 本地 API | FastAPI、Uvicorn、SSE 实时进度推送 |
| 创作运行时 | 章节规划、上下文装配、生成、审阅、章后资产提交与恢复保护 |
| 持久化 | SQLite、单写者调度、ChromaDB / FAISS 向量检索 |
| 模型接入 | Anthropic、方舟、OpenAI、Gemini 与本地嵌入模型配置 |

面向开发者的分层说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，文档索引见 [docs/README.md](docs/README.md)。

## 验证与贡献

后端测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

前端检查：

```powershell
Set-Location frontend
npm run lint
npm run test:unit
npm run build
```

欢迎通过 Issue 或 Pull Request 改进项目。提交前请确保不包含 API Key、`.env`、`data/`、日志、数据库、向量索引、未公开作品内容或办公文档。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)，并附加 Commons Clause 条件限制。详细权利和限制以 [LICENSE](LICENSE) 为准。
