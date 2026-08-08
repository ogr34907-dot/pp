# Canonical Aftermath Full Resync Design

## Goal

为已完成正文提供一个显式、可观察、可断点续跑的全章规范记忆重同步入口，修复单章章后同步失败导致的长期 `canonical_aftermath_not_ready` 闸门，同时不改正文、不绕过现有 CAS 和 MemoryEngine 提交屏障。

## Scope And Defaults

- 只处理当前小说中状态为 `completed` 且正文非空的章节，按正文序号升序处理。
- 全章重同步是显式用户操作；开始前小说进入暂停状态，期间不生成新正文。
- 已满足当前正文哈希、修订号、管线版本、规范摘要、MemoryEngine 和向量状态的章节直接 `skipped`，不调用 LLM；规范状态已 ready 但向量为 `failed`/`not_started` 时进入既有向量重试路径。
- 未就绪章节复用现有 `ChapterAftermathPipeline.run_after_chapter_saved()`、SQLite 规范提交仓储、MemoryEngine 和向量索引。
- 任一章节提交失败、正文版本变化、硬矛盾或关键 SQLite 写入失败，立即停止并暂停在该章节；后续章节不处理。
- 所有提交前执行正文哈希与修订号 CAS；重写或删除造成版本变化时，旧任务只能丢弃，不能写回派生状态。
- 全部章节完成后仍保持暂停，清除规范失败字段；用户必须显式继续自动驾驶。
- 服务重启或 SSE 断线不自动继续调用 LLM；用户再次点击同一入口时，从第一个未就绪章节继续。

## Architecture

新增 `application/engine/services/canonical_aftermath_full_resync.py`，作为全章编排器。它不创建新的数据库、队列、向量库或记忆系统，依赖以下既有组件：

1. `SqliteChapterNarrativeCommitRepository`：读取当前版本、领取终态重试、执行 MemoryEngine 领取和最终 readiness CAS。
2. `ChapterAftermathPipeline`：执行规范抽取、结构化写入、MemoryEngine 同步和向量重试。
3. 共享状态与现有自动驾驶暂停路径：发布当前阶段、当前章节、完成数、总数和失败原因。
4. 既有 SSE 响应模式：把每章状态即时推送给前端，避免长 HTTP 请求等待超时。

每次运行生成一个不影响正文的 `run_id`。运行开始时通过现有 `novels.autopilot_recovery_reason` 的 CAS 标记声明全章重同步；同时使用进程内按小说锁，防止同进程重复运行。数据库标记是跨请求的并发拒绝与诊断依据，章节提交表中的正文版本和状态才是最终事实来源。每处理一章更新一次标记时间；过期标记可由下一次显式操作接管。

## Service Contract

模块提供以下内部接口：

```python
async def resync_all_completed_chapters(
    *,
    novel_id: str,
    database: Any,
    aftermath_pipeline: Any,
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> FullResyncResult
```

`FullResyncResult` 包含 `run_id`、`total_chapters`、`processed_count`、`synced_count`、`skipped_count`、`vector_failed_chapters`、`failed_chapter`、`failure_reason`、`status` 和 `remains_paused`。`status` 取 `completed`、`failed`、`conflict`、`unavailable` 或 `cancelled`。`processed_count` 始终等于已发出终态 `chapter` 事件的数量，`synced_count` 只统计实际运行章后管线的章节，`skipped_count` 只统计完全 ready 的章节。

单章处理顺序固定为：

1. 读取正文、正文哈希和修订号，校验正文非空且哈希一致。
2. 调用 `is_current_version_ready(..., require_memory_sync=True)` 并读取向量状态；规范和 MemoryEngine ready 且向量已 `stored` 则发出 `skipped` 事件，向量未完成则进入既有向量重试路径。
3. 对当前版本的终态规范失败调用已有人工 reclaim；对 `committed + memory_status=failed` 调用已有 MemoryEngine reclaim；没有提交行时直接进入既有章后管线。
4. 调用 `run_after_chapter_saved()`，传入 `expected_content_sha256` 和 `expected_content_revision`。
5. 再次执行带 MemoryEngine 条件的 readiness 检查；失败则记录当前章节并立即返回。

## API And SSE Events

新增：

```text
POST /api/v1/autopilot/{novel_id}/canonical-aftermath/resync-all
```

响应类型为 `text/event-stream`。事件数据使用 JSON：

```json
{"type":"started","run_id":"...","total_chapters":63,"pending_chapters":1}
{"type":"chapter","run_id":"...","chapter_number":62,"action":"skipped","processed":1,"synced":0,"skipped":1,"total":63}
{"type":"chapter","run_id":"...","chapter_number":63,"action":"synced","processed":2,"synced":1,"skipped":1,"total":63}
{"type":"vector","run_id":"...","chapter_number":62,"status":"failed","processed":1,"total":63}
{"type":"failed","run_id":"...","chapter_number":63,"processed":1,"synced":0,"skipped":1,"total":63,"failure_reason":"API returned empty content"}
{"type":"completed","run_id":"...","processed":63,"synced":1,"skipped":62,"vector_failed":[62],"total":63,"remains_paused":true}
```

并发运行返回 HTTP 409；小说不存在返回 404；数据库或管线不可用返回 503。客户端断开时服务捕获取消，保留已提交章节和暂停状态，不把取消误报成成功。

## Frontend Behavior

在现有 canonical aftermath gate 中增加“全章重同步”操作，与“重新同步本章”并列。按钮只在小说未运行或已暂停时可用；运行中展示当前章节、`completed / total` 进度和取消状态。SSE 完成后刷新 `/status`；只有收到 `completed` 且后端状态不再是 `canonical_aftermath_not_ready` 时才显示可继续状态。失败事件显示具体章节和原因，保留再次启动入口。

## Safety And Recovery

- 不修改任何 `chapters.content`，不创建正文分支，不删除原草稿。
- 全章任务不能越过 `chapter_narrative_commits` 的规范提交、MemoryEngine 提交和 story-advance CAS。
- 当前章节或任一中间版本发生重写时，哈希/修订 CAS 使旧任务丢弃；任务不会重新激活旧派生状态。
- 已成功章节只读 readiness，不重复 LLM；规范状态 ready 但向量失败时只执行向量重试，向量失败沿用现有可重试、非规范阻断语义，不阻断后续章节。
- 失败后状态保持 `paused_for_review`，并写入实际失败章节、失败原因、已完成数和总数。

## Verification

后端测试覆盖：全章顺序、ready 跳过、首次缺失提交、终态失败 reclaim、MemoryEngine 失败 reclaim、首个失败停止、正文重写 CAS、重复运行/并发 409、SSE 事件顺序、取消后重启续跑和最终暂停闸门。前端 Vitest 覆盖 API 路径、事件解析、进度显示和失败后再次运行。

验收必须在 Python 3.14 环境执行相关单元/集成测试、30 章回归、100 章慢测、前端单测、`git diff --check`，并在正式区验证 8005/3019 健康和本地嵌入模型路径不变。
