import pytest

from application.blueprint.services.chapter_preplanning_service import ChapterPreplanningService
from application.engine.services.hierarchical_narrative_alignment_gate import AlignmentReport, AlignmentViolation
from application.engine.services.narrative_gate_guard import NarrativeAlignmentGateError


class _Gate:
    memory_engine = None

    def build_snapshot(self, chapter_id, nodes, **kwargs):
        return {"chapter_id": chapter_id, "nodes": list(nodes)}

    async def evaluate(self, snapshot, candidate):
        return AlignmentReport(
            decision="block",
            confidence=0.0,
            candidate_digest="candidate-digest",
            snapshot_digest="snapshot-digest",
            violations=(AlignmentViolation("chapter", "blocking", "contract digest"),),
            repair_plan=("补齐章节契约摘要",),
        )


class _Repo:
    def __init__(self):
        self.writes = 0

    def get_tree(self, novel_id):
        return type("Tree", (), {"nodes": []})()

    async def update(self, node):
        self.writes += 1


@pytest.mark.asyncio
async def test_blocked_rendered_preplan_does_not_persist():
    repo = _Repo()
    service = ChapterPreplanningService(
        llm_service=None,
        story_node_repo=repo,
        hierarchy_gate=_Gate(),
    )

    with pytest.raises(NarrativeAlignmentGateError) as caught:
        await service.ensure_execution_plan(
            novel_id="novel-1",
            chapter_number=1,
            current_outline=(
                "一、开篇切入点\n二、场景转换列表\n三、关键对话\n"
                "四、剧情事件链\n五、角色关键决策\n六、爽点/反转设计\n七、主角状态变化"
            ),
        )

    assert caught.value.report.to_dict()["decision"] == "block"
    assert repo.writes == 0
    assert service.last_alignment_report is caught.value.report
