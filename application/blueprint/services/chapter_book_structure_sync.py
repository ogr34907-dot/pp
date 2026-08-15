"""结构树与 ``chapters`` 正文表对齐：以 ``story_nodes`` 中的章节节点为准。

规则：结构上不存在的章节号，正文表也不保留（树上的章删了，正文占位一并消失），新章顺延编号。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Iterable, Optional, Set

from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    is_manifest_authority,
)

if TYPE_CHECKING:
    from domain.novel.repositories.chapter_repository import ChapterRepository
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

logger = logging.getLogger(__name__)


def assert_chapter_rows_safe_to_delete(
    chapters: Iterable[object],
    *,
    novel_id: str,
    operation: str,
) -> None:
    """Reject a structural mutation before it removes authored chapter prose."""
    authored = []
    for chapter in chapters:
        content = str(getattr(chapter, "content", "") or "").strip()
        if content:
            authored.append(int(getattr(chapter, "number", 0) or 0))

    if authored:
        numbers = ", ".join(str(number) for number in sorted(set(authored)))
        raise ValueError(
            f"拒绝{operation}：小说 {novel_id} 的第 {numbers} 章已有正文。"
            "请先通过可恢复的重写流程处理正文。"
        )


def collect_structure_chapter_numbers(
    story_node_repo: "StoryNodeRepository",
    novel_id: str,
) -> Set[int]:
    """全书结构树上 chapter 节点的全局章节号集合。"""
    nums: Set[int] = set()
    for n in story_node_repo.get_by_novel_sync(novel_id):
        if not n.is_chapter():
            continue
        try:
            nums.add(int(n.number))
        except (TypeError, ValueError):
            continue
    return nums


def purge_chapter_book_rows_not_matching_structure(
    story_node_repo: "StoryNodeRepository",
    chapter_repository: Optional["ChapterRepository"],
    novel_id: str,
) -> int:
    """删除正文表中「树上的 chapter 列表里不存在同名章号」的所有行。

    Returns:
        删除的行数
    """
    if chapter_repository is None:
        return 0
    get_connection = getattr(story_node_repo, "_get_connection", None)
    connection = get_connection() if callable(get_connection) else None
    if connection is not None and is_manifest_authority(connection, novel_id):
        raise PlanningAuthorityError(
            "manifest planning authority forbids tree-driven chapter purge"
        )
    structure_nums = collect_structure_chapter_numbers(story_node_repo, novel_id)
    novel_vo = NovelId(novel_id)
    orphaned = []
    for ch in list(chapter_repository.list_by_novel(novel_vo)):
        try:
            cn = int(ch.number)
        except (TypeError, ValueError):
            continue
        if cn in structure_nums:
            continue
        orphaned.append(ch)

    assert_chapter_rows_safe_to_delete(
        orphaned,
        novel_id=novel_id,
        operation="结构同步删除章节",
    )

    removed = 0
    for ch in orphaned:
        cn = int(ch.number)
        cid = getattr(ch.id, "value", ch.id)
        chapter_repository.delete(ChapterId(cid))
        removed += 1
    if removed:
        logger.info("[chapter↔structure] novel=%s 已删与树不一致的正文行 %s 条", novel_id, removed)
    return removed
