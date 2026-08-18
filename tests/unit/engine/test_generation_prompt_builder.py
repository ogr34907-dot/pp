"""StoryPipeline generation prompt helper tests."""
from pathlib import Path
from types import SimpleNamespace

from engine.pipeline.context import PipelineContext
from engine.pipeline.generation_prompt_builder import (
    build_director_contract,
    build_generation_prompt,
    make_prompt,
)


def test_chapter_prose_prompt_balances_fact_locks_with_literary_freedom():
    root = Path(__file__).resolve().parents[3]
    system = (root / "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation/system.md").read_text(encoding="utf-8")
    user = (root / "infrastructure/ai/prompt_packages/nodes/chapter-prose-generation/user.md").read_text(encoding="utf-8")

    assert "连续性与长期记忆上下文" in user
    assert "Canonical Hard Facts" in system
    assert "微动作" in system
    assert "潜台词" in system
    assert "无名环境人物" in system
    assert "章尾必须完成本章约定 Delta" in system
    assert "近3章承接摘要" not in user
    assert "主角发生重大变化" not in user


def test_build_generation_prompt_puts_beat_task_before_context():
    beat = SimpleNamespace(
        description="主角夺回证据",
        focus="action",
        visible_action="推门闯入档案室",
        delta="拿到芯片，守卫开始追击",
        card_prompt_block="━━━ 节点卡\n✅ 必须写出的行为：推开门",
    )
    ctx = PipelineContext(
        context_text="核心上下文",
        voice_anchors="声线锚点",
        outline="本章大纲",
        beats=[beat],
    )

    prompt = build_generation_prompt(ctx, beat, 0)

    beat_pos = prompt.index("【当前节拍 1/1】")
    ctx_pos = prompt.index("【参考背景")
    assert beat_pos < ctx_pos
    assert "本拍唯一任务" in prompt
    assert "拿到芯片" in prompt
    assert "核心上下文" in prompt


def test_build_director_contract_empty_when_no_delivery_fields():
    beat = SimpleNamespace(description="铺垫", focus="sensory")
    assert build_director_contract(beat) == ""


def test_build_generation_prompt_includes_bundle_genre_profile():
    beat = SimpleNamespace(description="进入现实困境", focus="action")
    ctx = PipelineContext(
        outline="本章大纲",
        beats=[beat],
        bundle={
            "genre_opening_profile": {"genre_major": "都市"},
            "genre_reader_contract": {"reader_promise": ["现实压迫快速建立"]},
            "genre_rhythm_constraints": {"payoff_interval": "短"},
        },
    )

    prompt = build_generation_prompt(ctx, beat, 0)

    assert "类型开篇画像" in prompt
    assert "现实压迫快速建立" in prompt
    assert "payoff_interval" in prompt


def test_make_prompt_returns_domain_prompt_when_available():
    prompt = make_prompt("正文要求")

    assert getattr(prompt, "user", None) == "正文要求"
    assert getattr(prompt, "system", "")
