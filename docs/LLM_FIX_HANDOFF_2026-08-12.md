# LLM 修复交接

交接目标：会话 `019feadb-babf-76c3-b983-b7a596348a12`

测试工作区：`W:\novel\test`

分支：`codex/plotpilot-memory-stability`
基线提交：`d5ff6eaf`

## 状态

LLM 子范围的代码和单元测试已经完成，尚未提交或推送。主线程仍需完成
`interfaces/api/v1/engine/ai_invocation_routes.py` 的显式值语义接入，之后才能把
本轮 LLM 修复视为端到端集成完成。

本交接不包含 DAG、大纲、候选章节流程、前端或数据库迁移改动；这些文件在同一
工作树中也有未提交修改，提交时必须选择性暂存。

## 已完成的 LLM 修复

| 主题 | 已落地内容 |
| --- | --- |
| 配置显式值 | `GenerationConfig` 用内部未设置哨兵区分调用方省略与显式传入默认值；支持任务级 `timeout_seconds`、`reasoning_effort`、`thinking`。显式 `temperature=1.0` 和 `max_tokens=120000` 不再被 Profile 覆盖。 |
| 控制面板 | 保存 Profile 时保留用户的 `max_tokens`；连通性测试固定为 `32` 个输出 token。 |
| 生成预算 | `config/generation_profiles.yaml` 删除未生效的 `provider_role`、`retries`，并按摘要、审阅、卷/部摘要、宏观规划、部纲、章节预规划设置真实 token 预算与超时。 |
| Provider 缓存 | 缓存键使用完整 API Key 的 SHA-256 摘要，并稳定序列化 `extra_headers`、`extra_query`、`extra_body`；字典顺序变化不会重建，真实配置变化会重建。 |
| OpenAI Responses | 解析原生输入/输出/缓存/推理用量；把 Chat 的 `response_format` 映射到 `text.format`；`extra_body` 通过 SDK 的同名参数传递；能力缓存按规范化 base URL 与模型隔离；仅明确 Responses 端点不支持时降级 Chat；`json_schema` 仅在明确格式不支持时降级 `json_object`。 |
| DeepSeek 控制 | 只有调用方显式给出时才发送：Chat 使用 `thinking` 的 `extra_body`，Responses 使用 `reasoning.effort`。普通模型和省略字段不会被自动注入推理参数。 |
| 用量与 Trace | `TokenUsage` 新增缓存命中、缓存未命中、推理 token；非流式 `llm_response` Trace 通过既有 `metadata` 记录完整用量，无数据库迁移。 |
| Anthropic / Gemini | 任务级超时下传；补齐缓存和推理 token 统计；Anthropic 的非流式、HTTPX 流和 SDK 回退流统一复用 Messages 请求构造，并移除 SDK 硬编码总超时。 |
| 重试 | 优先按异常类型和 HTTP 状态码判断可重试性，保留字符串匹配作为兼容回退。 |
| LLMClient | 对动态 Provider 不再把未指定字段伪造成显式 `120000` / `1.0`。 |

## LLM 文件清单

生产代码：

- `application/ai/llm_control_service.py`
- `application/ai/llm_retry_policy.py`
- `config/generation_profiles.yaml`
- `domain/ai/services/llm_service.py`
- `domain/ai/value_objects/token_usage.py`
- `infrastructure/ai/generation_profiles.py`
- `infrastructure/ai/llm_client.py`
- `infrastructure/ai/provider_factory.py`
- `infrastructure/ai/providers/anthropic_provider.py`
- `infrastructure/ai/providers/gemini_provider.py`
- `infrastructure/ai/providers/openai_provider.py`
- `infrastructure/ai/url_utils.py`

测试：

- `tests/unit/application/ai/test_llm_control_service.py`
- `tests/unit/application/ai/test_llm_retry_policy.py`（新增）
- `tests/unit/domain/ai/services/test_llm_service.py`
- `tests/unit/domain/ai/value_objects/test_token_usage.py`
- `tests/unit/infrastructure/ai/providers/test_anthropic_provider.py`
- `tests/unit/infrastructure/ai/providers/test_gemini_provider.py`
- `tests/unit/infrastructure/ai/providers/test_openai_provider.py`
- `tests/unit/infrastructure/ai/test_dynamic_llm_service.py`
- `tests/unit/infrastructure/ai/test_generation_profiles.py`（新增）
- `tests/unit/infrastructure/ai/test_llm_client.py`（新增）
- `tests/unit/infrastructure/ai/test_trace_recorder.py`

## 已验证

2026-08-12 在 `W:\novel\test` 执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/domain/ai tests/unit/infrastructure/ai tests/unit/application/ai -q
```

结果：`229 passed, 5 skipped`。

同时执行了：

```powershell
git diff --check
```

结果：退出码为 `0`，没有空白错误。未对真实供应商发送请求，避免使用用户密钥或
产生费用；供应商覆盖由单元测试的 SDK/HTTP 响应替身完成。

## 主线程必须补的集成

目标文件：`interfaces/api/v1/engine/ai_invocation_routes.py`。该文件由主线程持有，
不要把它作为本交接的已完成 LLM 文件直接暂存。

当前 `_config_from_dict()` 仍然使用：

```python
max_tokens = int(raw.get("max_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)
temperature = float(raw.get("temperature") if raw.get("temperature") is not None else 1.0)
```

这会把请求省略字段伪装成显式 `120000` / `1.0`，使动态 Profile 无法应用实际预算。

请按以下规则改造：

1. 仅当请求中实际提供且值不是 `None` 时，才向 `GenerationConfig` 传入 `model`、`max_tokens`、`temperature`、`response_format`、`timeout_seconds`、`reasoning_effort`。
2. `thinking=False` 是有效显式值，必须保留；使用键存在性判断，不能使用真值判断。
3. 不得再以 `or DEFAULT_MAX_OUTPUT_TOKENS` 或 `else 1.0` 补默认值。默认应由 `GenerationConfig()` 和 `DynamicLLMService._merge_config()` 处理。
4. 如保留 `setup.main_plot_options` / `setup.plot_outline` 的最小 `8192` 兼容规则，只能在调用方显式传入 `max_tokens` 时应用；仅注入 `operation` 不能让空配置变成显式预算。
5. 为该函数增加路由层单测，至少覆盖：空/仅 operation 配置不显式化 `max_tokens` 和 `temperature`；显式 `120000` / `1.0` 保持显式；`temperature=0`、`timeout_seconds`、`reasoning_effort`、`thinking=False` 正确透传。

建议测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/interfaces/api/v1 -q
.\.venv\Scripts\python.exe -m pytest tests/unit/domain/ai tests/unit/infrastructure/ai tests/unit/application/ai -q
.\.venv\Scripts\python.exe -m pytest tests -q
```

最后一条是整仓验证，应在 DAG/大纲主线程改动也完成后运行。

## 提交注意事项

当前工作树同时包含主线程的 DAG、大纲、候选流程、前端和迁移未提交改动。不得使用
`git add .`。主线程完成上面的路由集成并跑完统一验证后，可选择性暂存本文件中列出的
LLM 生产代码和测试文件，再加上它自己的已验收文件。

本交接文件可随最终提交纳入：`docs/LLM_FIX_HANDOFF_2026-08-12.md`。
