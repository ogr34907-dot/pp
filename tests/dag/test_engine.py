"""DAG 执行引擎测试"""
import pytest
from application.engine.dag.engine import DAGEngine, DAGExecutionError
from application.engine.dag.models import (
    DAGDefinition,
    EdgeCondition,
    EdgeDefinition,
    NodeCategory,
    NodeConfig,
    NodeDefinition,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeStatus,
    PortDataType,
    get_default_dag,
)
from application.engine.dag.registry import BaseNode, NodeRegistry
from application.engine.dag.validator import DAGValidator


class _RouteSourceNode(BaseNode):
    meta = NodeMeta(
        node_type="test_route_source",
        display_name="route source",
        category=NodeCategory.CONTEXT,
        output_ports=[NodePort(name="route_value", data_type=PortDataType.BOOLEAN)],
    )

    async def execute(self, inputs, context):
        return NodeResult(outputs={"route_value": True})

    def validate_inputs(self, inputs):
        return True


class _MatchedBranchNode(BaseNode):
    meta = NodeMeta(
        node_type="test_matched_branch",
        display_name="matched branch",
        category=NodeCategory.EXECUTION,
        output_ports=[NodePort(name="matched", data_type=PortDataType.TEXT)],
    )

    async def execute(self, inputs, context):
        context["shared_state"]["executed_branches"].append("matched")
        return NodeResult(outputs={"matched": "yes"})

    def validate_inputs(self, inputs):
        return True


class _UnmatchedBranchNode(BaseNode):
    meta = NodeMeta(
        node_type="test_unmatched_branch",
        display_name="unmatched branch",
        category=NodeCategory.EXECUTION,
        output_ports=[NodePort(name="unmatched", data_type=PortDataType.TEXT)],
    )

    async def execute(self, inputs, context):
        context["shared_state"]["executed_branches"].append("unmatched")
        return NodeResult(outputs={"unmatched": "no"})

    def validate_inputs(self, inputs):
        return True


class _PortSourceNode(BaseNode):
    meta = NodeMeta(
        node_type="test_port_source",
        display_name="port source",
        category=NodeCategory.CONTEXT,
        output_ports=[NodePort(name="source_value", data_type=PortDataType.TEXT)],
    )

    async def execute(self, inputs, context):
        return NodeResult(outputs={"source_value": "forwarded"})

    def validate_inputs(self, inputs):
        return True


class _PortTargetNode(BaseNode):
    meta = NodeMeta(
        node_type="test_port_target",
        display_name="port target",
        category=NodeCategory.EXECUTION,
        input_ports=[NodePort(name="target_value", data_type=PortDataType.TEXT)],
        output_ports=[NodePort(name="port_observed", data_type=PortDataType.TEXT)],
    )

    async def execute(self, inputs, context):
        return NodeResult(outputs={"port_observed": inputs["target_value"]})

    def validate_inputs(self, inputs):
        return "target_value" in inputs


class _RetryWriterNode(BaseNode):
    meta = NodeMeta(
        node_type="test_retry_writer",
        display_name="retry writer",
        category=NodeCategory.EXECUTION,
        output_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT),
            NodePort(name="drift_alert", data_type=PortDataType.BOOLEAN),
        ],
    )

    async def execute(self, inputs, context):
        state = context["shared_state"]
        state.setdefault("writer_attempts", []).append(state.get("retry_count", 0))
        return NodeResult(outputs={"content": "candidate", "drift_alert": True})

    def validate_inputs(self, inputs):
        return True


class _ExplicitErrorNode(BaseNode):
    meta = NodeMeta(
        node_type="test_explicit_error",
        display_name="explicit error",
        category=NodeCategory.EXECUTION,
    )

    async def execute(self, inputs, context):
        return NodeResult(
            outputs={"partial_output": "must not be treated as success"},
            status=NodeStatus.ERROR,
            error="explicit node failure",
        )

    def validate_inputs(self, inputs):
        return True


NodeRegistry.register("test_route_source")(_RouteSourceNode)
NodeRegistry.register("test_matched_branch")(_MatchedBranchNode)
NodeRegistry.register("test_unmatched_branch")(_UnmatchedBranchNode)
NodeRegistry.register("test_port_source")(_PortSourceNode)
NodeRegistry.register("test_port_target")(_PortTargetNode)
NodeRegistry.register("test_retry_writer")(_RetryWriterNode)
NodeRegistry.register("test_explicit_error")(_ExplicitErrorNode)


class TestDAGEngine:
    """DAG 执行引擎测试"""

    def test_engine_creation(self):
        engine = DAGEngine()
        assert engine is not None

    def test_has_cycle_detection(self):
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_cycle",
            name="环路测试",
            nodes=[
                NodeDefinition(id="node_a", type="ctx_blueprint"),
                NodeDefinition(id="node_b", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="node_a", target="node_b"),
                EdgeDefinition(id="edge_02", source="node_b", target="node_a"),
            ],
        )
        assert engine._has_cycle(dag) is True

    def test_no_cycle_in_linear_dag(self):
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_linear",
            name="线性 DAG",
            nodes=[
                NodeDefinition(id="node_a", type="ctx_blueprint"),
                NodeDefinition(id="node_b", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="node_a", target="node_b"),
            ],
        )
        assert engine._has_cycle(dag) is False

    def test_topological_layers(self):
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_layers",
            name="层级测试",
            nodes=[
                NodeDefinition(id="node_a", type="ctx_blueprint"),
                NodeDefinition(id="node_b", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="node_a", target="node_b"),
            ],
        )
        layers = engine._topological_layers(dag)
        assert len(layers) == 2
        assert layers[0][0].id == "node_a"
        assert layers[1][0].id == "node_b"

    def test_find_downstream_nodes(self):
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_downstream",
            name="下游测试",
            nodes=[
                NodeDefinition(id="node_a", type="ctx_blueprint"),
                NodeDefinition(id="node_b", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="node_a", target="node_b"),
            ],
        )
        downstream = engine._find_downstream_nodes(dag, "node_a")
        assert "node_b" in downstream

    def test_validate_no_entry_node(self):
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_no_entry",
            name="无入口",
            nodes=[
                NodeDefinition(id="node_a", type="ctx_blueprint"),
                NodeDefinition(id="node_b", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="node_a", target="node_b"),
                EdgeDefinition(id="edge_02", source="node_b", target="node_a"),
            ],
        )
        errors = engine.validate(dag)
        assert len(errors) > 0

    def test_parallel_topological_layers(self):
        """测试并行层 — 同层无依赖节点应该在同一层"""
        engine = DAGEngine()
        dag = DAGDefinition(
            id="dag_parallel",
            name="并行测试",
            nodes=[
                NodeDefinition(id="ctx_blueprint", type="ctx_blueprint"),
                NodeDefinition(id="ctx_memory", type="ctx_memory"),
                NodeDefinition(id="exec_writer", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="ctx_blueprint", target="exec_writer"),
                EdgeDefinition(id="edge_02", source="ctx_memory", target="exec_writer"),
            ],
        )
        layers = engine._topological_layers(dag)
        assert len(layers) == 2  # 第一层：ctx_blueprint + ctx_memory，第二层：exec_writer
        assert len(layers[0]) == 2  # 两个并行节点
        assert len(layers[1]) == 1

    @pytest.mark.asyncio
    async def test_run_with_topological_sort(self):
        """测试自研拓扑排序执行器"""
        engine = DAGEngine()
        # 强制使用自研执行器
        engine._use_langgraph = False

        dag = DAGDefinition(
            id="dag_run_test",
            name="运行测试",
            nodes=[
                NodeDefinition(id="ctx_blueprint", type="ctx_blueprint"),
                NodeDefinition(id="exec_writer", type="exec_writer"),
            ],
            edges=[
                EdgeDefinition(id="edge_01", source="ctx_blueprint", target="exec_writer"),
            ],
        )

        result = await engine.run(dag, {"novel_id": "test_novel"})
        assert result.status in ("completed", "error")

    @pytest.mark.asyncio
    async def test_explicit_node_error_is_recorded_as_a_dag_failure(self):
        engine = DAGEngine()
        engine._use_langgraph = False
        dag = DAGDefinition(
            id="dag_explicit_error",
            name="显式错误",
            nodes=[NodeDefinition(id="error_node", type="test_explicit_error")],
        )

        result = await engine.run(dag, {"novel_id": "test_novel"})

        assert result.status == "error"
        assert result.error_count == 1
        assert result.final_state["_errors"]["error_node"] == "explicit node failure"

    def test_default_dag_passes_its_own_validator(self):
        result = DAGValidator().validate(get_default_dag())

        assert result.is_valid, result.errors

    @pytest.mark.asyncio
    async def test_native_engine_executes_only_the_matching_conditional_branch(self):
        engine = DAGEngine()
        engine._use_langgraph = False
        dag = DAGDefinition(
            id="dag_conditional_branch",
            name="条件分支",
            nodes=[
                NodeDefinition(id="route_source", type="test_route_source"),
                NodeDefinition(id="matched_branch", type="test_matched_branch"),
                NodeDefinition(id="unmatched_branch", type="test_unmatched_branch"),
            ],
            edges=[
                EdgeDefinition(
                    id="edge_route_matched",
                    source="route_source",
                    target="matched_branch",
                    condition=EdgeCondition.ON_REVIEW_APPROVED,
                ),
                EdgeDefinition(
                    id="edge_route_unmatched",
                    source="route_source",
                    target="unmatched_branch",
                    condition=EdgeCondition.ON_REVIEW_REJECTED,
                ),
            ],
        )
        state = {"novel_id": "test_novel", "review_approved": True, "executed_branches": []}

        result = await engine._run_with_topological_sort(dag, state)

        assert result["executed_branches"] == ["matched"]
        assert result["matched"] == "yes"
        assert "unmatched" not in result

    @pytest.mark.asyncio
    async def test_native_engine_maps_explicit_edge_ports_to_the_target_input(self):
        engine = DAGEngine()
        engine._use_langgraph = False
        dag = DAGDefinition(
            id="dag_port_mapping",
            name="端口映射",
            nodes=[
                NodeDefinition(id="port_source", type="test_port_source"),
                NodeDefinition(id="port_target", type="test_port_target"),
            ],
            edges=[
                EdgeDefinition(
                    id="edge_port_mapping",
                    source="port_source",
                    source_port="source_value",
                    target="port_target",
                    target_port="target_value",
                ),
            ],
        )

        result = await engine._run_with_topological_sort(dag, {"novel_id": "test_novel"})

        assert result["port_observed"] == "forwarded"

    @pytest.mark.asyncio
    async def test_native_engine_waits_for_all_active_fan_in_edges(self):
        engine = DAGEngine()
        engine._use_langgraph = False
        dag = DAGDefinition(
            id="dag_conditional_fan_in",
            name="条件分支汇合",
            nodes=[
                NodeDefinition(id="route_source", type="test_route_source"),
                NodeDefinition(id="matched_branch", type="test_matched_branch"),
                NodeDefinition(id="unmatched_branch", type="test_unmatched_branch"),
                NodeDefinition(id="port_target", type="test_port_target"),
            ],
            edges=[
                EdgeDefinition(
                    id="edge_route_matched",
                    source="route_source",
                    target="matched_branch",
                    condition=EdgeCondition.ON_REVIEW_APPROVED,
                ),
                EdgeDefinition(
                    id="edge_route_unmatched",
                    source="route_source",
                    target="unmatched_branch",
                    condition=EdgeCondition.ON_REVIEW_REJECTED,
                ),
                EdgeDefinition(
                    id="edge_matched_target",
                    source="matched_branch",
                    source_port="matched",
                    target="port_target",
                    target_port="target_value",
                ),
                EdgeDefinition(
                    id="edge_unmatched_target",
                    source="unmatched_branch",
                    source_port="unmatched",
                    target="port_target",
                    target_port="target_value",
                ),
            ],
        )

        result = await engine._run_with_topological_sort(
            dag,
            {"novel_id": "test_novel", "review_approved": True, "executed_branches": []},
        )

        assert result["executed_branches"] == ["matched"]
        assert result["port_observed"] == "yes"

    @pytest.mark.asyncio
    async def test_native_engine_does_not_execute_a_retry_back_edge_without_an_explicit_state_machine(self):
        engine = DAGEngine()
        engine._use_langgraph = False
        dag = DAGDefinition(
            id="dag_bounded_retry",
            name="有界重写",
            nodes=[
                NodeDefinition(id="retry_writer", type="test_retry_writer"),
                NodeDefinition(
                    id="retry_gateway",
                    type="gw_retry",
                    config=NodeConfig(max_retries=2),
                ),
            ],
            edges=[
                EdgeDefinition(
                    id="edge_writer_retry",
                    source="retry_writer",
                    target="retry_gateway",
                    condition=EdgeCondition.ON_DRIFT_ALERT,
                ),
            ],
        )

        result = await engine._run_with_topological_sort(
            dag,
            {"novel_id": "test_novel", "input": {}, "writer_attempts": []},
        )

        assert result["writer_attempts"] == [0]
        assert "retry_count" not in result

    def test_default_dag_models_retry_as_a_terminal_decision_not_a_back_edge(self):
        dag = get_default_dag()

        assert not any(
            edge.source == "gw_retry" and edge.target == "exec_writer"
            for edge in dag.edges
        )
        assert any(
            edge.source == "gw_circuit"
            and edge.target == "gw_retry"
            and edge.condition == EdgeCondition.ON_BREAKER_OPEN
            for edge in dag.edges
        )
        assert any(
            edge.source == "gw_retry"
            and edge.target == "gw_review"
            and edge.source_port == "retry_exhausted"
            and edge.target_port == "review_required"
            and edge.condition == EdgeCondition.ON_RETRY_EXHAUSTED
            for edge in dag.edges
        )

    @pytest.mark.asyncio
    async def test_default_dag_executes_one_retry_decision_without_blocking_the_first_writer_run(self, monkeypatch):
        """The candidate revision loop lives outside the acyclic DAG run."""

        engine = DAGEngine()
        executed: list[str] = []

        async def execute(node, _state, _inputs=None):
            executed.append(node.id)
            outputs = {
                "exec_beat": {"beats": []},
                "exec_writer": {"content": "candidate prose"},
                "val_style": {"drift_alert": True},
                "val_tension": {"composite": 50.0},
                "val_anti_ai": {"severity_score": 0.0},
                "gw_circuit": {"breaker_status": "open"},
                "gw_retry": {"retry_requested": True, "attempts_used": 1},
            }
            return outputs.get(node.id, {})

        monkeypatch.setattr(engine, "_execute_node", execute)
        result = await engine._run_with_topological_sort(
            get_default_dag(), {"novel_id": "test_novel", "candidate_revision": 0}
        )

        assert executed.count("exec_writer") == 1
        assert "gw_retry" in executed
        assert "gw_review" not in executed
        assert "val_narrative" not in executed
        assert result["retry_requested"] is True

    @pytest.mark.asyncio
    async def test_default_dag_routes_exhausted_retry_to_review(self, monkeypatch):
        engine = DAGEngine()
        executed: list[str] = []

        async def execute(node, _state, _inputs=None):
            executed.append(node.id)
            outputs = {
                "exec_beat": {"beats": []},
                "exec_writer": {"content": "candidate prose"},
                "val_style": {"drift_alert": True},
                "val_tension": {"composite": 50.0},
                "val_anti_ai": {"severity_score": 0.0},
                "gw_circuit": {"breaker_status": "open", "review_required": True},
                "gw_retry": {"retry_requested": False, "retry_exhausted": True},
                "gw_review": {"approved": False, "review_required": True},
            }
            return outputs.get(node.id, {})

        monkeypatch.setattr(engine, "_execute_node", execute)
        result = await engine._run_with_topological_sort(
            get_default_dag(), {"novel_id": "test_novel", "candidate_revision": 2}
        )

        assert "gw_retry" in executed
        assert executed[-1] == "gw_review"
        assert result["retry_exhausted"] is True
        assert result["review_required"] is True

    @pytest.mark.asyncio
    async def test_default_dag_executes_the_clean_candidate_path_to_review(self, monkeypatch):
        engine = DAGEngine()
        executed: list[str] = []

        async def execute(node, _state, _inputs=None):
            executed.append(node.id)
            outputs = {
                "exec_beat": {"beats": []},
                "exec_writer": {"content": "candidate prose"},
                "val_style": {"drift_alert": False},
                "val_tension": {"composite": 50.0},
                "val_anti_ai": {"severity_score": 0.0},
                "gw_circuit": {"breaker_status": "closed"},
                "val_narrative": {"summary": "candidate-only proposal"},
                "val_foreshadow": {"recovered": 0},
                "val_kg_infer": {"inferred_triples": []},
                "gw_review": {"approved": True},
            }
            return outputs.get(node.id, {})

        monkeypatch.setattr(engine, "_execute_node", execute)
        result = await engine._run_with_topological_sort(
            get_default_dag(), {"novel_id": "test_novel", "candidate_revision": 0}
        )

        assert "gw_retry" not in executed
        assert executed.index("gw_circuit") < executed.index("val_narrative")
        assert executed[-1] == "gw_review"
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_engine_emits_authoritative_node_events_for_one_run(self):
        events = []

        class _Observer:
            def on_node_start(self, _novel_id, node_id, _node_type):
                events.append(("started", node_id))

            def on_node_complete(self, _novel_id, node_id, result):
                events.append(("completed", node_id, result.outputs))

            def on_node_error(self, _novel_id, node_id, error):
                events.append(("failed", node_id, str(error)))

            def on_node_bypassed(self, _novel_id, node_id):
                events.append(("skipped", node_id))

        engine = DAGEngine(observer=_Observer())
        dag = DAGDefinition(
            id="dag_observed",
            name="运行轨迹",
            nodes=[
                NodeDefinition(id="port_source", type="test_port_source"),
                NodeDefinition(id="port_target", type="test_port_target"),
            ],
            edges=[
                EdgeDefinition(
                    id="edge_observed",
                    source="port_source",
                    source_port="source_value",
                    target="port_target",
                    target_port="target_value",
                ),
            ],
        )

        result = await engine.run(dag, {"novel_id": "test_novel"})

        assert result.status == "completed"
        assert events == [
            ("started", "port_source"),
            ("completed", "port_source", {"source_value": "forwarded"}),
            ("started", "port_target"),
            ("completed", "port_target", {"port_observed": "forwarded"}),
        ]

    @pytest.mark.asyncio
    async def test_run_scopes_an_observer_to_one_invocation(self):
        default_events = []
        run_events = []

        class _Observer:
            def __init__(self, sink):
                self.sink = sink

            def on_node_start(self, _novel_id, node_id, _node_type):
                self.sink.append(("started", node_id))

            def on_node_complete(self, _novel_id, node_id, _result):
                self.sink.append(("completed", node_id))

        engine = DAGEngine(observer=_Observer(default_events))
        dag = DAGDefinition(
            id="dag_run_observer_scope",
            name="运行 observer 隔离",
            nodes=[NodeDefinition(id="port_source", type="test_port_source")],
        )

        result = await engine.run(dag, {"novel_id": "test_novel"}, observer=_Observer(run_events))

        assert result.status == "completed"
        assert run_events == [("started", "port_source"), ("completed", "port_source")]
        assert default_events == []

    @pytest.mark.asyncio
    async def test_run_does_not_expose_private_runtime_context_in_final_state(self):
        engine = DAGEngine()
        runtime_service = object()
        dag = DAGDefinition(
            id="dag_runtime_context_private",
            name="运行时上下文不持久化",
            nodes=[NodeDefinition(id="port_source", type="test_port_source")],
        )

        result = await engine.run(
            dag,
            {"novel_id": "test_novel"},
            runtime_context={"runtime_service": runtime_service},
        )

        assert result.status == "completed"
        assert "_runtime_context" not in result.final_state
