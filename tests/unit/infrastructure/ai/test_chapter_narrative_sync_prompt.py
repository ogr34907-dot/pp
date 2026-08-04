from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.world.services.chapter_narrative_sync import llm_chapter_extract_bundle
from infrastructure.ai.prompt_seed.loader import NODES_DIR, load_node_dir
from infrastructure.ai.prompt_template_engine import PromptTemplateEngine


def test_chapter_narrative_sync_renders_real_chapter_number_and_one_body_copy():
    record = load_node_dir(NODES_DIR / "chapter-narrative-sync")
    variables = {item["name"]: item for item in record["variables"]}

    assert record["source"] == (
        "infrastructure/ai/prompt_packages/nodes/chapter-narrative-sync/"
    )
    assert variables["chapter_number"] == {
        "name": "chapter_number",
        "desc": "章节号",
        "type": "integer",
        "required": True,
    }

    body = "BODY-MARKER-7f421a"
    rendered = PromptTemplateEngine().render(
        record["system"],
        record["user_template"],
        {
            "chapter_number": 12,
            "content": body,
            "foreshadow_context": "",
        },
    )

    combined = f"{rendered.system}\n{rendered.user}"
    assert combined.count(body) == 1
    assert rendered.user == f"第 12 章正文如下：\n\n{body}\n"
    assert len(rendered.user) == len(body) + 14


@pytest.mark.asyncio
async def test_chapter_narrative_sync_call_binds_declared_chapter_number(monkeypatch):
    captured = {}

    def fake_render(node_key, variables):
        captured["node_key"] = node_key
        captured["variables"] = dict(variables)
        return SimpleNamespace(system="system", user="user")

    monkeypatch.setattr("infrastructure.ai.prompt_utils.render_required_prompt", fake_render)
    llm = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                content=(
                    '{"summary":"摘要","key_events":"事件","open_threads":"线索",'
                    '"relation_triples":[],"foreshadow_hints":[]}'
                )
            )
        )
    )

    await llm_chapter_extract_bundle(llm, "唯一正文", 27)

    assert captured["node_key"] == "chapter-narrative-sync"
    assert captured["variables"] == {
        "chapter_number": 27,
        "content": "唯一正文",
        "foreshadow_context": "",
    }
