# 16. Issue Register

## Status convention

- `Open`: root cause recorded; no acceptance claim.
- `Implemented, pending acceptance`: source change exists in the inherited
  test-workspace diff but has not passed this audit's final suite.
- `Historical fixed`: already committed in `142052da`; retained here as
  provenance rather than counted as a new working-tree repair.
- `Accepted, pending commit`: targeted and full-suite evidence passed; the
  repair has not yet been committed or pushed.

## Current register

| ID | Severity | Verification | Status | Summary |
| --- | --- | --- | --- | --- |
| BASELINE-001 | P2 | 已确认 | Open condition | The test workspace is intentionally dirty with inherited changes; current audit provenance must state this explicitly. |
| MEMORY-001 | P1 | 动态确认 | Accepted, pending commit | Mock Provider now recognizes `memory-extraction`; focused, long-flow and full-suite evidence passed. |
| MEMORY-002 | P1 | 动态确认 | Accepted, pending commit | Replay head excludes empty planned tails; coordinator, long-flow and full-suite evidence passed. |
| MANUSCRIPT-001 | P1 | 动态确认 | Accepted, pending commit | Compatibility manuscript entity access uses unified tables; FastAPI/SQLite acceptance passed. |
| MANUSCRIPT-002 | P1 | 动态确认 | Accepted, pending commit | Prop mutation responses wait for synchronous visibility; FastAPI/SQLite acceptance passed. |
| API-001 | P2 | 动态确认 | Accepted, pending commit | Whitespace-only manuscript prop holder IDs now return the established 422 validation result instead of leaking a value-object exception. |
| FRONTEND-001 | P2 | 动态确认 | Accepted, pending commit | Empty macro-structure state exposes the existing planning command; build/browser evidence passed. |
| MOCK-E2E-001 | P1 | 动态确认 | Accepted, pending commit | Act planning dispatch is contract-specific; Mock API acceptance and full suite passed. |
| MOCK-E2E-002 | P1 | 动态确认 | Accepted, pending commit | Chapter preplanning has its execution-script response; Mock API acceptance and full suite passed. |
| MOCK-E2E-003 | P1 | 动态确认 | Accepted, pending commit | Chapter prose response is contract-specific; Mock API acceptance and full suite passed. |
| MOCK-E2E-004 | P1 | 动态确认 | Accepted, pending commit | Canonical narrative sync has valid deterministic output; Mock API acceptance and full suite passed. |
| ONBOARD-001 | P1 | 动态确认 | Accepted, pending commit | Setup preview no longer claims durable persistence; targeted/full suite passed. |
| BLUEPRINT-003 | P1 | 动态确认 + 代码级确认 | Accepted, pending commit | Stage ranges stay within target chapters; targeted/full suite and frontend build passed. |
| SETTING-003 | P1 | 动态确认 | Accepted, pending commit | Worldbuilding and locations bind through Variable Hub and appeared in isolated macro evidence. |
| SETTING-004 | P1 | 动态确认 | Accepted, pending commit | Writer context includes the bounded saved-location catalog; targeted/full suite passed. |
| PROMPT-001 | P1 | 动态确认 | Accepted, pending commit | Refresh/resume retains frozen explicit context; actual Mock LLM request regression and full suite passed. |
| TEST-002 | P2 | 代码级确认 | Deferred | `frontend/package.json` has no standalone lint or frontend unit-test script; build and shared config checks are available, but lint/test success cannot be claimed. |

## MEMORY-001: Mock Provider violates MemoryEngine extraction contract

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：Mock LLM / chapter aftermath / explicit chapter replay
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`infrastructure/ai/providers/mock_provider.py:53`; `application/engine/services/memory_engine.py:104`; `application/engine/services/memory_engine.py:626`; `infrastructure/ai/prompt_packages/nodes/memory-extraction/user.md:23`
- 生产入口：`ChapterAftermathPipeline` invokes canonical MemoryEngine update after `chapter-narrative-sync`; explicit `retain_prose` replay uses the same barrier.
- 前端输入位置：章节重写动作，`rewrite_mode=retain_prose`。
- API 字段：`PUT /api/v1/novels/{novel_id}/chapters/{chapter_number}` with `rewrite_mode: retain_prose`.
- 数据库存储：`chapter_narrative_commits` for test novel `novel-1785950463591` records revision 4 as `status=committed`, `memory_status=failed`, `memory_attempt_count=3`.
- 规划读取位置：not applicable; this is post-chapter memory extraction.
- 自动驾驶读取位置：the same aftermath gate prevents advancing a canonical replay with failed memory persistence.
- Prompt 接点：`memory-extraction` requires only `completed_beats`, `revealed_clues`, and `fact_violations`.
- 最终模型请求证据：the rendered contract contains `characters_involved` inside an allowed beat item. Current Mock intent detection sees `characters` and emits the unrelated top-level character payload.
- 触发条件：no-key/default Mock Provider and a chapter reaching MemoryEngine extraction.
- 复现步骤：run the isolated UI/API flow, generate/rewrite chapter 1 with `retain_prose`, and inspect the commit row and backend log.
- 预期行为：Mock returns JSON that validates as `MemoryDeltaPayload`; canonical replay can complete after all existing gates succeed.
- 实际行为：`MemoryDeltaPayload.model_validate()` reports `characters: Extra inputs are not permitted`; retry limit is exhausted and replay returns `chapter_1_canonical_replay_failed: memory_engine_update_failed`.
- 影响范围：local no-key generation, deterministic E2E, and explicit replay verification; real configured providers are not changed by this repair.
- 是否影响新书：yes, when a no-key Mock path produces chapters.
- 是否影响已有书：yes, for explicit replay through the Mock path.
- 是否影响手动写作：yes, if it uses the same aftermath path with the Mock provider.
- 是否影响自动驾驶：yes, the correct gate leaves it paused/failed rather than allowing false progress.
- 是否影响长期记忆：yes, no memory delta is persisted while the bad response is returned.
- 是否可能造成数据损坏：the existing gate prevents silent continuation; repeated failed replay leaves a correctly marked failed memory state rather than corrupting canonical state.
- 根因：`MockResponseFactory._detect_intent()` lacks a `memory-extraction` branch before generic `characters`/角色 matching. The actual memory schema contains `characters_involved`, which triggers the generic branch.
- 建议最小修复：add one high-priority Mock intent and a deterministic payload conforming exactly to `MemoryDeltaPayload`.
- 建议修改文件：`infrastructure/ai/providers/mock_provider.py`; `tests/unit/infrastructure/ai/providers/test_mock_provider.py`.
- 明确不建议修改：do not permit extra fields in `MemoryDeltaPayload`; do not bypass MemoryEngine failure propagation; do not change canonical CAS/retry behavior; do not alter real providers.
- 建议测试：a focused test validates Mock output with `MemoryDeltaPayload.model_validate`; then run Mock Provider, MemoryEngine production-path, replay, and end-to-end tests.
- 修复依赖：none; this is the first remaining blocker before replay acceptance.
- 回滚风险：limited to no-key deterministic provider output; reverting restores the known invalid output and blocked replay.
- 修复结果：focused regression was red with `characters: Extra inputs are not permitted`, then green. The real isolated revision-5 commit recorded `memory_status=committed` after the service restart.

## MEMORY-002: Retained-prose replay includes empty planned chapters

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：chapter rewrite coordinator / explicit retained-prose replay
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`application/core/services/chapter_rewrite_coordinator.py:254-258`; `application/core/services/chapter_rewrite_coordinator.py:653-689`
- 生产入口：`PUT /api/v1/novels/{novel_id}/chapters/{chapter_number}` with `rewrite_mode=retain_prose`.
- 前端输入位置：章节覆盖时选择“保留正文并重放”。
- API 字段：`content`, `rewrite_mode`.
- 数据库存储：test novel has chapter 1 with content revision 5 and planned chapter nodes 2 through 8 with empty content.
- 规划读取位置：structural planning pre-creates empty chapter records; these records are not retained prose.
- 自动驾驶读取位置：mainline is correctly paused after replay exception, but the explicit replay cannot complete.
- Prompt 接点：the first rewritten chapter reaches normal canonical extraction and MemoryEngine successfully; no missing prompt variable is involved.
- 最终模型请求证据：not applicable to the failure point; the failure occurs before an empty future chapter can invoke a model.
- 触发条件：rewrite a nonempty early chapter in a novel whose structure contains later draft/empty chapter rows.
- 复现步骤：append a unique marker to isolated test novel chapter 1 and issue the supported `retain_prose` PUT request.
- 预期行为：replay all retained nonempty prose from the rewritten chapter through the last retained prose chapter, then remain paused for review.
- 实际行为：`_chapter_head()` returns `MAX(number)` across all rows; `_replay_retained_prose()` reaches chapter 2 with empty content and returns HTTP 500 `chapter_2_content_unavailable`.
- 影响范围：any explicit retained-prose rewrite of a book with preplanned future chapter nodes.
- 是否影响新书：yes, planned new books commonly contain empty future chapter rows.
- 是否影响已有书：yes, whenever a rewrite precedes draft nodes.
- 是否影响手动写作：yes, manual chapter overwrite uses this API path.
- 是否影响自动驾驶：the mainline stays paused, which is safe, but users cannot complete a valid replay without deleting/authoring future draft rows.
- 是否影响长期记忆：the rewritten chapter's canonical memory committed, but the replay reports failure and cannot be considered complete.
- 是否可能造成数据损坏：the current fail-closed behavior prevents false completion; it leaves a stale/paused rebuild state that requires manual recovery.
- 根因：the head query uses `SELECT MAX(number) FROM chapters WHERE novel_id = ?` rather than selecting the last row with nonempty retained content.
- 建议最小修复：restrict replay-head selection to nonempty chapter content and leave the replay loop's missing-content failure intact as a defense against a genuine content gap inside the retained range.
- 建议修改文件：`application/core/services/chapter_rewrite_coordinator.py`; `tests/unit/application/services/test_chapter_rewrite_coordinator.py`.
- 明确不建议修改：do not mark empty planned chapters as completed; do not silently skip a missing/nonempty-content gap; do not alter safe-snapshot semantics or the canonical aftermath gate.
- 建议测试：a rewrite with chapters 1-3 containing prose and chapter 4 empty must replay only 2-3; an existing failed canonical replay test must still pause the mainline.
- 修复依赖：MEMORY-001's Mock contract must remain valid so dynamic replay can reach this boundary.
- 回滚风险：limited to retained-prose replay range calculation; no schema or API contract change.
- 修复结果：the focused tail-node regression was red with `chapter_4_content_unavailable`, then green. A real chapter-1 revision-6 API replay returned `200` and `replay_completed=true`; the current chapter hash equals both `chapter_narrative_commits.content_sha256` and `chapter_summaries.source_content_sha256`, canonical and memory states are committed, and the mainline remains `stopped/paused_for_review`.

## ONBOARD-001: Plot-outline preview is labeled as persisted before server persistence

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：new-book setup wizard / AI Invocation adoption / plot-outline persistence
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`frontend/src/components/onboarding/NovelSetupGuide.vue:1256-1268`; `frontend/src/components/onboarding/NovelSetupGuide.vue:2033-2046`; `interfaces/api/v1/engine/generation.py:677-754`
- 生产入口：`Home.vue` creates a book and mounts `NovelSetupGuide`; step 4 receives an AI Invocation result, then only the later `确认修改并继续` action invokes `PUT /novels/{id}/setup/plot-outline`.
- 前端输入位置：新书设置向导第 4 步“剧情总纲”。
- API 字段：preview arrives through the AI Invocation response; durable write is `PUT /api/v1/novels/{id}/setup/plot-outline`.
- 数据库存储：`variable_values` with `variable_key = plot.outline` / `plot.stage_plan` and scope `novel_id:{id}`.
- 规划读取位置：`GET /novels/{id}/setup/plot-outline` reads only `plot.outline` from `SqliteVariableHubRepository`.
- 自动驾驶读取位置：later planning and writing consume the persisted variable hub values, not the browser-only preview.
- Prompt 接点：the accepted setup-plot-outline result is available in the invocation result; it is not yet a durable Prompt input before the explicit save.
- 最终模型请求证据：not applicable at the failure point; the broken assertion is persistence state, before a downstream writing request.
- 触发条件：create a new book through the isolated UI and let the step-4 Mock LLM invocation finish without clicking the final `确认修改并继续` for step 4.
- 复现步骤：on `127.0.0.1:3015`, create `AUDIT_UI_EMPTY_STRUCTURE_A31F` with `target_chapters=2`; reach the plot-outline step. The UI renders `已保存剧情总纲`, then `GET /api/v1/novels/novel-1785955844327/setup/plot-outline` returns `{"plot_outline":null,...}` and no current `plot.outline` row exists in the isolated SQLite database.
- 预期行为：AI completion is labeled as a preview/backfill until the supported save request succeeds; only a completed `PUT` may show durable-save feedback.
- 实际行为：`applyPlotOutlineFromResult()` sets `plotOutlineCommitted = true`, which renders the durable-save alert, even though only `savePlotOutlineEdits()` performs the `PUT`.
- 影响范围：new books using the guided flow; closing or navigating away after the false success can leave the core plot outline absent from planning and prompt context.
- 是否影响新书：yes.
- 是否影响已有书：only when reopening an incomplete setup wizard.
- 是否影响手动写作：indirectly, after the missing setup data reaches writing.
- 是否影响自动驾驶：yes, its future planning sees no persisted plot outline.
- 是否影响长期记忆：no direct mutation, but the missing long-horizon setup contract weakens later continuity.
- 是否可能造成数据损坏：no destructive write; it causes a false success and a missing required durable input.
- 根因：the UI conflates invocation-result adoption with the subsequent persistence boundary.
- 建议最小修复：leave `plotOutlineCommitted` false when applying an invocation result; retain `true` only after `workflowApi.savePlotOutline()` returns successfully.
- 建议修改文件：`frontend/src/components/onboarding/NovelSetupGuide.vue`.
- 明确不建议修改：do not auto-save a user-editable AI preview, do not change the API contract, and do not make browser cache count as persistence.
- 建议测试：real UI/API verification that preview has no persisted-success alert and `PUT` followed by refresh exposes the same saved outline.
- 修复依赖：none.
- 回滚风险：limited to accurate setup-state feedback; the existing explicit save path remains unchanged.

## BLUEPRINT-003: Plot-outline chapter ranges are not bounded by the novel target

- 严重级别：P1
- 验证程度：动态确认 + 代码级确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：setup plot outline normalization / new-book wizard range editor
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`application/blueprint/services/setup_plot_outline_continuation.py:254-369`; `frontend/src/onboarding/plotOutlineModel.ts:102-157`; `frontend/src/components/onboarding/NovelSetupGuide.vue:1080-1088`; `interfaces/api/v1/engine/generation.py:719-724`
- 生产入口：new-book wizard step 4, manual `PUT /novels/{id}/setup/plot-outline`, and setup-plot-outline AI Invocation continuation.
- 前端输入位置：new-book advanced target chapter count and the five stage range editors.
- API 字段：`target_chapters`, `plot_outline.stage_plan[].chapter_start`, and `plot_outline.stage_plan[].chapter_end`.
- 数据库存储：`novels.target_chapters`; durable stage plan lives in `variable_values` under `plot.stage_plan` / `plot.outline`.
- 规划读取位置：the setup plot-outline GET endpoint and later planning read those variable values.
- 自动驾驶读取位置：the macro/continuous planning path assumes stage and target chapter bounds are coherent.
- Prompt 接点：setup plot-outline invocation receives `novel.target_chapters`; a contradictory stage plan can be emitted into subsequent planning context.
- 最终模型请求证据：the UI/API trace created a valid novel with `target_chapters=2` and rendered stage ranges `1-1`, `2-2`, `3-3`, `4-4`, and `5-5`; this violates the target before any later model request.
- 触发条件：a short novel (`target_chapters < 5`) or any manual/LLM stage range greater than the target.
- 复现步骤：the isolated UI creation trace for `AUDIT_UI_EMPTY_STRUCTURE_A31F` shows `target_chapters=2` in SQLite and stages through chapter 5. Static evaluation shows backend `_chapter_ranges(2) == [(1, -2), (-1, -1), (0, 0), (1, 1), (2, 2)]`; frontend `buildStageChapterRanges(2)` coerces the total to 5. The backend then preserves valid-looking manual ranges without an upper-bound check and computes `total_chapters = max(target_chapters, raw_end)`.
- 预期行为：every generated or manually saved stage range is within `1..target_chapters`; small books may compress five semantic phases onto shared chapters but may never invent later chapters.
- 实际行为：the frontend creates five distinct chapter ranges for a two-chapter book, and the backend can persist any positive user-provided end beyond the target.
- 影响范围：short-book onboarding, manual outline editing, setup invocation adoption, and downstream macro planning.
- 是否影响新书：yes.
- 是否影响已有书：yes, when a user edits/re-saves a setup outline.
- 是否影响手动写作：indirectly through an invalid long-horizon plan.
- 是否影响自动驾驶：yes, it can receive impossible chapter-stage boundaries.
- 是否影响长期记忆：indirectly, because impossible planning can advance the wrong structural state.
- 是否可能造成数据损坏：it can persist a contradictory plan, though existing later guards may stop some advances.
- 根因：both front- and back-end range normalizers assume five unique phases require at least five chapters; the backend additionally treats out-of-range manual input as a larger effective total.
- 建议最小修复：generate compressed, in-bound ranges for targets 1–4; reject out-of-target manual input at the backend; keep the frontend editor and validation constrained to the actual novel target.
- 建议修改文件：`application/blueprint/services/setup_plot_outline_continuation.py`; `tests/unit/application/blueprint/test_setup_plot_outline_continuation.py`; `frontend/src/onboarding/plotOutlineModel.ts`; `frontend/src/components/onboarding/NovelSetupGuide.vue`.
- 明确不建议修改：do not alter the five-stage plot-outline schema, broaden `target_chapters`, create placeholder chapters, or change macro planning/storage architecture.
- 建议测试：backend red/green tests for a two-chapter outline and an out-of-bounds manual range; browser verification that a two-chapter wizard shows no range over 2 and the persisted GET result remains bounded after explicit save.
- 修复依赖：ONBOARD-001 must be repaired first so the UI acceptance observes the real save boundary.
- 回滚风险：only setup stage-range behavior; normal targets of five or more retain the established phase split.

## SETTING-003: Autopilot macro prompt omits persisted worldbuilding and locations

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：autopilot macro planning / AI Invocation CPMS input bindings
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`application/ai_invocation/contracts/autopilot_planning.py:121-181`; `infrastructure/ai/prompt_packages/nodes/planning-quick-macro/user.md:1-11`.
- 生产入口：`POST /api/v1/autopilot/{novel_id}/start` -> `EngineDaemon` -> `macro_planning_delegate` -> `autopilot.macro.plan` AI Invocation.
- 前端输入位置：new-book Bible/worldbuilding and location setup; values are saved before starting autopilot.
- API 字段：Bible `GET /api/v1/bible/novels/{novel_id}/bible` and setup-variable endpoints; the macro invocation is observable at `GET /api/v1/ai-invocations/{session_id}`.
- 数据库存储：for the isolated novel, current `variable_values` rows exist for both `worldbuilding.content` (nonempty five-dimension object, version 3) and `locations.list` (three entries, version 3) in scope `novel_id:novel-1785955844327`.
- 规划读取位置：the legacy `ContinuousPlanningService.build_quick_macro_variables()` formats both data sets, but the default autopilot AI Invocation path resolves only CPMS bindings.
- 自动驾驶读取位置：`ensure_autopilot_macro_plan_contract()` persists the active input binding set used by the review-gated macro invocation.
- Prompt 接点：`planning-quick-macro/user.md` renders `{{worldbuilding.content}}` under `【世界观】` and `{{locations.list}}` under `【地点】`.
- 最终模型请求证据：the isolated pending session `40862a15-8155-4dae-ab26-01ad42299beb` resolves `worldbuilding.content` and `locations.list` as `invalid`, with `variable_key=""`, `source=cpms_template`, and an empty value; its rendered prompt contains both empty headings. The same session resolves `characters` from `novel.characters.list` successfully.
- 触发条件：start the default autopilot macro-planning flow for any novel with persisted Bible worldbuilding or locations.
- 复现步骤：use the isolated UI/API test novel, persist the setup plot outline, start autopilot, stop at the required pre-call review, then inspect the session variable plan without accepting the invocation.
- 预期行为：both persisted variables are resolved from the current novel scope and rendered into the exact template sections before a macro plan can call the model.
- 实际行为：the contract's `variable_keys` map has no entries for either declared template alias. They become optional generic CPMS template bindings instead of variable-hub bindings, so the final prompt loses both values.
- 影响范围：all default autopilot macro plans; a missing worldbuilding/location contract can cause the macro structure and all later downstream plans to ignore core user setting.
- 是否影响新书：yes.
- 是否影响已有书：yes, whenever their existing Bible data is used through default autopilot macro planning.
- 是否影响手动写作：not directly; the confirmed break is the default autopilot invocation path.
- 是否影响自动驾驶：yes.
- 是否影响长期记忆：indirectly, because the structural plan that later chapters inherit lacks those source constraints.
- 是否可能造成数据损坏：no direct write corruption, but it can persist an under-constrained plan derived from an incomplete model request.
- 根因：the manually maintained autopilot macro `variable_keys`/type sets drifted from the CPMS template declarations. Existing setup contracts use the declared aliases directly and do not have this gap.
- 建议最小修复：bind `worldbuilding.content` to `worldbuilding.content` as an object and `locations.list` to `locations.list` as a list in the existing macro contract; do not add a second context system or bypass the AI Invocation review gate.
- 建议修改文件：`application/ai_invocation/contracts/autopilot_planning.py`; focused contract and resolver regression tests.
- 明确不建议修改：do not alter the Bible tables, Variable Hub aliases, prompt template, provider, StoryPipeline, or review policy for this binding defect.
- 建议测试：first prove the existing contract does not register the two variable-hub keys, then prove the repaired binding set resolves nonempty values and the rendered final prompt contains both markers through the real resolver/CPMS assembler path.
- 修复依赖：none; this is required before continuing the currently paused macro-plan invocation.
- 回滚风险：limited to the macro-plan input binding metadata; a rollback restores the confirmed omitted setting behavior.

## SETTING-004: Chapter prose context omits the saved location catalog before a location is named

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：chapter context assembly / ContextBudgetAllocator / StoryPipeline prose composition
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`application/engine/services/context_budget_allocator.py:1305-1482`; `application/engine/services/context_budget_allocator.py:987-1055`; `application/engine/services/context_builder.py:220-313`; `application/workflows/auto_novel_generation_workflow.py:373-459`; `engine/pipeline/prose_composer.py:61-78`.
- 生产入口：`POST /api/v1/autopilot/{novel_id}/start` -> `EngineDaemon` -> `BaseStoryPipeline._step_build_context()` -> `AutoNovelGenerationWorkflow.prepare_chapter_generation()` -> `ContextBuilder.build_structured_context()` -> `ChapterProseInvocationComposer`.
- 前端输入位置：new-book Bible/location setup.
- API 字段：Bible location API and setup `locations.list`; the rendering result is observable at `GET /api/v1/ai-invocations/{session_id}`.
- 数据库存储：the isolated novel `novel-1785955844327` has three persisted `bible_locations` rows and a current Variable Hub `locations.list` value at version 3.
- 规划读取位置：macro planning now reads the list through the repaired `SETTING-003` Variable Hub binding.
- 自动驾驶读取位置：the chapter-writing workflow reads Bible data through `ContextBudgetAllocator`, not the macro-plan binding set.
- Prompt 接点：`chapter-prose-generation` renders the full `continuity_context` emitted by the StoryPipeline context bundle.
- 最终模型请求证据：the saved unaccepted prose session `0ee63698-c759-4c8c-b199-5b90825e2d78` currently contains all 16 persisted worldbuilding leaves in `continuity_context` and the rendered prompt, but zero of three persisted location names. A fresh isolated `ContextBuilder` reproduction with the same chapter outline reports `world_leaves=16/matches=16`, `location_names=3/matches=0`, and `matched_by_chapter_outline=0`.
- 触发条件：a new chapter whose outline and scene-director payload do not already mention one of the saved location names.
- 复现步骤：use the isolated two-chapter novel, retain its saved five-dimension worldbuilding and three Bible locations, build chapter 1 context with its persisted outline, then inspect only marker counts in the structured payload and final AI Invocation snapshot.
- 预期行为：a bounded, current-novel location catalog enters the chapter main context before prose generation, so a model can select and respect available locations even when the first chapter outline is not yet location-specific.
- 实际行为：`_format_scene_location_hints()` is the only direct Bible-location renderer in the chapter context path; it emits entries only when the outline or scene director already contains a matching location name. The unmentioned catalog never reaches a slot or final Prompt.
- 影响范围：new-book first chapters and any later chapter whose preplanning output does not name a saved location; worldbuilding is not affected by this finding.
- 是否影响新书：yes.
- 是否影响已有书：yes, for books with saved locations and no matching current outline term.
- 是否影响手动写作：yes, where the same `ContextBuilder` powers chapter generation.
- 是否影响自动驾驶：yes.
- 是否影响长期记忆：indirectly, because the model may invent a location instead of using the saved spatial model.
- 是否可能造成数据损坏：no direct write corruption; it creates an under-constrained prose request.
- 根因：the context slot inventory includes the narrative worldbuilding contract, worldbuilding-core, and triggered scene-location hints, but has no bounded unconditional location-catalog slot. The location list therefore has no carrier until after a name was already selected upstream.
- 建议最小修复：add one bounded T1 location-catalog slot sourced from the existing Bible repository, preserving canonical order and only active saved records; keep triggered scene-location hints as the higher-specificity supplement.
- 建议修改文件：`application/engine/services/context_slot_providers.py`; `application/engine/services/context_budget_allocator.py`; focused ContextBudgetAllocator regression tests.
- 明确不建议修改：do not alter CPMS `chapter-prose-generation` variables, Bible tables, Variable Hub aliases, prompt templates, Provider behavior, or StoryPipeline review policy; do not add a second context or location persistence system.
- 建议测试：first prove a saved location absent from the outline is absent from the final allocated context; then prove it appears exactly once in the new bounded slot, while an outline-selected location is not duplicated and the configured token budget is respected.
- 修复依赖：SETTING-003 remains required for macro planning but this writer-path repair is independently scoped.
- 回滚风险：limited to one T1 context block; removing it restores the confirmed omission without changing persisted data or APIs.

### SETTING-004 evidence clarification

The initial prose-session review described worldbuilding as absent. A later read of the persisted, unaccepted session snapshot from the same isolated database disproved that sub-claim: all 16 saved worldbuilding leaves are present in both the `continuity_context` alias and final rendered user prompt. This issue is intentionally narrowed to the independently reproducible location omission; the evidence record does not retain raw user settings or prompt text.

### `has_outline` assessment

The same API trace showed `GET /api/v1/novels/{id}` reporting `has_outline=false` while `GET /setup/plot-outline` returned a valid persisted outline. This is **not** the root cause of `SETTING-003`: `NovelService._check_has_outline()` intentionally reports whether the structural StoryNode tree contains an `ACT`, not whether the setup-specific `plot.outline` variable exists. Before macro planning creates ACT nodes, `false` is expected; existing service and integration tests encode that contract, and the only current frontend consumer does not use it as a plot-outline persistence indicator. No API behavior change is proposed without separate evidence that a caller depends on the alternate meaning.

## PROMPT-001: Review refresh drops the frozen StoryPipeline context snapshot

- 严重级别：P1
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 所属模块：AI Invocation review / prompt refresh / StoryPipeline prose continuation
- 审计提交：`142052da869cd9c2e6dbca194ff1f22d143b540d` plus current test-workspace diff
- 代码位置：`interfaces/api/v1/engine/ai_invocation_routes.py:_resolve_current_variable_plan`; `interfaces/api/v1/engine/ai_invocation_routes.py:_refresh_session_variables_from_hub`; `engine/pipeline/prose_composer.py:_build_variables`.
- 生产入口：`StoryPipeline` creates a review-gated `autopilot.chapter.prose` session; the UI loads it through `GET /api/v1/ai-invocations/{session_id}`, then resumes or retries it through the same router.
- 前端输入位置：AI Invocation review panel.
- API 字段：`continuity_context` is a prepared explicit input; `chapter.continuity_context` is its Variable Hub binding.
- 数据库存储：the isolated prose session `0d8260ac-8932-40f6-9a1a-1935a8c7632d` persists a 5,266-character explicit context snapshot and rendered Prompt before review.
- 规划读取位置：not applicable; this is the writer review boundary after context assembly.
- 自动驾驶读取位置：resume and retry re-render the persisted session immediately before creating an LLM attempt.
- Prompt 接点：`chapter-prose-generation` renders `continuity_context`.
- 最终模型请求证据：before the repair, an isolated `GET` returned a 10-character Hub value and an 1,889-character rendered Prompt despite the persisted snapshot containing the full context. The regression test captures the actual Mock LLM Prompt after `resume`; after the repair it contains the full explicit marker and excludes the stale Hub marker. A fresh real isolated API read reports 16/16 worldbuilding leaves, 3/3 locations, one `LOCATION_CATALOG` header, zero missing variables, zero diagnostics, and zero attempts.
- 触发条件：a prepared prose session has an explicit context value that differs from the current `chapter.continuity_context` Variable Hub value.
- 复现步骤：create a review-gated chapter prose session with a unique explicit context marker, replace only its chapter-scoped Hub value with a different marker, then read or resume the session.
- 预期行为：review, preview, resume, and retry preserve frozen explicit inputs supplied at invocation preparation; only Hub-originated values refresh automatically.
- 实际行为：the route preserved only runtime/genre aliases, resolved all ordinary bindings again from Variable Hub, and re-rendered the prompt from that replacement plan. Resume and retry would then persist and send the degraded prompt.
- 影响范围：review-gated prose sessions, especially StoryPipeline full-context invocations; any non-runtime explicit variable used by an editable invocation can be affected.
- 是否影响新书：yes, first chapter review can lose the prepared Bible/location context.
- 是否影响已有书：yes.
- 是否影响手动写作：yes, when it uses the AI Invocation review flow.
- 是否影响自动驾驶：yes, paused automatic writing resumes through this route.
- 是否影响长期记忆：indirectly, because a degraded chapter request can create continuity drift.
- 是否可能造成数据损坏：not a direct database corruption, but it can produce an incorrectly constrained chapter after user review.
- 根因：`_runtime_only_explicit_variables()` discarded all values whose lineage was `explicit` unless they were runtime/genre aliases. `GET`, draft preview/save, `resume`, and `retry` re-used that incomplete map.
- 建议最小修复：preserve raw values whose persisted lineage is `explicit`, use the shared resolver in all refresh paths, and exclude only aliases explicitly replaced through the variables API so user edits still win.
- 建议修改文件：`interfaces/api/v1/engine/ai_invocation_routes.py`; `tests/unit/interfaces/api/v1/test_chapter_prose_invocation_routes.py`.
- 明确不建议修改：do not weaken Variable Hub refresh for Hub-backed fields; do not copy prompt text into a second cache; do not bypass the review gate or change CPMS templates.
- 建议测试：a route test must assert both review refresh and actual resumed Mock LLM Prompt retain the frozen context while rejecting a stale Hub replacement.
- 修复依赖：SETTING-004 supplies the location catalog that this repair must preserve through review.
- 回滚风险：limited to session refresh precedence; reverting reintroduces the confirmed degraded-prompt path.
- 修复结果：the new test was red with the stale Hub marker in the rendered prompt, then green. The complete route module has 8 passing tests; final suite acceptance remains pending.

## Final acceptance update

All current P1/P2 repairs above passed their targeted verification and the
fresh default full backend suite. Explicit 30/100 chapter slow tests, Mock API
acceptance, frontend shared-config/build, and isolated browser acceptance also
passed. The status is intentionally "Accepted, pending commit" until the
post-documentation sensitive-file review, commit and remote verification are
complete.

## API-001: Whitespace holder ID leaked a value-object exception

- 严重级别：P2
- 验证程度：动态确认
- 执行者：当前 Codex（禁止子代理）
- 代码位置：`interfaces/api/v1/core/manuscript_entity_routes.py:_validate_holder`
- 触发条件：manuscript prop API receives a non-empty but whitespace-only
  `holder_character_id`.
- 根因：the newly adopted `CharacterId` value object correctly rejects
  whitespace, but the route did not translate that validation failure to its
  established HTTP error contract.
- 最小修复：catch only `ValueError` at this API boundary and use the
  existing 422 holder-not-found validation response.
- 红绿证据：the new integration regression first failed with
  `ValueError: Character ID cannot be empty`; after the route change it
  passed, and the six-test manuscript route module plus the fresh 2056-test
  full suite passed.
- 明确不修改：no change to CharacterId validation, unified repository lookup,
  response schema or Write Dispatch behavior.

## Inherited follow-up repair boundaries

`MANUSCRIPT-001`, `MANUSCRIPT-002`, `FRONTEND-001`, and `MOCK-E2E-001` to
`MOCK-E2E-004` are not being reimplemented from scratch. Their current source
and tests are preserved as inherited work. This audit will inspect the full
diff, rerun their targeted tests, and include them in a commit only if they
meet the same acceptance gates as `MEMORY-001`.

## Historical committed issues

The previous full audit recorded `DATA-001`, `BLUEPRINT-001`, `DB-001`,
`DB-001b`, `SETTING-001`, `SETTING-002`, `AUTOPILOT-001`, `DB-002`, `DB-003`,
`BLUEPRINT-002`, `SSE-001`, and `OBS-001` as fixed in committed baseline
`142052da`. Their original evidence remains under
`docs/audit/2026-08-05_bf839a1/`; current regression execution will test their
interfaces rather than duplicate their old root-cause investigation.
