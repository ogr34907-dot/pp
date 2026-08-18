"""
故事结构服务

由 AI 动态规划驱动的故事结构管理，替代原有的硬编码结构生成逻辑。
支持持续优化和智能合并，确保已有正文章节不会被覆盖或丢失。
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, TYPE_CHECKING, Set

from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from domain.structure.story_node import StoryNode, NodeType
from application.blueprint.services.chapter_book_structure_sync import (
    assert_chapter_rows_safe_to_delete,
    purge_chapter_book_rows_not_matching_structure,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.planning_authority_guard import (
    assert_story_node_write_allowed,
)

if TYPE_CHECKING:
    from application.novel.chapter_renumber.coordinator import ChapterRenumberCoordinator
    from domain.novel.repositories.chapter_repository import ChapterRepository
    from application.blueprint.services.continuous_planning_service import ContinuousPlanningService


class StoryStructureService:
    """故事结构服务

    核心特性：
    1. AI 动态规划：利用 LLM 生成"部-卷-幕"结构，非固定模板
    2. 安全合并机制：带有血缘继承的智能合并
    3. 结构与正文表对齐：显式删节点时同步清理正文占位；读整树不做破坏性清理
    4. 持续优化：支持通过提示词迭代和测试不断提升规划质量
    """

    def __init__(
        self,
        repository: StoryNodeRepository,
        chapter_repository: Optional["ChapterRepository"] = None,
        chapter_renumber_coordinator: Optional["ChapterRenumberCoordinator"] = None,
        planning_service: Optional["ContinuousPlanningService"] = None,
    ):
        self.repository = repository
        self._chapter_repository = chapter_repository
        self._chapter_renumber_coordinator = chapter_renumber_coordinator
        self._planning_service = planning_service

    def _enrich_chapter_nodes_from_chapters_table(
        self, novel_id: str, nodes: List[Dict[str, Any]]
    ) -> None:
        """章节正文写入 chapters 表后，story_nodes.word_count 可能未同步；展示时以章节表为准。"""
        if not self._chapter_repository or not nodes:
            return
        try:
            chapters = self._chapter_repository.list_by_novel(NovelId(novel_id))
        except Exception:
            return
        by_num = {c.number: c for c in chapters}

        def walk(ns: List[Dict[str, Any]]) -> None:
            for n in ns:
                if n.get("node_type") == "chapter":
                    num = n.get("number")
                    ch = by_num.get(num) if num is not None else None
                    if ch is not None:
                        wc = ch.word_count.value if hasattr(ch.word_count, "value") else int(ch.word_count)
                        n["word_count"] = int(wc)
                        st = ch.status.value if hasattr(ch.status, "value") else ch.status
                        n["status"] = st
                if n.get("children"):
                    walk(n["children"])

        walk(nodes)

    def _enrich_flat_chapter_nodes(self, novel_id: str, nodes: List[Dict[str, Any]]) -> None:
        if not self._chapter_repository or not nodes:
            return
        try:
            chapters = self._chapter_repository.list_by_novel(NovelId(novel_id))
        except Exception:
            return
        by_num = {c.number: c for c in chapters}
        for n in nodes:
            if n.get("node_type") != "chapter":
                continue
            num = n.get("number")
            ch = by_num.get(num) if num is not None else None
            if ch is None:
                continue
            wc = ch.word_count.value if hasattr(ch.word_count, "value") else int(ch.word_count)
            n["word_count"] = int(wc)
            st = ch.status.value if hasattr(ch.status, "value") else ch.status
            n["status"] = st

    async def get_tree(self, novel_id: str) -> Dict[str, Any]:
        """获取小说的完整结构树。"""
        # 注意：读接口必须无副作用。
        # 守护进程写入章节规划和前端刷新结构树可能并发发生；如果在读路径里做
        # “结构↔正文”清理，API 进程可能基于尚未刷新到当前连接快照的结构树，
        # 误删刚生成的正文章节，并触发章节仓储的重排级联删除 story_nodes。
        # 因此这里只展示结构；真正的级联清理只放在显式删除/重规划等写路径。

        tree = await self.repository.get_tree(novel_id)
        data = tree.to_tree_dict()
        self._enrich_chapter_nodes_from_chapters_table(novel_id, data.get("nodes") or [])
        return {
            "novel_id": novel_id,
            "tree": data,
        }

    async def get_children(self, novel_id: str, parent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取子节点（用于渐进式加载）"""
        nodes = await self.repository.get_children(parent_id)
        out = [node.to_dict() for node in nodes]
        self._enrich_flat_chapter_nodes(novel_id, out)
        return out

    async def create_node(
        self,
        novel_id: str,
        node_type: str,
        number: int,
        title: str,
        parent_id: Optional[str] = None,
        description: Optional[str] = None,
        order_index: Optional[int] = None
    ) -> Dict[str, Any]:
        """创建节点"""
        # 验证节点类型
        try:
            node_type_enum = NodeType(node_type)
        except ValueError:
            raise ValueError(f"Invalid node_type: {node_type}")

        # 如果未指定 order_index，自动计算
        if order_index is None:
            siblings = await self.repository.get_children(parent_id)
            order_index = len(siblings)

        # 创建节点
        node = StoryNode(
            id=f"node-{uuid.uuid4().hex[:12]}",
            novel_id=novel_id,
            parent_id=parent_id,
            node_type=node_type_enum,
            number=number,
            title=title,
            description=description,
            order_index=order_index
        )

        saved_node = await self.repository.save(node)
        return saved_node.to_dict()

    async def update_node(
        self,
        node_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        number: Optional[int] = None
    ) -> Dict[str, Any]:
        """更新节点"""
        node = await self.repository.get_by_id(node_id)
        if not node:
            raise ValueError(f"Node not found: {node_id}")

        if title is not None:
            node.title = title
        if description is not None:
            node.description = description
        if number is not None:
            node.number = number

        saved_node = await self.repository.update(node)
        return saved_node.to_dict()

    def _collect_descendant_chapter_numbers(self, novel_id: str, root_id: str) -> List[int]:
        """找出子树内挂着的正文章节编号，供删结构时同步清理正文库。"""
        nodes = self.repository.get_by_novel_sync(novel_id)
        by_id = {node.id: node for node in nodes}
        root = by_id.get(root_id)
        if root is None:
            return []

        children_by_parent: Dict[str, List[StoryNode]] = {}
        for node in nodes:
            if node.parent_id:
                children_by_parent.setdefault(node.parent_id, []).append(node)

        chapter_numbers: Set[int] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if node.node_type == NodeType.CHAPTER:
                try:
                    chapter_numbers.add(int(node.number))
                except (TypeError, ValueError):
                    pass
            stack.extend(children_by_parent.get(node.id, []))

        return sorted(chapter_numbers, reverse=True)

    async def delete_node(self, node_id: str) -> bool:
        """删除节点，并同步清理关联的正文章节。"""
        node = await self.repository.get_by_id(node_id)
        if not node:
            return False
        get_connection = getattr(self.repository, "_get_connection", None)
        chapter_delete_body = getattr(
            self._chapter_repository, "_delete_chapter_transaction_body", None
        )
        chapter_db = getattr(self._chapter_repository, "db", None)
        get_chapter_connection = getattr(chapter_db, "get_connection", None)
        if callable(get_connection):
            connection = get_connection()
            assert_story_node_write_allowed(
                connection,
                node.novel_id,
                operation="structure delete",
            )
            if callable(chapter_delete_body):
                if not callable(get_chapter_connection):
                    raise RuntimeError("transactional chapter repository has no database connection")
                if get_chapter_connection() is not connection:
                    raise RuntimeError("story and chapter repositories must share one SQLite connection")
                if connection.in_transaction:
                    raise RuntimeError("structure delete requires a clean SQLite connection")

                foreign_keys_enabled = bool(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                connection.execute("PRAGMA foreign_keys = OFF")
                deleted_chapters: list[int] = []
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    locked_node = await self.repository.get_by_id(node_id)
                    if locked_node is None:
                        connection.rollback()
                        return False
                    assert_story_node_write_allowed(
                        connection,
                        locked_node.novel_id,
                        operation="structure delete",
                    )

                    nodes = self.repository.get_by_novel_sync(locked_node.novel_id)
                    children_by_parent: Dict[str, List[StoryNode]] = {}
                    for candidate in nodes:
                        if candidate.parent_id:
                            children_by_parent.setdefault(candidate.parent_id, []).append(candidate)
                    subtree = []
                    stack = [locked_node]
                    while stack:
                        candidate = stack.pop()
                        subtree.append(candidate)
                        stack.extend(children_by_parent.get(candidate.id, []))

                    chapter_numbers = sorted(
                        {
                            int(candidate.number)
                            for candidate in subtree
                            if candidate.node_type == NodeType.CHAPTER
                        },
                        reverse=True,
                    )
                    chapters = []
                    for chapter_number in chapter_numbers:
                        chapter = self._chapter_repository.get_by_novel_and_number(
                            NovelId(locked_node.novel_id), chapter_number
                        )
                        if chapter is not None:
                            chapters.append((chapter_number, chapter))
                    assert_chapter_rows_safe_to_delete(
                        [chapter for _, chapter in chapters],
                        novel_id=locked_node.novel_id,
                        operation="结构删除章节",
                        connection=connection,
                    )

                    now = datetime.now(timezone.utc).isoformat()
                    for chapter_number, chapter in chapters:
                        chapter_id = (
                            chapter.id.value if hasattr(chapter.id, "value") else chapter.id
                        )
                        chapter_delete_body(
                            connection,
                            locked_node.novel_id,
                            chapter_number,
                            chapter_id,
                            now,
                        )
                        deleted_chapters.append(chapter_number)

                    for candidate in subtree:
                        if candidate.id == node_id:
                            continue
                        if connection.execute(
                            "SELECT 1 FROM story_nodes WHERE id = ?", (candidate.id,)
                        ).fetchone() is None:
                            continue
                        cursor = connection.execute(
                            "DELETE FROM story_nodes WHERE id = ?", (candidate.id,)
                        )
                        if cursor.rowcount <= 0:
                            connection.rollback()
                            return False

                    if connection.execute(
                        "SELECT 1 FROM story_nodes WHERE id = ?", (node_id,)
                    ).fetchone() is not None:
                        cursor = connection.execute(
                            "DELETE FROM story_nodes WHERE id = ?", (node_id,)
                        )
                        if cursor.rowcount <= 0:
                            connection.rollback()
                            return False
                    if connection.execute(
                        "SELECT 1 FROM story_nodes WHERE id = ?", (node_id,)
                    ).fetchone() is not None:
                        connection.rollback()
                        return False

                    remaining_structure_numbers = set()
                    for candidate in self.repository.get_by_novel_sync(locked_node.novel_id):
                        if candidate.node_type != NodeType.CHAPTER:
                            continue
                        try:
                            remaining_structure_numbers.add(int(candidate.number))
                        except (TypeError, ValueError):
                            continue
                    orphan_chapters = []
                    for chapter in self._chapter_repository.list_by_novel(
                        NovelId(locked_node.novel_id)
                    ):
                        try:
                            chapter_number = int(chapter.number)
                        except (TypeError, ValueError):
                            continue
                        if chapter_number not in remaining_structure_numbers:
                            orphan_chapters.append((chapter_number, chapter))
                    orphan_chapters.sort(key=lambda item: item[0], reverse=True)
                    assert_chapter_rows_safe_to_delete(
                        [chapter for _, chapter in orphan_chapters],
                        novel_id=locked_node.novel_id,
                        operation="结构同步删除章节",
                        connection=connection,
                    )
                    for chapter_number, chapter in orphan_chapters:
                        chapter_id = (
                            chapter.id.value if hasattr(chapter.id, "value") else chapter.id
                        )
                        chapter_delete_body(
                            connection,
                            locked_node.novel_id,
                            chapter_number,
                            chapter_id,
                            now,
                        )
                        deleted_chapters.append(chapter_number)
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.execute(
                        "PRAGMA foreign_keys = "
                        + ("ON" if foreign_keys_enabled else "OFF")
                    )

                coordinator = self._chapter_renumber_coordinator
                if coordinator is not None:
                    for chapter_number in deleted_chapters:
                        coordinator.on_chapter_deleted(
                            locked_node.novel_id, chapter_number
                        )
                return True

        deleted_any = False
        if self._chapter_repository is not None:
            chapter_numbers = self._collect_descendant_chapter_numbers(node.novel_id, node_id)
            chapters = []
            for chapter_number in chapter_numbers:
                chapter = self._chapter_repository.get_by_novel_and_number(
                    NovelId(node.novel_id), chapter_number
                )
                if chapter is None:
                    continue
                chapters.append((chapter_number, chapter))
            connection = get_connection() if callable(get_connection) else None
            assert_chapter_rows_safe_to_delete(
                [chapter for _, chapter in chapters],
                novel_id=node.novel_id,
                operation="结构删除章节",
                connection=connection,
            )
            for chapter_number, chapter in chapters:
                chapter_id = chapter.id.value if hasattr(chapter.id, "value") else chapter.id
                self._chapter_repository.delete(ChapterId(chapter_id))
                coordinator = self._chapter_renumber_coordinator
                if coordinator is not None:
                    coordinator.on_chapter_deleted(node.novel_id, chapter_number)
                deleted_any = True

        novel_id_scope = node.novel_id
        outcome: bool
        remaining = await self.repository.get_by_id(node_id)
        if remaining is None:
            outcome = node.node_type == NodeType.CHAPTER and deleted_any
        else:
            deleted_node = await self.repository.delete(node_id)
            if deleted_node:
                outcome = True
            elif await self.repository.get_by_id(node_id) is None:
                outcome = True
            else:
                outcome = False
        # 删掉子树后再扫一遍全书，摘掉「树上没有的」残留正文行。
        if outcome and self._chapter_repository is not None:
            purge_chapter_book_rows_not_matching_structure(
                self.repository,
                self._chapter_repository,
                novel_id_scope,
            )
        return outcome

    async def reorder_nodes(self, node_ids: List[str]) -> List[Dict[str, Any]]:
        """重新排序节点"""
        nodes = []
        for idx, node_id in enumerate(node_ids):
            node = await self.repository.get_by_id(node_id)
            if node:
                node.order_index = idx
                nodes.append(node)

        saved_nodes = await self.repository.save_batch(nodes)
        return [node.to_dict() for node in saved_nodes]

    async def update_chapter_ranges(self, novel_id: str):
        """更新章节范围"""
        await self.repository.update_chapter_ranges(novel_id)

    async def create_default_structure(
        self,
        novel_id: str,
        total_chapters: int = 100,
        structure_preference: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """创建默认结构（AI 动态规划驱动）

        替代原有的硬编码逻辑，由 AI 模型根据小说设定智能规划"部-卷-幕"结构。
        支持两种模式：
        - 极速模式（structure_preference=None）：AI 自主决定最优结构
        - 精密模式（structure_preference=dict）：用户指定结构网格，AI 填充内容

        Args:
            novel_id: 小说 ID
            total_chapters: 目标总章数
            structure_preference: 结构偏好配置
                {
                    "parts": 3,              # 部数
                    "volumes_per_part": 3,   # 每部卷数
                    "acts_per_volume": 3     # 每卷幕数
                }
                若为 None，则使用极速模式

        Returns:
            包含生成结构树的字典

        Raises:
            RuntimeError: 当 planning_service 未注入或 AI 规划失败时
        """
        # 检查 planning_service 是否可用
        if self._planning_service is None:
            raise RuntimeError(
                "AI 规划服务未初始化。请确保在创建 StoryStructureService 时 "
                "传入了 planning_service 参数。"
            )

        # 步骤 1：调用 AI 生成宏观规划
        plan_result = await self._planning_service.generate_macro_plan(
            novel_id=novel_id,
            target_chapters=total_chapters,
            structure_preference=structure_preference
        )

        if not plan_result.get("success"):
            raise RuntimeError(f"AI 结构规划失败: {plan_result.get('error', '未知错误')}")

        structure = plan_result.get("structure", [])
        if not structure:
            raise RuntimeError("AI 返回的结构为空")

        # 步骤 2：使用安全合并机制保存结构
        # 这会保护已有正文章节，避免数据丢失
        merge_result = await self._planning_service.confirm_macro_plan_safe(
            novel_id=novel_id,
            structure=structure
        )

        if not merge_result.get("success"):
            raise RuntimeError(f"结构保存失败: {merge_result.get('message', '未知错误')}")

        # 步骤 3：更新章节范围并返回树结构
        await self.update_chapter_ranges(novel_id)

        return await self.get_tree(novel_id)
