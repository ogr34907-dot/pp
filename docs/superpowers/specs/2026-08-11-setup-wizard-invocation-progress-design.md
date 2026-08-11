# 新书设置向导 AI 任务进度设计

## 目标

修复“文风 / 世界观”生成期间长期显示 `0 / 5` 和“准备中”的误导性状态。保留现有 AI Invocation、审阅、轮询、提交以及世界观生成语义，不修改后端 API、SSE 事件格式、提示词或模型配置。

## 已确认事实

世界观阶段通过 `POST /api/v1/bible/novels/{id}/generate-stream?stage=worldbuilding` 建立 `bible.setup.worldbuilding` Invocation。该调用会立即发送 `approval_required`，前端随后通过 `aiInvocationStore` 轮询任务。旧的逐维 `worldbuilding_dimension` SSE 事件在此路径上不会产生，因此现有 `completedDimensions` 一直为空，直到整份结果提交。

## 方案

### 状态模型

在 `NovelSetupGuide` 中为 Bible Invocation 使用一个小型、纯前端的展示状态：

- `creating`：请求已发出、尚未收到 Invocation 会话；
- `generating`：收到 `approval_required` 并开始观察会话；
- `validating`：任务返回候选结果，等待提交；
- `committing`：正在写入 Bible / 世界观；
- `completed`：提交成功；
- `failed`：Invocation 或流返回错误。

状态只从既有 `consumeBibleGenerateStream` 回调和 `aiInvocationStore.onSessionUpdate` 载荷导出，不发起额外请求，不改变提交时机。

### 向导展示

世界观生成面板在 Invocation 路径中显示：

- 真实阶段名称与解释，例如“AI 正在生成完整世界观（五个维度会在完成后统一写入）”；
- 自收到任务会话起的已等待时间；
- 无障碍状态播报（`role=status` / `aria-live=polite`）；
- 明确的非伪进度说明，而不是把未产生逐维事件表示成 0/5 的失败或停滞。

在完成提交时才显示完整的 `5 / 5`，并保留现有结果预览和下一步逻辑。若走旧的逐维 SSE 路径，则保留原有逐维实时更新行为。

### 错误与清理

- 出错时停止计时、保留现有错误提示与重试入口；
- 组件卸载、重新开始生成、会话完成时清理计时器与订阅；
- 同一会话不可重复启动计时或重复提交完成状态。

## 不做的事情

- 不拆分五维生成，不缩短提示词，不切换模型；
- 不修改 `FULL_INTERACTIVE` 策略、AI Invocation API 或 SSE 协议；
- 不把“已等待时间”伪装成模型完成百分比。

## 验收

1. 任务收到 `approval_required` 后，世界观向导显示“生成中”而非“准备中 / 0/5”。
2. 已等待时间递增，且会话完成、失败、重新开始、卸载时停止或重置。
3. 已有逐维 SSE 数据仍会立即更新相应维度。
4. 已提交的世界观仍使用原来的完成与下一步流程，完成后显示 5/5。
5. 不改变现有 API/SSE、后端或生成输出。
6. 新增可重复运行的前端行为测试，随后运行前端单测、lint 与 build。
