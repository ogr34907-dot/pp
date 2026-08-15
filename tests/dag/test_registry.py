"""节点注册表测试"""
import pytest
from application.engine.dag.models import (
    NodeCategory,
    NodeConfig,
    NodeMeta,
    NodePort,
    PortDataType,
    get_default_dag,
)
from application.engine.dag.registry import BaseNode, NodeRegistry


@pytest.fixture(scope="module", autouse=True)
def _load_builtin_dag_nodes():
    """Preload the production registry so test isolation covers cached imports."""
    NodeRegistry.ensure_builtins_loaded()


class TestNodeRegistry:
    """节点注册表测试"""

    def setup_method(self):
        """每个测试前清理注册表"""
        self._registry_snapshot = NodeRegistry._registry.copy()
        self._meta_registry_snapshot = NodeRegistry._meta_registry.copy()
        NodeRegistry._registry.clear()
        NodeRegistry._meta_registry.clear()

    def teardown_method(self):
        """恢复被测试隔离的生产节点定义。"""
        NodeRegistry._registry.clear()
        NodeRegistry._registry.update(self._registry_snapshot)
        NodeRegistry._meta_registry.clear()
        NodeRegistry._meta_registry.update(self._meta_registry_snapshot)

    def test_register_node(self):
        @NodeRegistry.register("test_node_a")
        class TestNodeA(BaseNode):
            meta = NodeMeta(
                node_type="test_node_a",
                display_name="测试节点A",
                category=NodeCategory.CONTEXT,
            )
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={"result": "ok"})
            def validate_inputs(self, inputs):
                return True

        assert NodeRegistry.has("test_node_a")
        assert "test_node_a" in NodeRegistry.all_types()

    def test_get_meta(self):
        @NodeRegistry.register("test_node_b")
        class TestNodeB(BaseNode):
            meta = NodeMeta(
                node_type="test_node_b",
                display_name="测试节点B",
                category=NodeCategory.EXECUTION,
            )
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})
            def validate_inputs(self, inputs):
                return True

        meta = NodeRegistry.get_meta("test_node_b")
        assert meta.display_name == "测试节点B"
        assert meta.category == NodeCategory.EXECUTION

    def test_get_unregistered_raises(self):
        with pytest.raises(KeyError, match="未注册"):
            NodeRegistry.get("nonexistent")

    def test_create_instance(self):
        @NodeRegistry.register("test_node_c")
        class TestNodeC(BaseNode):
            meta = NodeMeta(
                node_type="test_node_c",
                display_name="测试节点C",
                category=NodeCategory.VALIDATION,
            )
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})
            def validate_inputs(self, inputs):
                return True

        instance = NodeRegistry.create_instance("test_node_c")
        assert isinstance(instance, TestNodeC)

    def test_create_instance_with_config(self):
        @NodeRegistry.register("test_node_d")
        class TestNodeD(BaseNode):
            meta = NodeMeta(
                node_type="test_node_d",
                display_name="测试节点D",
                category=NodeCategory.GATEWAY,
            )
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})
            def validate_inputs(self, inputs):
                return True

        config = NodeConfig(temperature=0.3, max_retries=2)
        instance = NodeRegistry.create_instance("test_node_d", config=config)
        assert instance._config.temperature == 0.3

    @pytest.mark.parametrize(
        "expected_timeout",
        [
            5,
            120,
            300,
        ],
    )
    def test_omitted_timeout_uses_node_metadata(self, expected_timeout):
        class TimeoutDefaultNode(BaseNode):
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})

            def validate_inputs(self, inputs):
                return True

        TimeoutDefaultNode.meta = NodeMeta(
            node_type="timeout_default_node",
            display_name="timeout default node",
            category=NodeCategory.GATEWAY,
            default_timeout_seconds=expected_timeout,
        )

        assert TimeoutDefaultNode().get_timeout() == expected_timeout

    def test_explicit_timeout_override_wins_over_node_metadata(self):
        class TimeoutDefaultNode(BaseNode):
            meta = NodeMeta(
                node_type="timeout_default_node",
                display_name="timeout default node",
                category=NodeCategory.GATEWAY,
                default_timeout_seconds=300,
            )

            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})

            def validate_inputs(self, inputs):
                return True

        node = TimeoutDefaultNode(NodeConfig(timeout_seconds=120))

        assert node.get_timeout() == 120

    def test_omitted_retry_override_uses_node_metadata(self):
        class RetryDefaultNode(BaseNode):
            meta = NodeMeta(
                node_type="retry_default_node",
                display_name="retry default node",
                category=NodeCategory.GATEWAY,
                default_max_retries=4,
            )

            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})

            def validate_inputs(self, inputs):
                return True

        assert RetryDefaultNode().get_max_retries() == 4

    @pytest.mark.asyncio
    async def test_retry_node_uses_metadata_when_retry_override_is_omitted(self):
        from application.engine.dag.nodes.gateway_nodes import RetryNode

        result = await RetryNode(NodeConfig(max_retries=None)).execute(
            {"max_attempts": 4},
            {"shared_state": {"candidate_revision": 1}},
        )

        assert result.outputs["retry_requested"] is False
        assert result.outputs["retry_exhausted"] is True

    def test_all_meta(self):
        @NodeRegistry.register("test_node_e")
        class TestNodeE(BaseNode):
            meta = NodeMeta(
                node_type="test_node_e",
                display_name="测试节点E",
                category=NodeCategory.CONTEXT,
            )
            async def execute(self, inputs, context):
                from application.engine.dag.models import NodeResult
                return NodeResult(outputs={})
            def validate_inputs(self, inputs):
                return True

        all_meta = NodeRegistry.all_meta()
        assert "test_node_e" in all_meta


def test_registry_examples_leave_default_dag_constructible():
    """Custom registry examples must not unload production DAG node definitions."""
    dag = get_default_dag()

    assert {node.type for node in dag.nodes} >= {"ctx_blueprint", "exec_writer", "val_style"}
