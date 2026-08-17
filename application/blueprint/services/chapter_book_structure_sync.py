"""结构树与 ``chapters`` 正文表对齐：以 ``story_nodes`` 中的章节节点为准。

规则：结构上不存在的章节号，正文表也不保留（树上的章删了，正文占位一并消失），新章顺延编号。
"""

from __future__ import annotations

import logging
import sqlite3
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


def _chapter_value(chapter: object, field: str) -> object:
    value = getattr(chapter, field, None)
    if value is None:
        try:
            value = chapter[field]  # type: ignore[index]
        except (KeyError, TypeError, IndexError):
            value = None
    return getattr(value, "value", value)


def _formal_identity_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            is not None
        )
    except sqlite3.DatabaseError as exc:
        raise ValueError("无法验证 Formal 身份，拒绝结构删除。") from exc


def _assert_no_formal_identity(
    chapters: list[object],
    *,
    novel_id: str,
    operation: str,
    connection: Optional[sqlite3.Connection],
) -> None:
    if connection is None:
        return
    chapter_ids = sorted(
        {
            str(value)
            for chapter in chapters
            if (value := _chapter_value(chapter, "id")) not in (None, "")
        }
    )
    chapter_numbers = sorted(
        {
            int(value)
            for chapter in chapters
            if (value := _chapter_value(chapter, "number")) not in (None, "")
        }
    )
    if not chapter_ids and not chapter_numbers:
        return

    for table, has_chapter_id in (
        ("pre_candidate_formal_history", True),
        ("chapter_candidate_formal_commits", True),
        ("chapter_narrative_commits", False),
    ):
        if not _formal_identity_table_exists(connection, table):
            continue
        predicates: list[str] = []
        params: list[object] = [novel_id]
        if has_chapter_id and chapter_ids:
            predicates.append("chapter_id IN (" + ", ".join("?" for _ in chapter_ids) + ")")
            params.extend(chapter_ids)
        if chapter_numbers:
            predicates.append(
                "chapter_number IN (" + ", ".join("?" for _ in chapter_numbers) + ")"
            )
            params.extend(chapter_numbers)
        try:
            protected = connection.execute(
                f"SELECT chapter_number FROM {table} "
                "WHERE novel_id = ? AND (" + " OR ".join(predicates) + ") LIMIT 1",
                tuple(params),
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ValueError("无法验证 Formal 身份，拒绝结构删除。") from exc
        if protected is not None:
            raise ValueError(
                f"拒绝{operation}：小说 {novel_id} 的 Formal 身份受保护。"
            )


def assert_chapter_rows_safe_to_delete(
    chapters: Iterable[object],
    *,
    novel_id: str,
    operation: str,
    connection: Optional[sqlite3.Connection] = None,
    require_empty_content: bool = True,
) -> None:
    """Reject a structural mutation before it removes authored chapter prose."""
    chapter_rows = list(chapters)
    if require_empty_content:
        authored = []
        for chapter in chapter_rows:
            content = str(_chapter_value(chapter, "content") or "").strip()
            if content:
                authored.append(int(_chapter_value(chapter, "number") or 0))

        if authored:
            numbers = ", ".join(str(number) for number in sorted(set(authored)))
            raise ValueError(
                f"拒绝{operation}：小说 {novel_id} 的第 {numbers} 章已有正文。"
                "请先通过可恢复的重写流程处理正文。"
            )
    _assert_no_formal_identity(
        chapter_rows,
        novel_id=novel_id,
        operation=operation,
        connection=connection,
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
        connection=connection,
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
