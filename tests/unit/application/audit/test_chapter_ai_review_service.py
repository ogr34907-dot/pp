import json

import pytest

from application.audit.services.chapter_ai_review_service import (
    ChapterAIReviewContractError,
    ChapterAIReviewService,
)
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


@pytest.fixture(autouse=True)
def _stub_cpms_review_prompt(monkeypatch):
    """Keep service unit tests independent of the process-wide CPMS database."""

    monkeypatch.setattr(
        "application.audit.services.chapter_ai_review_service.render_required_prompt",
        lambda _node_key, variables: Prompt(
            system="chapter review system",
            user=json.dumps(variables or {}, ensure_ascii=False),
        ),
    )


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append((prompt, config))
        return GenerationResult(
            json.dumps(self.payload, ensure_ascii=False),
            TokenUsage(input_tokens=1, output_tokens=1),
        )


@pytest.mark.asyncio
async def test_chapter_ai_review_service_builds_memo_from_cpms_result():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 91,
            "summary": "推进完整，人物行动可信。",
            "issues": [],
            "suggestions": ["保留当前冲突链，只微调个别长句。"],
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=3,
        chapter_title="雨夜追问",
        chapter_content="雨声压在窗沿。沈岚把证据推到灯下。",
    )

    assert result.status == "approved"
    assert result.score == 91
    assert result.suggestions == ["保留当前冲突链，只微调个别长句。"]
    assert "综合评分：91" in result.memo
    assert llm.calls[0][1].response_format == {"type": "json_object"}


@pytest.mark.asyncio
async def test_chapter_ai_review_service_critical_issue_forces_draft():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 88,
            "summary": "模型误判为可通过。",
            "issues": [
                {
                    "severity": "critical",
                    "location": "结尾",
                    "description": "章节明显截断。",
                    "suggestion": "补完关键行动结果。",
                }
            ],
            "suggestions": ["补完关键行动结果。"],
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=4,
        chapter_title="断点",
        chapter_content="他刚推开门，",
    )

    assert result.status == "draft"
    assert "[critical] 结尾：章节明显截断" in result.memo


@pytest.mark.asyncio
async def test_chapter_ai_review_service_blocks_when_suggestions_missing():
    llm = FakeLLM({"status": "approved", "score": 90, "summary": "可通过"})
    service = ChapterAIReviewService(llm)

    with pytest.raises(ChapterAIReviewContractError, match="缺少可执行建议"):
        await service.review(
            chapter_number=1,
            chapter_title="空建议",
            chapter_content="这是一段正文。",
        )


@pytest.mark.asyncio
async def test_chapter_ai_review_service_marks_missing_event_coverage_unverified():
    llm = FakeLLM(
        {
            "status": "reviewed",
            "score": 76,
            "summary": "需要核验事件落实情况。",
            "issues": [],
            "suggestions": ["补充事件完成证据。"],
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=5,
        chapter_title="未回传事件证据",
        chapter_content="沈岚站在雨里，没有交出证据。",
        required_events=["沈岚交出证据"],
    )

    assert result.event_coverage == [
        {"event": "沈岚交出证据", "status": "unverified", "evidence": ""}
    ]


@pytest.mark.asyncio
async def test_event_evidence_normalizes_format_only_and_preserves_semantic_boundaries():
    event = "本章完成证据核验"
    positive_cases = [
        ("沈岚\r\n在第12章交出证据", "沈岚\n在第12章交出证据"),
        ("沈岚\r在第12章交出证据", "沈岚\n在第12章交出证据"),
        ("沈岚\n在第12章交出证据", "沈岚\r\n在第12章交出证据"),
        ("沈岚  在第12章交出证据", "沈岚 在第12章交出证据"),
        ("沈岚在第１２章交出Ａ证据", "沈岚在第12章交出A证据"),
    ]
    negative_cases = [
        ("沈岚交出,证据", "沈岚交出，证据"),
        ('他说"沈岚交出证据"', "他说“沈岚交出证据”"),
        ("沈岚没有交出证据", "沈岚交出证据"),
        ("证据由沈岚保管，随后离开", "随后离开，证据由沈岚保管"),
    ]

    actual_positive = []
    for content, evidence in positive_cases:
        result = await ChapterAIReviewService(
            FakeLLM(
                {
                    "status": "approved",
                    "score": 90,
                    "summary": "证据已核验。",
                    "suggestions": ["保留正文行动证据。"],
                    "event_coverage": [
                        {"event": event, "status": "completed", "evidence": evidence}
                    ],
                }
            )
        ).review(
            chapter_number=6,
            chapter_title="证据核验",
            chapter_content=content,
            required_events=[event],
        )
        actual_positive.append(result.event_coverage[0])

    actual_negative = []
    for content, evidence in negative_cases:
        result = await ChapterAIReviewService(
            FakeLLM(
                {
                    "status": "approved",
                    "score": 90,
                    "summary": "证据需要继续核验。",
                    "suggestions": ["保留正文行动证据。"],
                    "event_coverage": [
                        {"event": event, "status": "completed", "evidence": evidence}
                    ],
                }
            )
        ).review(
            chapter_number=6,
            chapter_title="证据核验",
            chapter_content=content,
            required_events=[event],
        )
        actual_negative.append(result.event_coverage[0])

    assert actual_positive == [
        {"event": event, "status": "completed", "evidence": evidence}
        for _content, evidence in positive_cases
    ]
    assert actual_negative == [
        {"event": event, "status": "unverified", "evidence": ""}
        for _content, _evidence in negative_cases
    ]


@pytest.mark.asyncio
async def test_transition_rhythm_can_pass_without_a_climax_when_progress_and_handoff_are_evidenced():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 87,
            "summary": "余波完成了关系推进。",
            "issues": [],
            "suggestions": ["保留追兵逼近的交接。"],
            "rhythm_assessment": {
                "status": "complete",
                "progress": {
                    "status": "completed",
                    "evidence": "二人从敌对转为暂时合作",
                },
                "handoff": {
                    "status": "completed",
                    "evidence": "门外的追兵已经踩上石阶",
                },
            },
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=8,
        chapter_title="雨夜余波",
        chapter_content="二人从敌对转为暂时合作。门外的追兵已经踩上石阶。",
        chapter_outline=json.dumps(
            {
                "chapter_rhythm": {
                    "chapter_function": "transition",
                    "chapter_goal": "完成交接并改变关系",
                    "ending_hook": "追兵逼近",
                }
            },
            ensure_ascii=False,
        ),
    )

    assert result.status == "approved"
    assert result.rhythm_assessment["status"] == "complete"


@pytest.mark.asyncio
async def test_transition_accepts_a_state_delta_as_progress_evidence():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 84,
            "summary": "关系变化推动了下一步。",
            "issues": [],
            "suggestions": ["保留新的合作边界。"],
            "rhythm_assessment": {
                "status": "complete",
                "state_delta": {
                    "status": "completed",
                    "evidence": "她收回刀，答应暂时替他引路",
                },
                "handoff": {
                    "status": "completed",
                    "evidence": "城外的钟声催他们立刻出发",
                },
            },
        }
    )
    result = await ChapterAIReviewService(llm).review(
        chapter_number=8,
        chapter_title="关系换挡",
        chapter_content="她收回刀，答应暂时替他引路。城外的钟声催他们立刻出发。",
        chapter_outline=json.dumps(
            {"chapter_rhythm": {"chapter_function": "aftermath"}},
            ensure_ascii=False,
        ),
    )

    assert result.status == "approved"
    assert result.rhythm_assessment["status"] == "complete"


@pytest.mark.asyncio
async def test_escalation_without_choice_cost_and_state_delta_is_blocked():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 94,
            "summary": "动作场面激烈。",
            "issues": [],
            "suggestions": ["补足人物选择与代价。"],
            "rhythm_assessment": {
                "status": "complete",
                "progress": {"status": "completed", "evidence": "他冲进仓库"},
            },
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=9,
        chapter_title="仓库冲突",
        chapter_content="他冲进仓库，和守卫厮打，夺下钥匙。",
        chapter_outline=json.dumps(
            {
                "chapter_rhythm": {
                    "chapter_function": "escalation",
                    "chapter_goal": "夺下钥匙",
                    "decisive_choice": "放弃撤退",
                    "cost_or_risk": "暴露身份",
                    "chapter_delta": "守卫拉响警报",
                }
            },
            ensure_ascii=False,
        ),
    )

    assert result.status == "draft"
    assert any(issue.severity == "critical" for issue in result.issues)


@pytest.mark.asyncio
async def test_climax_without_payoff_and_cost_is_blocked_even_if_the_fight_is_present():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 92,
            "summary": "完成了战斗。",
            "issues": [],
            "suggestions": ["补足兑现和代价。"],
            "rhythm_assessment": {
                "status": "complete",
                "choice": {"status": "completed", "evidence": "他选择迎战"},
            },
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=10,
        chapter_title="城门决战",
        chapter_content="他选择迎战，刀光在城门下撞成一片。",
        chapter_outline=json.dumps(
            {
                "chapter_rhythm": {
                    "chapter_function": "climax",
                    "chapter_goal": "夺回城门",
                    "decisive_choice": "迎战",
                    "cost_or_risk": "失去退路",
                    "chapter_delta": "城门易手",
                    "turn_or_payoff": "钟声伏笔兑现",
                }
            },
            ensure_ascii=False,
        ),
    )

    assert result.status == "draft"
    assert any("payoff" in issue.description or "cost_or_risk" in issue.description for issue in result.issues)


@pytest.mark.asyncio
async def test_reveal_that_only_explains_setting_is_blocked():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 90,
            "summary": "说明了世界规则。",
            "issues": [],
            "suggestions": ["让揭示改变人物理解或目标。"],
            "rhythm_assessment": {
                "status": "complete",
                "exposition": {"status": "completed", "evidence": "城中禁令由旧王制定"},
            },
        }
    )
    service = ChapterAIReviewService(llm)

    result = await service.review(
        chapter_number=11,
        chapter_title="旧王禁令",
        chapter_content="旁白解释：城中禁令由旧王制定。",
        chapter_outline=json.dumps(
            {
                "chapter_rhythm": {
                    "chapter_function": "reveal",
                    "chapter_goal": "揭示禁令来源",
                    "chapter_delta": "主角改变对禁令的理解",
                }
            },
            ensure_ascii=False,
        ),
    )

    assert result.status == "draft"
    assert any(issue.severity == "critical" for issue in result.issues)


@pytest.mark.asyncio
async def test_reveal_can_pass_when_it_changes_understanding_without_a_fight():
    llm = FakeLLM(
        {
            "status": "approved",
            "score": 89,
            "summary": "揭示改变了人物判断。",
            "issues": [],
            "suggestions": ["保留新的追查目标。"],
            "rhythm_assessment": {
                "status": "complete",
                "understanding_change": {
                    "status": "completed",
                    "evidence": "他终于明白禁令是为了掩盖港口名单",
                },
            },
        }
    )
    result = await ChapterAIReviewService(llm).review(
        chapter_number=12,
        chapter_title="名单背后",
        chapter_content="他终于明白禁令是为了掩盖港口名单，决定连夜去查那份名单。",
        chapter_outline=json.dumps(
            {"chapter_rhythm": {"chapter_function": "reveal"}},
            ensure_ascii=False,
        ),
    )

    assert result.status == "approved"
    assert result.rhythm_assessment["status"] == "complete"
