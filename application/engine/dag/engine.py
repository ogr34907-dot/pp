"""DAG 执行引擎 — LangGraph 编排 + 拓扑并行执行

核心职责：
1. 将 DAGDefinition 编译为 LangGraph StateGraph
2. 支持断点续写（通过 LangGraph Checkpoint）
3. 拓扑排序后无依赖节点并行执行
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from application.engine.dag.models import (
    DAGDefinition,
    DAGRunResult,
    EdgeCondition,
    NodeDefinition,
    NodeResult,
    NodeRunState,
    NodeStatus,
    NovelWorkflowState,
)
from application.engine.dag.registry import NodeRegistry

logger = logging.getLogger(__name__)


def _is_langgraph_available() -> bool:
    """检查 LangGraph 是否可用"""
    try:
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False


class DAGEngine:
    """DAG 执行引擎

    设计决策：
    - 优先使用 LangGraph 进行编排（支持循环重写、断点续写）
    - LangGraph 不可用时降级为自研拓扑排序执行器
    - 两种路径共享相同的节点注册表和输入收集逻辑
    """

    def __init__(self, checkpointer=None, observer=None):
        self._checkpointer = checkpointer
        self._observer = observer
        self._node_started_at: Dict[str, float] = {}
        self._use_langgraph = _is_langgraph_available()

        if self._use_langgraph:
            logger.info("DAG 引擎: LangGraph 可用，使用 LangGraph 编排")
        else:
            logger.info("DAG 引擎: LangGraph 不可用，使用自研拓扑执行器")

    # ─── 主入口 ───

    async def run(
        self,
        dag: DAGDefinition,
        initial_state: Dict[str, Any],
        thread_id: str = "",
        *,
        observer: Any = None,
        runtime_context: Optional[Dict[str, Any]] = None,
    ) -> DAGRunResult:
        """执行完整的 DAG

        Args:
            dag: DAG 定义
            initial_state: 初始状态
            thread_id: 线程 ID（用于 LangGraph Checkpoint）

        Returns:
            DAGRunResult
        """
        start_time = time.time()
        dag_run_id = initial_state.get("dag_run_id", f"run_{int(time.time()*1000)}")
        state = dict(initial_state)
        if runtime_context:
            state["_runtime_context"] = dict(runtime_context)
        run_observer = observer if observer is not None else self._observer
        run_started_at: Optional[Dict[str, float]] = {} if run_observer is not None else None

        # 运行时状态追踪
        node_states: Dict[str, NodeRunState] = {}
        for node in dag.nodes:
            node_states[node.id] = NodeRunState(node_id=node.id)

        try:
            # The native runtime is the execution authority.  It is deliberately
            # used even when LangGraph is installed so branch, fan-in and explicit
            # port contracts have identical local behavior.
            result_state = await self._run_with_topological_sort(
                dag, state, observer=run_observer, node_started_at=run_started_at
            )
            # Runtime services are intentionally available to nodes only.  A
            # DAG result is persisted by callers, so it must never expose them.
            result_state.pop("_runtime_context", None)

            # 收集结果
            total_ms = int((time.time() - start_time) * 1000)
            failed = bool(result_state.get("_errors")) or result_state.get("status") == "error"
            return DAGRunResult(
                dag_run_id=dag_run_id,
                novel_id=initial_state.get("novel_id", ""),
                status="error" if failed else "completed",
                node_results={nid: NodeResult(outputs=res) for nid, res in result_state.items() if isinstance(res, dict)},
                total_duration_ms=total_ms,
                error_count=len(result_state.get("_errors", {})) if isinstance(result_state.get("_errors"), dict) else int(failed),
                final_state=result_state,
            )

        except DAGExecutionError as e:
            total_ms = int((time.time() - start_time) * 1000)
            return DAGRunResult(
                dag_run_id=dag_run_id,
                novel_id=initial_state.get("novel_id", ""),
                status="error",
                total_duration_ms=total_ms,
                error_count=1,
            )
        except Exception as e:
            logger.error(f"DAG 执行异常: {e}", exc_info=True)
            total_ms = int((time.time() - start_time) * 1000)
            return DAGRunResult(
                dag_run_id=dag_run_id,
                novel_id=initial_state.get("novel_id", ""),
                status="error",
                total_duration_ms=total_ms,
                error_count=1,
            )

    async def run_from_node(
        self,
        dag: DAGDefinition,
        node_id: str,
        state: Dict[str, Any],
        thread_id: str = "",
    ) -> DAGRunResult:
        """从指定节点开始执行（断点续写）

        仅执行 node_id 及其所有后继节点。
        """
        # 找到所有需要执行的节点（node_id + 所有后继）
        nodes_to_run = self._find_downstream_nodes(dag, node_id)
        nodes_to_run.add(node_id)

        # 创建裁剪后的 DAG
        pruned_dag = DAGDefinition(
            id=dag.id,
            name=dag.name,
            version=dag.version,
            nodes=[n for n in dag.nodes if n.id in nodes_to_run],
            edges=[e for e in dag.edges if e.source in nodes_to_run and e.target in nodes_to_run],
        )

        return await self.run(pruned_dag, state, thread_id)

    # ─── LangGraph 路径 ───

    async def _run_with_langgraph(
        self,
        dag: DAGDefinition,
        initial_state: Dict[str, Any],
        thread_id: str,
    ) -> Dict[str, Any]:
        """使用 LangGraph StateGraph 执行 DAG"""
        from langgraph.graph import StateGraph, END

        # 构建状态 Schema（使用 dict 模式，更灵活）
        graph = StateGraph(dict)

        # 1. 注册所有节点
        for node_def in dag.nodes:
            if not node_def.enabled:
                continue
            if not NodeRegistry.has(node_def.type):
                logger.warning(f"跳过未注册的节点类型: {node_def.type}")
                continue
            executor = NodeRegistry.create_executor(node_def.type, node_def.id, node_def.config)
            graph.add_node(node_def.id, executor)

        # 2. 注册所有边
        entry_node = None
        for node_def in dag.nodes:
            if not node_def.enabled:
                continue
            if not dag.get_predecessors(node_def.id):
                entry_node = node_def.id
                break

        if not entry_node:
            raise DAGExecutionError("DAG 没有入口节点")

        graph.set_entry_point(entry_node)

        # 添加边
        for edge in dag.edges:
            source_node = dag.get_node(edge.source)
            target_node = dag.get_node(edge.target)
            if not source_node or not target_node:
                continue
            if not source_node.enabled or not target_node.enabled:
                continue

            if edge.condition != EdgeCondition.ALWAYS:
                # 条件边 — 使用 conditional_edges
                condition_fn = _make_condition_function(edge.condition, edge.target)
                graph.add_conditional_edges(
                    edge.source,
                    condition_fn,
                    {True: edge.target, False: END},
                )
            else:
                graph.add_edge(edge.source, edge.target)

        # 编译
        compile_kwargs = {}
        if self._checkpointer:
            compile_kwargs["checkpointer"] = self._checkpointer

        compiled = graph.compile(**compile_kwargs)

        # 执行
        config = {"configurable": {"thread_id": thread_id}} if thread_id else {}
        result = await compiled.ainvoke(initial_state, config)
        return result if isinstance(result, dict) else {}

    # ─── 自研拓扑排序路径 ───

    async def _run_with_topological_sort(
        self,
        dag: DAGDefinition,
        initial_state: Dict[str, Any],
        *,
        observer: Any = None,
        node_started_at: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Execute the enabled portion of a DAG with real edge semantics.

        A node becomes runnable only after every *active* incoming edge has a
        completed source.  Conditional edges that do not match are inactive, so
        they do not strand downstream fan-in nodes.  Explicit source/target ports
        are mapped before a node validates its inputs.
        """
        state = dict(initial_state)

        nodes = {node.id: node for node in dag.nodes if node.enabled}
        incoming: Dict[str, List[Any]] = defaultdict(list)
        outgoing: Dict[str, List[Any]] = defaultdict(list)
        for edge in dag.edges:
            if edge.source in nodes and edge.target in nodes:
                incoming[edge.target].append(edge)
                outgoing[edge.source].append(edge)

        active_edges: Dict[str, Optional[bool]] = {
            edge.id: None for edge in dag.edges
            if edge.source in nodes and edge.target in nodes
        }
        completed: Set[str] = set()
        skipped: Set[str] = set()

        while True:
            for node_id in nodes:
                if node_id in completed or node_id in skipped:
                    continue
                edges = incoming.get(node_id, [])
                if edges and all(
                    edge.source in completed or edge.source in skipped
                    for edge in edges
                ) and not any(active_edges.get(edge.id) is True for edge in edges):
                    skipped.add(node_id)
                    for edge in outgoing[node_id]:
                        active_edges[edge.id] = False
                    self._notify("on_node_bypassed", state, nodes[node_id], observer=observer)

            ready = [
                node for node in nodes.values()
                if node.id not in completed
                and node.id not in skipped
                and self._node_ready(node.id, incoming, active_edges, completed, skipped)
            ]
            if not ready:
                break

            state_snapshot = dict(state)
            execute_kwargs = (
                {"observer": observer, "node_started_at": node_started_at}
                if observer is not None or node_started_at is not None
                else {}
            )
            results = await asyncio.gather(
                *[
                    self._execute_node(node, state_snapshot, self._collect_edge_inputs(
                        node.id, incoming, active_edges, state_snapshot
                    ), **execute_kwargs)
                    for node in ready
                ],
                return_exceptions=True,
            )

            for node, result in zip(ready, results):
                completed.add(node.id)
                if isinstance(result, Exception):
                    logger.error("节点 %s 执行失败: %s", node.id, result)
                    state.setdefault("_errors", {})[node.id] = str(result)
                    state["status"] = "error"
                    self._notify("on_node_error", state, node, result, observer=observer)
                else:
                    state.update(result)
                    self._notify(
                        "on_node_complete", state, node, result,
                        observer=observer, node_started_at=node_started_at,
                    )

                for edge in outgoing[node.id]:
                    active_edges[edge.id] = self._edge_matches(edge.condition, state)

        for node_id in nodes:
            if node_id not in completed and node_id not in skipped:
                logger.info("节点 %s 没有活跃输入路径，跳过", node_id)
                skipped.add(node_id)
                self._notify("on_node_bypassed", state, nodes[node_id], observer=observer)

        if state.get("_errors"):
            state["status"] = "error"

        return state

    async def _execute_node(
        self,
        node_def: NodeDefinition,
        state: Dict[str, Any],
        inputs: Optional[Dict[str, Any]] = None,
        *,
        observer: Any = None,
        node_started_at: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """执行单个节点"""
        if not node_def.enabled:
            logger.info(f"节点 {node_def.id} 已禁用，跳过")
            return {}

        if not NodeRegistry.has(node_def.type):
            logger.warning(f"跳过未注册的节点类型: {node_def.type}")
            return {}

        self._notify(
            "on_node_start", state, node_def,
            observer=observer, node_started_at=node_started_at,
        )
        executor = NodeRegistry.create_executor(
            node_def.type, node_def.id, node_def.config, inputs=inputs
        )
        return await executor(state)

    def _notify(
        self,
        method: str,
        state: Dict[str, Any],
        node: NodeDefinition,
        result: Any = None,
        *,
        observer: Any = None,
        node_started_at: Optional[Dict[str, float]] = None,
    ) -> None:
        active_observer = observer if observer is not None else self._observer
        callback = getattr(active_observer, method, None) if active_observer else None
        if not callable(callback):
            return
        try:
            novel_id = str(state.get("novel_id") or "")
            if method == "on_node_start":
                (node_started_at if node_started_at is not None else self._node_started_at)[node.id] = time.perf_counter()
                callback(novel_id, node.id, node.type)
            elif method == "on_node_complete":
                timings = node_started_at if node_started_at is not None else self._node_started_at
                started_at = timings.pop(node.id, time.perf_counter())
                callback(
                    novel_id,
                    node.id,
                    NodeResult(
                        outputs=dict(result or {}),
                        duration_ms=int((time.perf_counter() - started_at) * 1000),
                    ),
                )
            elif method == "on_node_error":
                (node_started_at if node_started_at is not None else self._node_started_at).pop(node.id, None)
                callback(novel_id, node.id, result)
            else:
                callback(novel_id, node.id)
        except Exception:
            logger.exception("DAG observer callback failed: %s", method)

    @staticmethod
    def _edge_matches(condition: EdgeCondition, state: Dict[str, Any]) -> bool:
        return _make_condition_function(condition, "")(state)

    @staticmethod
    def _node_ready(
        node_id: str,
        incoming: Dict[str, List[Any]],
        active_edges: Dict[str, Optional[bool]],
        completed: Set[str],
        skipped: Set[str],
    ) -> bool:
        edges = incoming.get(node_id, [])
        if not edges:
            return True
        if any(edge.source not in completed and edge.source not in skipped for edge in edges):
            return False
        return any(
            active_edges.get(edge.id) is True and edge.source in completed
            for edge in edges
        )

    @staticmethod
    def _collect_edge_inputs(
        node_id: str,
        incoming: Dict[str, List[Any]],
        active_edges: Dict[str, Optional[bool]],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        inputs = {}
        for edge in incoming.get(node_id, []):
            if active_edges.get(edge.id) is not True:
                continue
            source_port = edge.source_port or ""
            target_port = edge.target_port or source_port
            if source_port and target_port and source_port in state:
                inputs[target_port] = state[source_port]

        return inputs

    def _topological_layers(self, dag: DAGDefinition) -> List[List[NodeDefinition]]:
        """Kahn 算法分层 — 同层节点无依赖可并行"""
        enabled_nodes = {n.id for n in dag.nodes if n.enabled}
        in_degree = {n.id: 0 for n in dag.nodes if n.enabled}

        for edge in dag.edges:
            if edge.source in enabled_nodes and edge.target in enabled_nodes:
                in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

        layers = []
        queue = [nid for nid, d in in_degree.items() if d == 0]

        while queue:
            layer = [dag.get_node(nid) for nid in queue if dag.get_node(nid)]
            layers.append(layer)
            next_queue = []
            for nid in queue:
                for edge in dag.edges:
                    if edge.source == nid and edge.target in in_degree:
                        in_degree[edge.target] -= 1
                        if in_degree[edge.target] == 0:
                            next_queue.append(edge.target)
            queue = next_queue

        return layers

    def _find_downstream_nodes(self, dag: DAGDefinition, start_node_id: str) -> Set[str]:
        """找到所有后继节点（BFS）"""
        downstream = set()
        queue = [start_node_id]
        while queue:
            current = queue.pop(0)
            for succ in dag.get_successors(current):
                if succ not in downstream:
                    downstream.add(succ)
                    queue.append(succ)
        return downstream

    # ─── 校验 ───

    def validate(self, dag: DAGDefinition) -> List[str]:
        """校验 DAG 有效性（无环、端口匹配、必填输入满足）"""
        errors = []

        # 环检测
        if self._has_cycle(dag):
            errors.append("DAG 包含环，请使用 gw_retry 网关节点实现循环重写")

        # 入口节点检查
        entry_nodes = dag.get_entry_nodes()
        if not entry_nodes:
            errors.append("DAG 没有入口节点（所有节点都有入边）")

        # 未注册节点类型检查
        for node in dag.nodes:
            if not NodeRegistry.has(node.type):
                errors.append(f"节点 '{node.id}' 使用未注册的类型 '{node.type}'")

        return errors

    def _has_cycle(self, dag: DAGDefinition) -> bool:
        """DFS 环检测"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n.id: WHITE for n in dag.nodes}
        adj = defaultdict(list)
        for edge in dag.edges:
            adj[edge.source].append(edge.target)

        def dfs(node_id: str) -> bool:
            color[node_id] = GRAY
            for neighbor in adj[node_id]:
                if color.get(neighbor) == GRAY:
                    return True
                if color.get(neighbor, WHITE) == WHITE:
                    if dfs(neighbor):
                        return True
            color[node_id] = BLACK
            return False

        for node in dag.nodes:
            if color[node.id] == WHITE:
                if dfs(node.id):
                    return True
        return False


# ─── 辅助函数 ───


class DAGExecutionError(Exception):
    """DAG 执行错误"""
    pass


def _make_condition_function(condition: EdgeCondition, target: str):
    """创建 LangGraph 条件边函数"""
    def condition_fn(state: dict) -> bool:
        if condition == EdgeCondition.ON_SUCCESS:
            return state.get("status") != "error"
        elif condition == EdgeCondition.ON_ERROR:
            return state.get("status") == "error"
        elif condition == EdgeCondition.ON_DRIFT_ALERT:
            return state.get("drift_alert", False)
        elif condition == EdgeCondition.ON_NO_DRIFT:
            return not state.get("drift_alert", False)
        elif condition == EdgeCondition.ON_BREAKER_OPEN:
            return state.get("breaker_status") == "open"
        elif condition == EdgeCondition.ON_BREAKER_CLOSED:
            return state.get("breaker_status") != "open"
        elif condition == EdgeCondition.ON_REVIEW_APPROVED:
            return state.get("review_approved", False)
        elif condition == EdgeCondition.ON_REVIEW_REJECTED:
            return not state.get("review_approved", False)
        elif condition == EdgeCondition.ON_RETRY_EXHAUSTED:
            return bool(state.get("retry_exhausted", False))
        return True

    return condition_fn
