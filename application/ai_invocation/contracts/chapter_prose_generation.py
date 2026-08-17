"""Contracts and continuations for interactive chapter prose generation."""
from __future__ import annotations

from typing import Any

from application.ai_invocation.continuation import ContinuationContext, register_continuation_handler
from application.ai_invocation.dtos import InvocationPolicy, InvocationSpec, VariableBinding
from infrastructure.ai.prompt_keys import CHAPTER_PROSE_GENERATION
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


OPERATION = "chapter.generate.prose"
NODE_KEY = CHAPTER_PROSE_GENERATION
INPUT_BINDING_SET_ID = "chapter-prose-generation:input:v1"
OUTPUT_BINDING_SET_ID = "chapter-prose-generation:output:v1"
CONTINUATION_HANDLER_KEY = "chapter_generate_prose_commit"


def chapter_prose_input_bindings() -> list[VariableBinding]:
    return [
        VariableBinding("novel_title", "novel.setup.title", False, "", scope="novel", stage="setup", display_name="小说标题"),
        VariableBinding("genre", "novel.setup.genre", False, "", scope="novel", stage="setup", display_name="题材"),
        VariableBinding("writing_style", "novel.generation.writing_style", False, "", scope="novel", stage="writing", display_name="写作风格"),
        VariableBinding("style_guide", "novel.generation.style_guide", False, "", scope="novel", stage="writing", display_name="风格指南"),
        VariableBinding("voice_anchors", "novel.generation.voice_anchors", False, "", scope="novel", stage="writing", display_name="声线锚点"),
        VariableBinding("genre_opening_profile", "novel.taxonomy.opening_profile", False, {}, scope="novel", stage="planning", value_type="object", display_name="类型开篇画像"),
        VariableBinding("genre_reader_contract", "novel.taxonomy.reader_contract", False, {}, scope="novel", stage="planning", value_type="object", display_name="读者契约"),
        VariableBinding("genre_rhythm_constraints", "novel.taxonomy.rhythm_constraints", False, {}, scope="novel", stage="planning", value_type="object", display_name="节奏约束"),
        VariableBinding("target_words", "chapter.target_words", False, 2500, scope="chapter", stage="writing", value_type="integer", display_name="文章目标字数"),
        VariableBinding("chapter_outline", "chapter.outline", False, "", scope="chapter", stage="writing", display_name="正文细纲"),
        VariableBinding("continuity_context", "chapter.continuity_context", False, "", scope="chapter", stage="writing", display_name="连续性上下文"),
    ]


def chapter_prose_output_bindings() -> list[VariableBinding]:
    return [
        VariableBinding("content", "chapter.prose.generated", True, scope="chapter", stage="writing", display_name="生成正文"),
        VariableBinding("accepted_content", "chapter.prose.accepted", True, scope="chapter", stage="writing", display_name="采纳正文"),
        VariableBinding("generation_notes", "chapter.generation.notes", False, scope="chapter", stage="review", display_name="生成说明"),
        VariableBinding("quality_flags", "chapter.generation.quality_flags", False, scope="chapter", stage="review", value_type="list", display_name="质量标记"),
    ]


def _input_bindings() -> list[VariableBinding]:
    return chapter_prose_input_bindings()


def _output_bindings() -> list[VariableBinding]:
    return chapter_prose_output_bindings()


def ensure_chapter_prose_generation_contract(db) -> InvocationSpec:
    from infrastructure.ai.prompt_manager import get_prompt_manager
    from infrastructure.ai.prompt_registry import get_prompt_registry
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
        SqliteInvocationSpecRepository,
        SqliteVariableHubRepository,
    )

    get_prompt_manager().ensure_seeded()
    node = get_prompt_registry().get_node(NODE_KEY)
    if node is None:
        raise RuntimeError(f"CPMS node is not published: {NODE_KEY}")
    node_version_id = getattr(node, "active_version_id", None) or ""
    if not node_version_id:
        raise RuntimeError(f"CPMS node has no active version: {NODE_KEY}")

    spec = InvocationSpec(
        operation=OPERATION,
        node_key=NODE_KEY,
        prompt_node_version_id=node_version_id,
        input_binding_set_id=INPUT_BINDING_SET_ID,
        output_binding_set_id=OUTPUT_BINDING_SET_ID,
        default_policy=InvocationPolicy.FULL_INTERACTIVE,
        risk_level="medium",
        supports_stream=False,
        continuation_handler_key=CONTINUATION_HANDLER_KEY,
        commit_policy_key="projection:chapter_prose_to_chapters_v1",
        metadata={
            "projection_key": "chapter_prose_to_chapters_v1",
            "projection": {
                "source": {"variable_key": "chapter.prose.accepted"},
                "target": {"adapter": "chapters_table", "fields": {"content": "$.value", "word_count": "$.computed.length", "status": "draft"}},
                "context": {"novel_id": "required", "chapter_number": "required"},
            },
        },
    )
    with sqlite_writes_bypass_queue():
        variable_repo = SqliteVariableHubRepository(db)
        variable_repo.set_bindings(INPUT_BINDING_SET_ID, NODE_KEY, _input_bindings(), direction="input")
        variable_repo.set_bindings(OUTPUT_BINDING_SET_ID, NODE_KEY, _output_bindings(), direction="output")
        SqliteInvocationSpecRepository(db).upsert(
            spec,
            spec_id=f"spec:{NODE_KEY}:v1",
            spec_version=1,
            status="published",
        )
    register_chapter_prose_generation_continuation()
    return spec


def register_chapter_prose_generation_continuation() -> None:
    register_continuation_handler(CONTINUATION_HANDLER_KEY, _chapter_generate_prose_commit)


def _chapter_generate_prose_commit(context: ContinuationContext) -> dict[str, Any]:
    content = (context.decision.accepted_content or "").strip()
    if not content:
        raise ValueError("accepted prose content is empty")

    return {
        "content": content,
        "accepted_content": content,
        "generation_notes": {
            "source": "chapter.generate.prose",
            "session_id": context.session.id,
            "attempt_id": context.decision.attempt_id,
        },
        "_projection": {
            "projection_key": "chapter_prose_to_chapters_v1",
            "adapter": "chapters_table",
            "novel_id": str(context.session.context.get("novel_id") or ""),
            "chapter_number": context.session.context.get("chapter_number"),
            "content": content,
            "word_count": len(content.replace(" ", "")),
            "status": "draft",
            "idempotency_key": f"{context.session.id}:{context.decision.id}:chapter_prose_to_chapters_v1",
        },
    }


def project_chapter_prose_to_chapters(_db, _projection: dict[str, Any]) -> dict[str, Any]:
    """Block the retired direct-to-chapters projection at its final write boundary."""

    return {
        "blocked": True,
        "reason": "legacy_chapter_projection_retired",
        "replacement": "/api/v1/generation/novels/{novel_id}/start",
    }
