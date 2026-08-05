"""Narrative promise extraction and prompt rendering.

The promise layer is deliberately small and genre-neutral: it turns a novel's
title/premise into a stable T0 contract so chapter generation does not spend the
opening payoff too early or drift away from the advertised hook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List

from application.world.services.narrative_lexicon import get_narrative_lexicon


_INTERNAL_HEADER_RE = re.compile(r"【系统内部[^】]*】[\s\S]*?(?=\n\n【|$)")
_TYPE_RE = re.compile(r"【类型：([^】]+)】")
_CORE_CONFLICT_RE = re.compile(r"核心冲突[:：]\s*([^\n]+)")
_OPENING_HOOK_RE = re.compile(r"开篇钩子[:：]\s*([^\n]+)")


@dataclass(frozen=True)
class NarrativePromise:
    title: str
    genre_signal: str = ""
    world_preset: str = ""
    story_structure: str = ""
    pacing_control: str = ""
    writing_style: str = ""
    special_requirements: str = ""
    core_conflict: str = ""
    opening_hook: str = ""
    promise_keywords: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not any(
            (
                self.title.strip(),
                self.genre_signal.strip(),
                self.world_preset.strip(),
                self.story_structure.strip(),
                self.pacing_control.strip(),
                self.writing_style.strip(),
                self.special_requirements.strip(),
                self.core_conflict.strip(),
                self.opening_hook.strip(),
                self.promise_keywords,
            )
        )


def _clean_premise(premise: str) -> str:
    text = _INTERNAL_HEADER_RE.sub("", premise or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text or "")
    return m.group(1).strip() if m else ""


def _dedupe(items: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def extract_narrative_promise(
    title: str,
    premise: str,
    *,
    locked_genre: str = "",
    locked_world_preset: str = "",
    locked_story_structure: str = "",
    locked_pacing_control: str = "",
    locked_writing_style: str = "",
    locked_special_requirements: str = "",
) -> NarrativePromise:
    """Build a compact promise model from a novel setup."""
    clean = _clean_premise(premise)
    genre = _first_match(_TYPE_RE, clean)
    conflict = _first_match(_CORE_CONFLICT_RE, clean)
    hook = _first_match(_OPENING_HOOK_RE, clean)

    keyword_candidates: List[str] = []
    for kw in get_narrative_lexicon().promise_keywords:
        if kw in (title or "") or kw in clean:
            keyword_candidates.append(kw)

    return NarrativePromise(
        title=(title or "").strip(),
        genre_signal=(locked_genre or genre).strip()[:160],
        world_preset=(locked_world_preset or "").strip()[:180],
        story_structure=(locked_story_structure or "").strip()[:220],
        pacing_control=(locked_pacing_control or "").strip()[:180],
        writing_style=(locked_writing_style or "").strip()[:180],
        special_requirements=(locked_special_requirements or "").strip()[:220],
        core_conflict=conflict[:260],
        opening_hook=hook[:260],
        promise_keywords=tuple(_dedupe(keyword_candidates)[:8]),
    )


def build_narrative_promise_block(novel: object, chapter_number: int) -> str:
    """Render a T0 prompt block for generation.

    The block is intentionally prescriptive about pacing, not plot content.
    """
    generation_prefs = getattr(novel, "generation_prefs", None)

    def locked_value(field: str) -> str:
        return str(
            getattr(generation_prefs, field, getattr(novel, field, "")) or ""
        ).strip()

    promise = extract_narrative_promise(
        getattr(novel, "title", "") or "",
        getattr(novel, "premise", "") or "",
        locked_genre=locked_value("locked_genre"),
        locked_world_preset=locked_value("locked_world_preset"),
        locked_story_structure=locked_value("locked_story_structure"),
        locked_pacing_control=locked_value("locked_pacing_control"),
        locked_writing_style=locked_value("locked_writing_style"),
        locked_special_requirements=locked_value("locked_special_requirements"),
    )
    if promise.is_empty():
        return ""

    lines = ["━━━ 叙事承诺锁（不可提前消费）━━━"]
    if promise.title:
        lines.append(f"书名承诺：{promise.title}")
    if promise.genre_signal:
        lines.append(f"类型信号：{promise.genre_signal}")
    if promise.world_preset:
        lines.append(f"世界基调：{promise.world_preset}")
    if promise.story_structure:
        lines.append(f"结构承诺：{promise.story_structure}")
    if promise.pacing_control:
        lines.append(f"节奏约束：{promise.pacing_control}")
    if promise.writing_style:
        lines.append(f"文风约束：{promise.writing_style}")
    if promise.special_requirements:
        lines.append(f"特殊要求：{promise.special_requirements}")
    if promise.core_conflict:
        lines.append(f"核心冲突：{promise.core_conflict}")
    if promise.opening_hook and chapter_number <= 20:
        lines.append(f"开篇钩子：{promise.opening_hook}")
    if promise.promise_keywords:
        lines.append("关键词锚点：" + "、".join(promise.promise_keywords))

    if chapter_number <= 12:
        lines.append(
            "开篇节奏：只加压、揭一角、留代价；不要在前12章彻底平反、彻底解谜或让核心敌人无代价退场。"
        )
    else:
        lines.append("连载节奏：每章推进一个核心变化，其余悬念只推进半步，避免一次性打光主线筹码。")

    if "不是" in promise.title or "无根" in " ".join(promise.promise_keywords):
        lines.append("标题/设定反差必须持续可感：若出现剑、传承或身份翻案，也要服务于更大的反命题。")

    return "\n".join(lines)
