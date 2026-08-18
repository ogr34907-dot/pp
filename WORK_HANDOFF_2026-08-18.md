# PlotPilot 当前 main 复核审计与 GPT-5.6 Luna 最小修复执行工单

## 工作交接报告

**交接日期：** 2026-08-18  
**仓库：** `https://github.com/ogr34907-dot/pp`  
**交接原因：** 用户要求暂停当前修复工作，将测试区现有修改和本报告保存到远程 `work` 分支。  
**重要声明：** 本报告是当前实际进度记录，不代表工单已经全部完成。未完成项目必须由后续执行者继续复核、修复和测试。

## 1. 工作区与 Git 基线

| 项目 | 当前事实 |
|---|---|
| 执行工作区 | `W:\novel\test` |
| 保护工作区 | `W:\novel\work`，本次未修改，保持 clean |
| 本轮开始分支 | `main` |
| BASE_SHA | `4cb7be33837efca5a3e8ae721a1f7c1d28cd3709` |
| `origin/main`（本地已知） | `4cb7be33837efca5a3e8ae721a1f7c1d28cd3709` |
| Git 提交身份 | `ogr34907-dot <ogr34907-dot@users.noreply.github.com>` |
| Python | `3.14.7` |
| 当前交接分支 | `work` |

测试区原本在 `main` 上存在未提交修改。本次没有 reset、clean、rebase、强制推送，也没有覆盖或删除这些修改；它们会原样随本报告提交到 `work` 分支。

## 2. 已完成的代码修改

以下是已经写入测试区、将随 `work` 分支保存的修改。这里的“已完成”表示对应代码改动已经实施；不等于整项工单已经通过最终验收。

| 工单项 | 当前完成情况 | 已实施内容 |
|---|---|---|
| FIX-001 | 已修改 | Continuous 正常启动不再由前端在 `/start` 后重复调用 `/run-continuous`；`GenerationRunCoordinator.claim()` 先识别同 novel、同 generation epoch 的存活 Runner，使重复 Claim 幂等成功。 |
| FIX-002 | 已修改，测试仍需补齐 | 将决定 N 到 N+1 是否允许推进的 blocking governance decision 提升到 Aftermath barrier；Candidate-owned run 的暂停/CAS 异常不再伪装成 Legacy fallback；非阻断 enrichment 仍留在 auxiliary 路径。 |
| FIX-003 | 已修改 | 增加局部 `WritingStageResult` 语义，让 StoryPipeline 的真实失败由 NovelLifecycle 识别，不再因为 novel 仍为 RUNNING 就记录本轮成功；HARD_FAIL 保持暂停并避免同章立即循环。 |
| FIX-004 | 已修改 | 已提交正文查询区分“没有记录”和数据库读取异常；数据库异常通过 `CommittedContentLookupError` 传播，避免把异常误当成 NOT_FOUND 后再次调用正文 LLM。 |
| FIX-005 | 已修改 | Runtime 在依赖准备后再启动 Generation Runner；shutdown 改为异步等待 Runner 实际退出，并保留进程强制退出时的 best-effort cancel 路径。 |
| FIX-006 | 已部分完成 | Candidate durable commit 与 continuation claim 的结果分离；Manifest cohort publish 也区分主发布和自动继续；API 增加 `continuation_started`、`continuation_error` 等结果字段；前端已开始提供自动继续失败后的重试入口。 |
| FIX-007 | 已部分完成 | Outline retry 参数在非空时才传给旧 service，保持旧 fake service 兼容；Working Tree/API 增加最近 cohort attempt 投影，包括状态、错误、retry 关系和层级信息。 |
| FIX-008 | 部分完成 | 已开始处理 Candidate 多小说重启恢复的隔离问题；多小说恢复异常隔离的完整回归测试尚未补齐，需后续继续确认代码闭环。 |
| VERIFY-013 | 已有修改，仍需明确回归测试 | 已按 Base Voice Rewrite 的当前实现处理 rewrite 状态相关逻辑；尚未完成工单要求的明确回归测试和最终证据整理。 |
| VERIFY-014 | 已复查并已有修改 | 当前 UI 启动路径已切到 Candidate/Generation 主链，Legacy Direct Autopilot 不再作为当前正常 UI 启动入口；仍需在最终验收中补充可复核的 grep/测试证据。 |

## 3. 尚未完成的工作

以下项目在暂停时明确没有完成，不得在后续交接中误报为已验收：

1. 修复 `CandidateReviewDesk.vue` 对旧 Mock 或兼容实现返回 `undefined` 的处理。当前 `approveAndCommit()` 结果被直接读取 `continuation_error`，导致定向前端测试有 2 个失败。
2. 完成 FIX-006 的后端 API 回归测试，覆盖 durable commit/publish 成功但 continuation claim 失败时，主操作仍返回成功的语义。
3. 完成 FIX-007 的 cohort failed/cancelled 用户 Retry 闭环，包括前端“重试生成”按钮、接口联动和对应测试。
4. 完成 FIX-008 的多小说重启恢复异常隔离测试，并依据测试结果补足实现。
5. 完成 FIX-009：将长期 `continuity_context` 的 Prompt 语义从“近 3 章”改为长期连续记忆，并补变量/Prompt 测试。
6. 完成 FIX-010：从第 4 章起过滤 opening-only profile，避免开篇信息长期注入所有章节，并补第 1、3、4 章生命周期测试。
7. 完成 FIX-011：继续锁定 Canonical hard facts，同时放宽细纲对正文文学表现的限制，补冲突、过渡、高潮和降速场景的变量测试。
8. 完成 VERIFY-012：复查 `character_anchors` 的最低 token 预算；如果仍可能为 0，设置安全的非零最低预算并补极小预算测试。
9. 补齐 VERIFY-013 的明确回归测试。
10. 复查并补齐 Governance barrier 的阻断、Candidate pause 失败 fail-closed、以及 auxiliary 不重复 blocking commit 的测试。
11. 运行工单要求的完整定向测试、全量相关测试、前端 unit test、frontend typecheck 和 `npm run build`。
12. 完成最终 diff 范围审查，并确认没有调试代码、临时日志、测试跳过或无关文件。
13. 本次暂停前没有向 `origin/main` 提交或推送这些未完成修改；后续不得把 `work` 分支内容直接视为 `main` 已验收版本。

## 4. 已执行验证与结果

已执行并通过：

```text
tests/integration/interfaces/api/v1/test_outline_contract_routes.py
tests/integration/infrastructure/persistence/database/test_outline_plan_repository.py
结果：65 passed
```

```text
tests/integration/interfaces/api/v1/test_candidate_generation_routes.py
结果：15 passed
```

```text
python -m compileall -q ...
结果：通过
```

```text
git diff --check
结果：没有空白错误；Git 仅报告工作副本 LF/CRLF 转换提示
```

前端定向结果：

```text
OutlineStudio.spec.ts：通过
GenerationModeLauncher.spec.ts：通过
CandidateReviewDesk.spec.ts：2 个失败
```

`CandidateReviewDesk.spec.ts` 的失败原因是测试 Mock 的 `approveAndCommit()` 返回 `undefined`，当前实现直接读取结果字段；由此还连带造成逐章模式 `generateNext()` 未执行及刷新断言失败。该问题尚未修复。

完整最终门禁尚未执行，因此当前不能报告 FIX-001 至 FIX-011、VERIFY-012 至 VERIFY-014 全部通过。

## 5. 当前修改文件

本次 `work` 分支将包含测试区当前全部代码修改和新增测试文件，共 25 个文件：

```text
application/blueprint/services/outline_cohort_generation_service.py
application/blueprint/services/outline_contract_service.py
application/engine/services/chapter_aftermath_pipeline.py
application/engine/services/generation_run_coordinator.py
application/governance/service.py
engine/pipeline/base.py
engine/pipeline/prose_composer.py
engine/runtime/novel_lifecycle.py
engine/runtime/writing_delegate.py
frontend/src/api/generation.ts
frontend/src/components/autopilot/GenerationModeLauncher.vue
frontend/src/components/autopilot/GenerationModeLauncher.spec.ts
frontend/src/views/CandidateReviewDesk.vue
infrastructure/persistence/database/chapter_candidate_repository.py
infrastructure/persistence/database/outline_contract_repository.py
interfaces/api/dependencies.py
interfaces/api/v1/blueprint/outline_routes.py
interfaces/api/v1/engine/generation_routes.py
interfaces/main.py
interfaces/runtime.py
tests/unit/application/engine/test_generation_run_coordinator.py
tests/unit/engine/test_phase5_delegates.py
tests/unit/engine/test_story_pipeline_prose_composer.py
tests/unit/engine/test_writing_delegate.py
tests/unit/interfaces/test_runtime.py
WORK_HANDOFF_2026-08-18.md
```

其中前 24 个代码/测试文件来自测试区已有修改，`GenerationModeLauncher.spec.ts` 是当前新增的前端契约测试，本报告是本次交接新增文件。未修改 `W:\novel\work`。

## 6. Git 与安全状态

```text
是否发生 git reset --hard：NO
是否发生 git clean：NO
是否发生 rebase 或历史重写：NO
是否发生 force push：NO
是否删除已有分支或 tag：NO
是否修改 W:\novel\work：NO
是否已将未完成代码推送到 origin/main：NO
```

后续执行者应从 `work` 分支继续，不要把未完成代码直接合并到 `main`。完成剩余修复后，应重新运行工单规定的测试和门禁，再由用户决定是否向 `main` 交付。

## 7. 交接结论

本次交接保存的是：

```text
BASE_SHA = 4cb7be33837efca5a3e8ae721a1f7c1d28cd3709
测试区现有全部未提交代码修改
新增的 WORK_HANDOFF_2026-08-18.md
```

交接状态为：**已完成部分稳定性修复，工单整体未完成，等待后续继续实现和验收。**
