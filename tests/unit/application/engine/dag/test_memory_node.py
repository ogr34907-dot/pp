import pytest

from application.engine.dag.models import NodeStatus
from application.engine.dag.nodes.context_nodes import MemoryNode
from application.engine.dag.registry import NodeRegistry


class _MemoryEngine:
    def __init__(self, fact_lock="", error=None):
        self.fact_lock = fact_lock
        self.error = error

    def build_fact_lock_section(self, novel_id, chapter_number):
        if self.error:
            raise self.error
        return f"{self.fact_lock}:{novel_id}:ch{chapter_number}"


@pytest.mark.asyncio
async def test_memory_node_emits_existing_memory_engine_fact_lock():
    result = await MemoryNode().execute(
        {"novel_id": "novel-1", "chapter_number": 7},
        {"memory_engine": _MemoryEngine("FACT_LOCK")},
    )

    assert result.status == NodeStatus.SUCCESS
    assert result.outputs["fact_lock"] == "FACT_LOCK:novel-1:ch7"


@pytest.mark.asyncio
async def test_memory_node_reports_fact_lock_failure_instead_of_successful_empty_lock():
    with pytest.raises(RuntimeError, match="fact lock unavailable"):
        await MemoryNode().execute(
            {"novel_id": "novel-1", "chapter_number": 7},
            {"memory_engine": _MemoryEngine(error=RuntimeError("fact lock unavailable"))},
        )


@pytest.mark.asyncio
async def test_memory_node_executor_surfaces_fact_lock_failure():
    executor = NodeRegistry.create_executor("ctx_memory", "ctx_memory")

    with pytest.raises(RuntimeError, match="fact lock unavailable"):
        await executor(
            {
                "novel_id": "novel-1",
                "chapter_number": 7,
                "memory_engine": _MemoryEngine(
                    error=RuntimeError("fact lock unavailable")
                ),
            }
        )
