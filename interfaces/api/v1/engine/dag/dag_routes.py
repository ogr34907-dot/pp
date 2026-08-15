"""DAG 管理 REST API — 纯展示层路由

设计原则：
- DAG 路由只投影受保护的默认定义
- 节点注册是代码行为，写一个节点就注册一个
- 执行权在全托管模式，DAG 只展示状态流转

路由分组：
- 健康检查: GET /dag/health/dag
- 节点类型注册表: GET /dag/registry/types, /dag/registry/types/{node_type}
- DAG↔CPMS 联动内核: GET /dag/registry/linkage
- SSE 事件流: GET /dag/events?novel_id=xxx
- DAG 定义（只读）: GET /dag/{novel_id}
- 节点详情（只读）: GET /dag/{novel_id}/nodes/{node_id}
- 运行状态: GET /dag/{novel_id}/status
- 提示词来源: GET /dag/{novel_id}/nodes/{node_id}/prompt-live

注意：静态路由（registry, health, events）必须定义在参数化路由（/{novel_id}）之前，
否则 FastAPI 会将 "registry", "health", "events" 当作 novel_id 参数匹配。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from application.engine.dag.models import (
    DAGDefinition,
    get_default_dag,
)
from application.engine.dag.registry import NodeRegistry
from application.engine.narrative_projection.dag_runtime_projection import (
    node_states_to_sse_events,
    project_node_states,
    snapshot_from_shared,
)
from application.engine.narrative_projection.linkage_kernel import linkage_bundle
from interfaces.api.v1.engine.dag.dag_runtime_settings import get_dag_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dag", tags=["DAG 工作流"])

# ─── 全局单例 ───

# SSE 事件订阅者管理
_sse_subscribers: Dict[str, List[asyncio.Queue]] = {}  # novel_id -> [Queue]

# SSE 断线恢复只需覆盖当前进程生命周期。持久化状态仍由 /status 作为权威来源。
_SSE_PROCESS_EPOCH = uuid.uuid4().hex
_sse_event_history: Dict[str, Deque[Dict[str, Any]]] = {}
_sse_event_sequences: Dict[str, int] = {}
_sse_projection_snapshots: Dict[str, Dict[str, Dict[str, Any]]] = {}

# DAG definitions are cached only after loading them from the version store.
_dag_cache: "OrderedDict[str, DAGDefinition]" = OrderedDict()


def _evict_dag_cache_overflow() -> None:
    max_size = get_dag_runtime_settings().dag_cache_max_size
    while len(_dag_cache) > max_size:
        _dag_cache.popitem(last=False)


def _get_dag_for_novel(novel_id: str) -> DAGDefinition:
    """Return the protected Candidate DAG definition for display."""
    dag = _dag_cache.get(novel_id)
    if dag is not None:
        _dag_cache.move_to_end(novel_id)
        return dag

    dag = get_default_dag()
    _dag_cache[novel_id] = dag
    _evict_dag_cache_overflow()
    return dag


def _event_history_for_novel(novel_id: str) -> Deque[Dict[str, Any]]:
    """Return the bounded in-process replay history for one novel."""
    max_size = get_dag_runtime_settings().sse_queue_size
    history = _sse_event_history.get(novel_id)
    if history is None or history.maxlen != max_size:
        history = deque(history or (), maxlen=max_size)
        _sse_event_history[novel_id] = history
    return history


def _record_sse_event(novel_id: str, event_data: Dict[str, Any]) -> Dict[str, Any]:
    """Give an event a process-scoped ID and retain it before fan-out."""
    sequence = _sse_event_sequences.get(novel_id, 0) + 1
    _sse_event_sequences[novel_id] = sequence

    event = dict(event_data)
    event["novel_id"] = novel_id
    event["event_id"] = f"{_SSE_PROCESS_EPOCH}:{sequence}"
    _event_history_for_novel(novel_id).append(event)
    return event


def _events_after_cursor(novel_id: str, cursor: Optional[str]) -> List[Dict[str, Any]]:
    """Replay only a complete suffix after a cursor retained by this process."""
    if not cursor:
        return []

    history = list(_sse_event_history.get(novel_id, ()))
    for index, event in enumerate(history):
        if event.get("event_id") == cursor:
            return [dict(replayed) for replayed in history[index + 1:]]

    # A foreign process epoch or an evicted cursor cannot be safely resumed.
    return []


def _format_sse_event(event_data: Dict[str, Any]) -> str:
    """Serialize one event using the standard SSE id/event/data frame."""
    event_id = event_data.get("event_id")
    event_type = str(event_data.get("type", "message"))
    id_line = f"id: {event_id}\n" if event_id else ""
    return (
        f"{id_line}event: {event_type}\n"
        f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"
    )


def _publish_projected_node_state_changes(
    novel_id: str,
    new_projection: Dict[str, Dict[str, Any]],
) -> None:
    """Publish each shared-state transition once for all active subscribers."""
    previous_projection = _sse_projection_snapshots.get(novel_id)
    _sse_projection_snapshots[novel_id] = new_projection
    if previous_projection is None:
        return

    for event in node_states_to_sse_events(novel_id, previous_projection, new_projection):
        publish_sse_event(novel_id, event)


def publish_sse_event(novel_id: str, event_data: dict):
    """向指定小说的 SSE 订阅者推送事件"""
    event = _record_sse_event(novel_id, event_data)
    subscribers = _sse_subscribers.get(novel_id, [])
    dead_queues = []
    for queue in subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            dead_queues.append(queue)
    # 清理满队列
    for q in dead_queues:
        subscribers.remove(q)


# ─── Request/Response Models ───


class DAGStatusResponse(BaseModel):
    """DAG 运行状态响应"""
    novel_id: str
    dag_enabled: bool
    current_version: int
    node_states: Dict[str, Dict[str, Any]]


# ═══════════════════════════════════════════════════════════════
# 静态路由 — 必须在 /{novel_id} 参数化路由之前定义
# ═══════════════════════════════════════════════════════════════


# ─── 健康检查 ───


@router.get("/health/dag")
async def dag_health_check():
    """DAG 引擎健康检查"""
    checks = {}

    # 节点注册表
    checks["node_registry"] = {
        "registered_types": len(NodeRegistry.all_types()),
        "types": sorted(NodeRegistry.all_types()),
    }

    # SSE 订阅者统计
    total_subscribers = sum(len(qs) for qs in _sse_subscribers.values())
    checks["sse"] = {
        "active_novels": len(_sse_subscribers),
        "total_subscribers": total_subscribers,
    }

    overall = "ok" if all(
        c.get("status") != "error" for c in checks.values()
    ) else "degraded"

    return {"status": overall, "checks": checks}


# ─── 节点类型注册表 ───


@router.get("/registry/types")
async def list_node_types():
    """获取所有已注册的节点类型"""
    metas = NodeRegistry.all_meta()
    return {
        "types": {
            node_type: meta.model_dump(mode="json")
            for node_type, meta in metas.items()
        }
    }


@router.get("/registry/types/{node_type}")
async def get_node_type_meta(node_type: str):
    """获取单个节点类型的元数据"""
    try:
        meta = NodeRegistry.get_meta(node_type)
        return meta.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"节点类型 '{node_type}' 未注册")


@router.get("/registry/linkage")
async def get_dag_registry_linkage():
    """DAG 默认画布与 CPMS 一一对应表 + 全类型 CPMS 索引（单一联动内核导出）。"""
    return linkage_bundle()


# ─── SSE 事件流 ───


@router.get("/events")
async def dag_event_stream(
    novel_id: str = Query(..., description="小说 ID"),
    after_event_id: Optional[str] = Query(
        default=None,
        description="主动重连时传入最后处理的 SSE event id",
    ),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    """SSE 事件流 — 前端实时接收节点状态变更"""
    runtime_settings = get_dag_runtime_settings()
    queue: asyncio.Queue = asyncio.Queue(maxsize=runtime_settings.sse_queue_size)
    # Direct route tests receive FastAPI Query/Header default objects; live requests receive strings.
    cursor = after_event_id if isinstance(after_event_id, str) and after_event_id else None
    if cursor is None and isinstance(last_event_id, str) and last_event_id:
        cursor = last_event_id
    replay_events = _events_after_cursor(novel_id, cursor)

    # 注册订阅者
    if novel_id not in _sse_subscribers:
        _sse_subscribers[novel_id] = []
    _sse_subscribers[novel_id].append(queue)

    async def event_generator():
        try:
            from interfaces.runtime_state import get_shared_novel_state

            # 发送初始连接确认
            yield f"event: connected\ndata: {json.dumps({'novel_id': novel_id, 'timestamp': time.time()})}\n\n"
            for replayed_event in replay_events:
                yield _format_sse_event(replayed_event)

            idle_ticks = 0

            while True:
                try:
                    event_data = await asyncio.wait_for(
                        queue.get(),
                        timeout=runtime_settings.sse_idle_poll_seconds,
                    )
                    idle_ticks = 0
                    yield _format_sse_event(event_data)
                except asyncio.TimeoutError:
                    idle_ticks += 1
                    event_data = None

                dag = _get_dag_for_novel(novel_id)
                shared = get_shared_novel_state(novel_id)
                snap = snapshot_from_shared(novel_id, shared)
                node_ids = [(n.id, n.type, n.enabled) for n in dag.nodes]
                new_proj = project_node_states(node_ids, snap)
                _publish_projected_node_state_changes(novel_id, new_proj)
                if idle_ticks >= runtime_settings.sse_heartbeat_every_idle_ticks:
                    yield f"event: heartbeat\ndata: {json.dumps({'timestamp': time.time()})}\n\n"
                    idle_ticks = 0
        except asyncio.CancelledError:
            pass
        finally:
            if novel_id in _sse_subscribers:
                try:
                    _sse_subscribers[novel_id].remove(queue)
                    if not _sse_subscribers[novel_id]:
                        del _sse_subscribers[novel_id]
                except ValueError:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
# 参数化路由 — /{novel_id}
# ═══════════════════════════════════════════════════════════════


# ─── DAG 定义（只读） ───


@router.get("/{novel_id}")
async def get_dag(novel_id: str):
    """获取当前 DAG 定义（只读展示）"""
    dag = _get_dag_for_novel(novel_id)
    return dag.model_dump(mode="json")


# ─── 节点详情（只读） ───


@router.get("/{novel_id}/nodes/{node_id}")
async def get_node(novel_id: str, node_id: str):
    """获取节点详情（只读展示）"""
    dag = _get_dag_for_novel(novel_id)

    node = dag.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 '{node_id}' 不存在")

    # 附加节点元数据
    result = node.model_dump(mode="json")
    try:
        meta = NodeRegistry.get_meta(node.type)
        result["meta"] = meta.model_dump(mode="json")
    except KeyError:
        result["meta"] = None

    return result


# ─── 已退役的节点编辑控制 ───


@router.post("/{novel_id}/nodes/{node_id}/toggle")
async def toggle_node(novel_id: str, node_id: str):
    """Candidate DAG barriers cannot be modified from the display surface."""
    raise HTTPException(status_code=410, detail="candidate_generation_control_required")


# ─── 运行状态（只读） ───


@router.get("/{novel_id}/status")
async def get_dag_status(novel_id: str):
    """获取运行状态（含所有节点状态）— 由全托管共享状态投影，与 DAG 定义节点 id 对齐。"""
    from interfaces.runtime_state import get_shared_novel_state

    dag = _get_dag_for_novel(novel_id)
    shared = get_shared_novel_state(novel_id)
    snap = snapshot_from_shared(novel_id, shared)
    node_ids = [(n.id, n.type, n.enabled) for n in dag.nodes]
    states = project_node_states(node_ids, snap)

    return DAGStatusResponse(
        novel_id=novel_id,
        dag_enabled=True,
        current_version=dag.version,
        node_states=states,
    )


# ─── 提示词来源（只读） ───


@router.get("/{novel_id}/nodes/{node_id}/prompt-live")
async def get_node_prompt_live(novel_id: str, node_id: str):
    """获取节点当前的实时提示词"""
    dag = _get_dag_for_novel(novel_id)

    node_def = next((n for n in dag.nodes if n.id == node_id), None)
    if not node_def:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")

    try:
        base_node = NodeRegistry.create_instance(node_def.type, config=node_def.config)
        prompt_dict = base_node.get_effective_prompt()

        cpms_node_key = ""
        if base_node.meta and base_node.meta.cpms_node_key:
            cpms_node_key = base_node.meta.cpms_node_key

        # 收集 CPMS 子注入点信息
        cpms_sub_keys = []
        if base_node.meta and base_node.meta.cpms_sub_keys:
            cpms_sub_keys = [
                {
                    "cpms_node_key": inj.cpms_node_key,
                    "target_variable": inj.target_variable,
                    "description": inj.description,
                    "required": inj.required,
                }
                for inj in base_node.meta.cpms_sub_keys
            ]

        prompt_mode = ""
        if base_node.meta and base_node.meta.prompt_mode:
            prompt_mode = base_node.meta.prompt_mode.value

        return {
            "node_id": node_id,
            "system": prompt_dict["system"],
            "user_template": prompt_dict["user_template"],
            "source": prompt_dict["source"],
            "cpms_node_key": cpms_node_key,
            "cpms_sub_keys": cpms_sub_keys,
            "prompt_mode": prompt_mode,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail=f"节点类型 {node_def.type} 未注册")


# ─── 渲染后 Prompt（只读预览） ───


@router.get("/{novel_id}/nodes/{node_id}/prompt")
async def get_rendered_prompt(novel_id: str, node_id: str):
    """获取渲染后的 Prompt（预览）"""
    dag = _get_dag_for_novel(novel_id)

    node = dag.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 '{node_id}' 不存在")

    template = node.config.prompt_template or ""
    variables = node.config.prompt_variables or {}

    # 渲染模板
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))

    return {
        "node_id": node_id,
        "template": template,
        "variables": variables,
        "rendered": rendered,
    }


@router.put("/{novel_id}/nodes/{node_id}")
async def update_node_config(novel_id: str, node_id: str):
    """Candidate DAG barriers cannot be modified from the display surface."""
    raise HTTPException(status_code=410, detail="candidate_generation_control_required")


# ─── 已退役的 DAG 运行控制 ───


@router.post("/{novel_id}/run")
async def run_dag(novel_id: str):
    """DAG 只投影 Candidate 运行状态，不能独立启动。"""
    raise HTTPException(status_code=410, detail="candidate_generation_control_required")


@router.post("/{novel_id}/stop")
async def stop_dag(novel_id: str):
    """DAG 只投影 Candidate 运行状态，不能独立停止。"""
    raise HTTPException(status_code=410, detail="candidate_generation_control_required")
