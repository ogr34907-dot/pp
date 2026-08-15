# PlotPilot 架构

PlotPilot 是面向长篇小说创作的本地工作台。当前新书写作以五级大纲、
Candidate-first、Canonical 章后同步和版本化长期记忆为主线。`engine/` 中的
StoryPipeline/daemon 仍作为兼容实现保留，但不是新书正文的写入权威。

产品说明和日常启动方式见 [README.md](../README.md)，章节状态机和直接写入口
分类见 [CHAPTER_LIFECYCLE.md](CHAPTER_LIFECYCLE.md)。

## 真实创作链路

```text
Novel + Bible（时代、世界、人物、地点、规则、故事核心、题材、目标）
  -> 已发布且 synced 的总纲
  -> 部纲 -> 卷纲 -> 幕纲 -> 章纲
  -> GenerationStartPreflight
  -> ContextBuilder / ContextBudgetAllocator
  -> DAG V2
  -> Candidate + machine audit
  -> 作者批准（chapter_review）或机器批准（continuous）
  -> ChapterCandidateRepository.commit_formal()
  -> ChapterAftermathPipeline
  -> ChapterNarrativeSync + exact-version MemoryEngine
  -> ChapterCandidateRepository.mark_sync_succeeded()
  -> 下一章 Candidate
```

两个模式都必须先产生 Candidate。人工模式在作者批准前停止；连续模式也只有在
Formal、Canonical 和 Memory 全部 ready 后才推进。Candidate、草稿、未发布大纲
和旧正文 revision 都不能进入下一章上下文。

## 业务权威

| 业务事实 | 唯一权威 |
|---|---|
| Planning | `OutlineContractRepository` / `OutlineContractService` 的已发布五级链 |
| Candidate | `CandidateChapterWorkflowService` + `ChapterCandidateRepository` |
| Formal write | `ChapterCandidateRepository.commit_formal()` |
| Formal rewrite | `ChapterRewriteCoordinator` |
| Canonical commit | `ChapterNarrativeSync` + `SqliteChapterNarrativeCommitRepository` |
| Canonical visibility | 当前 `content_sha256 + content_revision` 关联的 committed 投影 |
| Memory | `MemoryEngine.update_canonical_version_from_chapter()`，受 narrative claim 约束 |
| Recovery | Candidate run/cursor、narrative claim 和 generation epoch |
| Runtime state | `novel_generation_runs` 与 Candidate repository |
| Observability | 持久化 DAG run / attempt / event；UI 和 shared state 只读投影 |
| Worldline | `WorldlineRegenerationService` + `WorldlineRebuildService` |

## 分层

```text
domain/          领域实体、值对象、仓储协议和业务异常
application/     用例编排：新书初始化、五级规划、Context、Candidate、章后处理
infrastructure/  SQLite 仓储、LLM Provider、向量存储、导出和运行时适配
interfaces/      FastAPI、依赖装配、REST/SSE 边界
engine/          旧 daemon/StoryPipeline 兼容内核和题材扩展
frontend/        Vue 3 工作台
shared/          跨端配置和分类资源
```

主要应用模块：

| 目录 | 职责 |
|---|---|
| `application/onboarding/` | 新书向导与 Bible 初始化 |
| `application/blueprint/` | 五级大纲、发布/同步 Barrier、章节节奏合同 |
| `application/engine/` | Context、DAG V2、Candidate、Aftermath、恢复与 Worldline |
| `application/world/` | Narrative sync、Canonical 状态、人物/因果/伏笔/KG |
| `application/core/` | 小说/章节 CRUD 与正式正文 Rewrite |
| `infrastructure/persistence/database/` | Formal/Candidate/Canonical/Memory 持久化权威 |

## 规划和上下文边界

正文规划只有：

```text
总纲 -> 部纲 -> 卷纲 -> 幕纲 -> 章纲
```

下级生成和发布必须通过已发布、已同步父级 Barrier。章纲中的
`chapter_function`、`intensity_curve`、`chapter_goal`、`chapter_delta`、
`ending_hook`，以及按章节功能要求的 `decisive_choice`、`cost_or_risk`、
`turn_or_payoff` 会进入 Candidate 的 Beat/Context。旧 `story_nodes` 可保存兼容
投影，但其未发布描述不是正文规划输入。

Context Assembly 保留 Bible、人物、地点、规则、已发布大纲、当前章纲、最近正式
章节、Canonical State、最近 500 条 `completed_beats`、已揭示线索、伏笔、叙事债、
长期 Memory 和 Vector Recall。任何一项状态投影都不能反向决定业务提交。

## 兼容运行时

`EngineDaemon`、`BaseStoryPipeline`、PersistenceQueue、StatePublisher、
`AutoNovelGenerationWorkflow` 和旧 continuation 尚被兼容代码引用，因此没有按文件
长度或静态命中直接删除。它们不拥有新书正式正文：

- 旧 autopilot start/resume API 返回 `410`，要求使用 generation API。
- 旧 prose invocation 返回 `410 candidate_first_required`。
- daemon/StoryPipeline 的 completed 写入有无条件 Candidate-first 守卫。
- `StateSnapshotManager.AtomicStateTransaction` 和
  `ChapterGenerationWorkspace.commit_to_chapter()` 当前无生产调用者，不是 Recovery
  或 Formal authority。

## 服务入口

日常本机使用只启动 FastAPI，由 8005 直接托管已构建前端：

```powershell
tools\start-local.vbs
```

访问 `http://127.0.0.1:8005/`，OpenAPI 为
`http://127.0.0.1:8005/docs`。仅修改 Vue 源码时才另外运行：

```powershell
tools\start-frontend-dev.bat
```

开发模式使用 3000，并将 `/api` 代理到 8005。

## 数据和验证

- 主数据库：默认 `data/plotpilot.db`，实际目录由 `application.paths.DATA_DIR` 解析。
- 向量存储：默认 `data/chromadb/`。
- 日志：默认 `logs/plotpilot.log`。
- 配置：以 [.env.example](../.env.example) 为准，密钥不得提交。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -v
Set-Location frontend
npm run lint
npm run test:unit
npm run build
```
