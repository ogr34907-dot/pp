# 章节生命周期与写入边界

本文记录当前新书工作流的真实权威和兼容入口。它描述代码行为，不以 README、UI
动画或 legacy shared state 作为业务事实。

## 前置条件

`GenerationStartPreflight` 在创建 run 前 fail-closed 检查：

- `novels.target_chapters` 是全书目标章节数唯一来源；
- 五级大纲已发布并同步，当前章有有效章纲；
- 正式章节从第 1 章连续，来源和 hash/revision 可证明；
- 没有活跃 generation epoch、未完成全量 Canonical resync 或 Worldline rebuild；
- 目标章节位没有冲突 Candidate/Formal。

正文上下文只读取已发布五级链及当前版本 Canonical 投影。未发布大纲、Candidate、
旧 revision 和废弃世界线不会作为下一章事实。

## 人工审核模式

```text
generate_next
  -> DAG V2 生成 Candidate revision
  -> machine audit
  -> awaiting_review
  -> 作者可编辑 Candidate
  -> 旧 audit/commit_plan stale
  -> re-audit（不重新生成正文）
  -> approve
  -> commit_formal
  -> Canonical aftermath + exact-version Memory
  -> mark_sync_succeeded
  -> 下一章
```

`awaiting_review` 期间，正文只存在 Candidate 表和 Candidate 版本历史中。它不写
`chapters` 的 completed 内容，不创建 committed narrative claim，不更新 Memory/
Vector，也不触发 N+1 LLM。

作者编辑正式章节走 `ChapterRewriteCoordinator`，不是 Candidate edit。协调器使用
hash/revision CAS，将本章及后续 Candidate、摘要、事实、人物状态、事件、伏笔、
因果、Vector、Memory 和 narrative commit 标为 stale，并建立 Worldline/rebuild
Barrier。

## 连续模式

```text
Candidate
  -> machine audit
  -> 安全决策通过
  -> commit_formal
  -> ChapterAftermathPipeline
  -> ChapterNarrativeSync committed
  -> MemoryEngine committed
  -> mark_sync_succeeded / Candidate cursor advance
  -> 下一章
```

连续模式不绕过 Candidate。审核硬阻断、结果不确定、Canonical/Memory 失败、停止请求
或版本竞争都会暂停。同步失败的 Candidate 已经拥有 Formal，不能编辑或重新生成；
只能重试同一 hash/revision 的 aftermath。

## Formal、Canonical 与 Memory Barrier

Formal 身份至少包含：

```text
novel_id + chapter_number + content_sha256 + content_revision + candidate_id
```

`ChapterCandidateRepository.commit_formal()` 在事务中验证 Candidate 状态、审核版本、
commit plan、连续 Formal head 和 generation epoch，然后创建或提升 `chapters` 行并
记录 formal provenance。Formal 成功不表示章节生命周期结束。

`ChapterNarrativeSync` 只对当前 Formal hash/revision claim Canonical 工作。其提交表
`chapter_narrative_commits` 同时记录 pipeline version、status、vector status、
memory status 和 advance status。`ChapterAftermathPipeline` 在 Narrative committed 后
调用 `MemoryEngine.update_canonical_version_from_chapter()`；Memory claim 与正文的
hash/revision 相同，旧版本结果不能发布。

`ChapterCandidateRepository.mark_sync_succeeded()` 再次以 `BEGIN IMMEDIATE` 校验
Candidate、Formal、实际章节、epoch、run cursor 和同步状态。任一值变化都 rollback，
不会把过期同步发布为 ready。

## 崩溃恢复与 Exactly Once

- generation run、Candidate、DAG attempt/event 和 Formal sync 状态持久化在 SQLite。
- 服务重启在 `BEGIN IMMEDIATE` 内重新读取状态，释放被中断的 exact-version
  narrative/Memory claim，并把同一 Formal Candidate 置为可重试同步。
- 已完成同步的并发结果使恢复安全 no-op；恢复不能覆盖 committed Candidate 或新 run。
- legacy StoryPipeline 的 `advance_story_pipeline_once()` 只接受当前章节 hash/revision、
  committed narrative claim、committed summary，以及（调用方要求时）
  `memory_status='committed'`。`advance_status` CAS 和连续游标保证重试只推进一次。
- 新书 Candidate run 的下一章游标由 `ChapterCandidateRepository` 在同步成功后推进；
  legacy `novels.current_auto_chapters` 不是新书 Candidate authority。

## Worldline

`WorldlineRegenerationService` 的 preview/execute 都使用
`ChapterCandidateRepository.assert_formal_history_is_proven()` 和
`formal_chapter_head()`。空草稿或手工占位不扩大正式尾部。废弃尾部完成失效和
`WorldlineRebuildService` 重建前，generation preflight 禁止生成。

## Authority Map

| Authority | 实现 |
|---|---|
| Planning | `OutlineContractRepository` / `OutlineContractService` |
| Candidate | `CandidateChapterWorkflowService` / `ChapterCandidateRepository` |
| Formal write | `ChapterCandidateRepository.commit_formal()` |
| Formal rewrite | `ChapterRewriteCoordinator` |
| Canonical transaction | `ChapterNarrativeSync` / `SqliteChapterNarrativeCommitRepository` |
| Canonical visibility | 当前 chapter hash/revision + committed projection join |
| Memory | `MemoryEngine.update_canonical_version_from_chapter()` |
| Recovery | Candidate run/cursor + exact-version claim + generation epoch |
| Runtime | `novel_generation_runs` / Candidate repository |
| Observability | durable DAG run/attempt/event |
| Worldline | `WorldlineRegenerationService` / `WorldlineRebuildService` |

## Chapters INSERT/UPDATE 分类

以下是生产代码中所有正文表写入类别。测试 fixture 不属于运行时入口。

| 位置 | 分类 | 保护条件 |
|---|---|---|
| `chapter_candidate_repository.py` | 新书 Formal authority | Candidate/audit/plan/epoch/连续 head/hash/revision 事务校验 |
| `chapter_rewrite_coordinator.py` | 正式正文合法改写 | 当前 hash/revision CAS，下游 stale + rebuild Barrier |
| `sqlite_chapter_repository.py` | 草稿、规划占位、metadata、删章重编号 | 已 completed/Canonical 正文内容变化拒绝，要求 RewriteCoordinator |
| `persistence_queue.py` | legacy 草稿/metadata 队列 | 已 completed/Canonical 正文覆盖拒绝；completed 生产调用被上游 Candidate-first 挡住 |
| `state_publisher.py` | legacy 状态投影 | status-only 队列方法当前无生产调用者，不能作为 Formal authority |
| `daemon_host.py` | legacy daemon 草稿/兼容 SQL | completed 写入前无条件 `candidate_first_required`；公开 legacy start/resume 为 410 |
| `autopilot/continuations.py` | legacy beat 草稿 | completed transition 无条件拒绝；公开 prose operations 为 410 |
| `autopilot_recovery_policy.py` | 中断草稿清理 | 仅 `status != completed`，清空未完成内容/章前规划 |
| `chapter_narrative_sync.py` | Canonical 后处理 metadata | 只更新张力/摘要相关字段，不替换正文 |
| `connection.py` / narrative commit repository | 迁移和身份补齐 | 从现有正文计算 hash，claim 按实际 hash/revision fail-closed |
| `state_snapshot_manager.py` | 未接线 legacy 原子快照写入 | 当前无生产调用者，不是 Formal/Recovery authority |
| `chapter_generation_workspace.py` | 未接线 workspace commit helper | 只有单测调用；当前 StoryPipeline 不使用它提交 |
| `scripts/backup_novel.py` | 显式管理员备份恢复 | 离线数据恢复工具，不是生成链路 |

`NovelService.add_chapter()`、`ChapterService.ensure_chapter()`、Continuous Planning 和
Chapter Preplanning 通过 `SqliteChapterRepository` 只创建 `draft`/空占位。公开旧
Chapter Review 写入返回 `410 candidate_first_required`，不能将草稿批准为 Formal。

## Narrative Commit、Memory 与 Advance 分类

- `SqliteChapterNarrativeCommitRepository` 是 narrative claim、Memory claim、状态和
  legacy advance 的唯一事务仓储；迁移代码只补列/补身份，不是运行期第二权威。
- `ChapterAftermathPipeline` 是生成链路中唯一 Memory 调用编排。正常依赖装配具有 DB，
  因而使用 durable claim 和 `update_canonical_version_from_chapter()`；无 DB 的兼容/
  单测适配才使用无版本方法。
- `AutoNovelGenerationWorkflow.post_process_generated_chapter()` 仍含 legacy 无版本
  Memory 调用，但它只由被 Candidate-first 阻断的旧 daemon/StoryPipeline 路径使用；
  对外 legacy/hosted prose 入口同样为 `410`，不是新书 generation API。
- `advance_story_pipeline_once()` 与 recovery 只有 `writing_delegate.py` 调用；它们属于
  legacy cursor，并强制当前 Canonical summary 和 Memory Barrier。新书下一章由
  Candidate repository 的 sync-success transaction 决定。

## 禁止的旁路

以下行为均不是受支持的正式写入：

- LLM 直接写 `chapters.status='completed'`；
- 通过普通 CRUD/review 把草稿升级为 Formal；
- 未经 current hash/revision claim 写 Memory、Vector 或 narrative facts；
- Formal 刚写入就生成 N+1；
- 使用 shared state、SSE 或前端状态替代持久化业务状态；
- Worldline rebuild ready 前继续生成。
