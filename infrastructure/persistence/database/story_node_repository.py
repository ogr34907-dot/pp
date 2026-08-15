"""
故事结构节点 Repository
"""

import sqlite3
import json
import logging
import hashlib
from typing import Any, List, Optional, Union
from datetime import datetime

logger = logging.getLogger(__name__)

from domain.structure.story_node import StoryNode, NodeType, StoryTree, PlanningStatus, PlanningSource
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    ProjectionWriteCapability,
    assert_story_node_write_allowed,
    is_manifest_authority,
)


class StoryNodeRepository:
    """故事结构节点仓储

    改进：接受 DatabaseConnection 实例，复用线程本地连接（WAL 模式 + busy_timeout），
    避免每个方法 connect→close 短连接模式。
    向后兼容：仍接受 db_path 字符串（与 `get_database(db_path)` 共用线程本地连接）。
    """

    def __init__(self, db: Union[str, "DatabaseConnection"]):
        # 延迟导入避免循环引用
        from infrastructure.persistence.database.connection import DatabaseConnection

        if isinstance(db, DatabaseConnection):
            self._db = db
            self.db_path = db.db_path
        elif isinstance(db, str):
            self._db = None
            self.db_path = db
        else:
            self._db = None
            self.db_path = str(db)

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接（优先复用 DatabaseConnection 线程本地连接）。"""
        if self._db is not None:
            return self._db.get_connection()
        from infrastructure.persistence.database.connection import get_database

        return get_database(self.db_path).get_connection()

    def _should_close_after_use(self) -> bool:
        """不再在方法末尾 close：db_path 模式也走全局 DatabaseConnection，避免误关线程本地连接。"""
        return False

    def _assert_write_allowed(
        self,
        novel_id: str,
        *,
        operation: str,
        capability: Optional[ProjectionWriteCapability] = None,
    ) -> None:
        assert_story_node_write_allowed(
            self._get_connection(),
            novel_id,
            operation=operation,
            capability=capability,
        )

    @staticmethod
    def _json_object(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if not raw:
            return {}
        try:
            value = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _runtime_only_update(self, node: StoryNode) -> bool:
        """Allow the manifest runtime whitelist without a full planning upsert."""

        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM story_nodes WHERE id = ? AND novel_id = ?",
            (node.id, node.novel_id),
        ).fetchone()
        if row is None:
            return False
        current = dict(row)
        protected_values = {
            "parent_id": node.parent_id,
            "node_type": node.node_type.value,
            "number": node.number,
            "title": node.title,
            "description": node.description,
            "order_index": node.order_index,
            "planning_status": node.planning_status.value,
            "planning_source": node.planning_source.value,
            "chapter_start": node.chapter_start,
            "chapter_end": node.chapter_end,
            "chapter_count": node.chapter_count,
            "suggested_chapter_count": node.suggested_chapter_count,
            "content": node.content,
            "outline": node.outline,
            "themes": json.dumps(node.themes),
            "key_events": json.dumps(node.key_events),
            "narrative_arc": node.narrative_arc,
            "conflicts": json.dumps(node.conflicts),
            "pov_character_id": node.pov_character_id,
            "timeline_start": node.timeline_start,
            "timeline_end": node.timeline_end,
        }
        for column, expected in protected_values.items():
            if current.get(column) != expected:
                return False
        old_metadata = self._json_object(current.get("metadata"))
        new_metadata = self._json_object(node.metadata)
        protected_metadata = {
            key
            for key in set(old_metadata) | set(new_metadata)
            if not str(key).startswith("runtime.")
        }
        return all(old_metadata.get(key) == new_metadata.get(key) for key in protected_metadata)

    def update_runtime_fields(
        self,
        node_id: str,
        *,
        word_count: Optional[int] = None,
        status: Optional[str] = None,
        runtime_metadata: Optional[dict[str, Any]] = None,
        _connection: Optional[sqlite3.Connection] = None,
        _commit: Optional[bool] = None,
        _verify_summary_sources: bool = False,
    ) -> bool:
        """Update only display/runtime fields while preserving planning fields.

        Callers that already own a transaction pass ``_connection`` and
        ``_commit=False``.  That keeps runtime cache invalidation in the same
        transaction as the canonical rewrite or Worldline switch.
        """

        conn = _connection or self._get_connection()
        owns_transaction = _connection is None and not conn.in_transaction
        commit_after_write = owns_transaction if _commit is None else bool(_commit)
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT novel_id, metadata FROM story_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is None:
                if owns_transaction:
                    conn.rollback()
                return False
            novel_id = str(row["novel_id"] if isinstance(row, sqlite3.Row) else row[0])
            metadata = self._json_object(
                row["metadata"] if isinstance(row, sqlite3.Row) else row[1]
            )
            for key, value in (runtime_metadata or {}).items():
                key = str(key)
                if not key.startswith("runtime."):
                    raise ValueError("runtime metadata keys must start with runtime.")
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value

            summary_patch_keys = {
                "runtime.summary",
                "runtime.summary_state",
                "runtime.checkpoint_summary",
                "runtime.checkpoint_summary_state",
            }
            summary_payload_present = any(
                str(metadata.get(summary_key, "") or "").strip()
                for summary_key in (
                    "runtime.summary",
                    "runtime.checkpoint_summary",
                )
            )
            manifest_summary_write = (
                is_manifest_authority(conn, novel_id)
                and bool(summary_patch_keys.intersection(runtime_metadata or {}))
                and summary_payload_present
            )
            if (
                (_verify_summary_sources or manifest_summary_write)
                and not self._runtime_summary_sources_are_current(
                    conn, novel_id, metadata
                )
            ):
                if owns_transaction:
                    conn.rollback()
                return False

            columns = {
                str(item[1]) for item in conn.execute("PRAGMA table_info(story_nodes)")
            }
            sets: list[str] = ["metadata = ?"]
            params: list[Any] = [json.dumps(metadata, ensure_ascii=False, sort_keys=True)]
            if "updated_at" in columns:
                sets.append("updated_at = ?")
                params.append(datetime.now().isoformat())
            if word_count is not None:
                sets.append("word_count = ?")
                params.append(int(word_count))
            if status is not None:
                sets.append("status = ?")
                params.append(str(status))
            params.append(node_id)
            conn.execute(
                f"UPDATE story_nodes SET {', '.join(sets)} WHERE id = ?", tuple(params)
            )
            if commit_after_write:
                conn.commit()
            return True
        except Exception:
            if owns_transaction and conn.in_transaction:
                conn.rollback()
            raise

    def _runtime_summary_sources_are_current(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        runtime_metadata: dict[str, Any],
    ) -> bool:
        for summary_key, state_key in (
            ("runtime.summary", "runtime.summary_state"),
            ("runtime.checkpoint_summary", "runtime.checkpoint_summary_state"),
        ):
            if not str(runtime_metadata.get(summary_key, "") or "").strip():
                continue
            state = runtime_metadata.get(state_key)
            if not self._runtime_summary_state_is_current(conn, novel_id, state):
                return False
        return True

    def visible_summary_metadata_pairs(
        self,
        node: Any,
        *,
        summary_key: str,
        state_key: str,
    ) -> tuple[tuple[str, Any], ...]:
        """Return summaries that may be consumed by the current authority.

        Legacy-authority books retain their existing committed-cache selector.
        Once a book has a Manifest Head, a summary cache is Context-visible
        only when each source chapter proves the exact Formal, Canonical, and
        Memory aftermath identity. This is intentionally a read policy: cache
        writers already verify the same predicate before storing runtime
        metadata, but a legacy key or direct SQL write must not bypass it
        after Manifest cutover.
        """

        metadata = self._json_object(getattr(node, "metadata", None))
        marker = metadata.get(f"runtime.{summary_key}_invalidated_from_chapter")
        if marker not in (None, ""):
            return ()

        novel_id = str(getattr(node, "novel_id", "") or "")
        conn = self._get_connection()
        manifest_mode = bool(novel_id) and is_manifest_authority(conn, novel_id)
        pairs: list[tuple[str, Any]] = []
        for prefix in ("runtime.", ""):
            summary = str(metadata.get(f"{prefix}{summary_key}", "") or "").strip()
            if not summary:
                continue
            state = metadata.get(f"{prefix}{state_key}") or {}
            if manifest_mode:
                if (
                    not isinstance(state, dict)
                    or str(state.get("pipeline_version") or "")
                    != "node-summary/v1"
                    or not self._runtime_summary_state_is_current(
                        conn,
                        novel_id,
                        state,
                        legacy_baseline_only=(prefix == ""),
                    )
                ):
                    continue
            pairs.append((summary, state))
        return tuple(pairs)

    def _runtime_summary_state_is_current(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        state: Any,
        *,
        legacy_baseline_only: bool = False,
    ) -> bool:
        if not isinstance(state, dict) or state.get("status") != "committed":
            return False
        expected_version = str(state.get("source_version") or "")
        chapter_start = state.get("chapter_start")
        chapter_end = state.get("chapter_end")
        if not expected_version or chapter_start is None or chapter_end is None:
            return False
        try:
            start = int(chapter_start)
            end = int(chapter_end)
        except (TypeError, ValueError):
            return False
        if start < 1 or end < start:
            return False

        raw_numbers = state.get("source_chapter_numbers")
        if raw_numbers is None:
            expected_numbers = list(range(start, end + 1))
        else:
            if not isinstance(raw_numbers, list) or not raw_numbers:
                return False
            try:
                expected_numbers = [int(number) for number in raw_numbers]
            except (TypeError, ValueError):
                return False
            if (
                any(number < start or number > end for number in expected_numbers)
                or expected_numbers != sorted(set(expected_numbers))
            ):
                return False

        placeholders = ", ".join("?" for _ in expected_numbers)
        rows = conn.execute(
            f"""
            SELECT number, content, content_sha256, content_revision
            FROM chapters
            WHERE novel_id = ? AND number IN ({placeholders})
            ORDER BY number
            """,
            (novel_id, *expected_numbers),
        ).fetchall()
        if len(rows) != len(expected_numbers):
            return False
        try:
            from infrastructure.persistence.database.chapter_candidate_repository import (
                CandidateGateError,
                ChapterCandidateRepository,
            )

            source_repository = ChapterCandidateRepository(
                self._db if self._db is not None else self.db_path
            )
            identities = source_repository.require_formal_prefix_aftermath_ready(
                novel_id,
                max(expected_numbers),
                connection=conn,
            )
        except (CandidateGateError, sqlite3.Error, ValueError):
            return False
        identities_by_number = {
            identity.chapter_number: identity for identity in identities
        }
        payload_rows = []
        for row, expected_number in zip(rows, expected_numbers):
            number = int(row[0])
            if number != expected_number:
                return False
            content = str(row[1] or "")
            content_sha256 = str(row[2] or "") or hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            content_revision = int(row[3] or 0)
            identity = identities_by_number.get(number)
            if (
                identity is None
                or identity.content_sha256 != content_sha256
                or int(identity.content_revision) != content_revision
                or (legacy_baseline_only and identity.source != "legacy")
            ):
                return False
            payload_rows.append(
                f"{number}:{content_sha256}:{content_revision}"
            )
        source_version = hashlib.sha256(
            "|".join(payload_rows).encode("utf-8")
        ).hexdigest()
        return source_version == expected_version

    @classmethod
    def _runtime_cache_may_depend_on_boundary(
        cls,
        state: Any,
        *,
        node_type: Any,
        node_number: Any,
        node_chapter_start: Any,
        node_chapter_end: Any,
        invalidated_from_chapter: int,
    ) -> bool:
        if isinstance(state, dict):
            raw_numbers = state.get("source_chapter_numbers")
            if raw_numbers is not None:
                if not isinstance(raw_numbers, list) or not raw_numbers:
                    return True
                try:
                    numbers = [int(number) for number in raw_numbers]
                except (TypeError, ValueError):
                    return True
                if numbers != sorted(set(numbers)) or any(number < 1 for number in numbers):
                    return True
                return any(number >= invalidated_from_chapter for number in numbers)
            try:
                chapter_start = int(state.get("chapter_start"))
                chapter_end = int(state.get("chapter_end"))
            except (TypeError, ValueError):
                return True
            if chapter_start < 1 or chapter_end < chapter_start:
                return True
            return chapter_end >= invalidated_from_chapter

        try:
            chapter_end = int(node_chapter_end)
        except (TypeError, ValueError):
            chapter_end = None
        if chapter_end is not None:
            return chapter_end >= invalidated_from_chapter
        try:
            chapter_start = int(node_chapter_start)
        except (TypeError, ValueError):
            chapter_start = None
        if chapter_start is not None:
            return chapter_start >= invalidated_from_chapter
        if str(getattr(node_type, "value", node_type) or "") != NodeType.CHAPTER.value:
            # A part/volume/act ordinal is not a chapter boundary.  Without
            # provenance or a chapter range it cannot prove cache safety.
            return True
        try:
            return int(node_number) >= invalidated_from_chapter
        except (TypeError, ValueError):
            return True

    def invalidate_runtime_summary_caches(
        self,
        novel_id: str,
        invalidated_from_chapter: int,
        *,
        _connection: Optional[sqlite3.Connection] = None,
        _commit: Optional[bool] = None,
    ) -> list[str]:
        """Fail closed for cached summaries that may include a changed chapter.

        This only writes the runtime namespace.  A marker deliberately hides
        runtime, legacy, and committed fallback summaries until an exact-source
        runtime regeneration clears it.
        """

        boundary = int(invalidated_from_chapter)
        if boundary < 1:
            raise ValueError("invalidated_from_chapter must be positive")
        conn = _connection or self._get_connection()
        owns_transaction = _connection is None and not conn.in_transaction
        commit_after_write = owns_transaction if _commit is None else bool(_commit)
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT id, node_type, number, chapter_start, chapter_end, metadata
                FROM story_nodes
                WHERE novel_id = ?
                """,
                (novel_id,),
            ).fetchall()
            invalidated: list[str] = []
            for row in rows:
                metadata = self._json_object(row[5])
                committed = self._json_object(metadata.get("committed_metadata"))
                runtime_patch: dict[str, Any] = {}
                for summary_key, state_key in (
                    ("summary", "summary_state"),
                    ("checkpoint_summary", "checkpoint_summary_state"),
                ):
                    summary_sources = (
                        (
                            metadata.get(f"runtime.{summary_key}"),
                            metadata.get(f"runtime.{state_key}"),
                        ),
                        (metadata.get(summary_key), metadata.get(state_key)),
                        (committed.get(summary_key), committed.get(state_key)),
                    )
                    existing_sources = [
                        state
                        for summary, state in summary_sources
                        if str(summary or "").strip()
                    ]
                    if not existing_sources or not any(
                        self._runtime_cache_may_depend_on_boundary(
                            state,
                            node_type=row[1],
                            node_number=row[2],
                            node_chapter_start=row[3],
                            node_chapter_end=row[4],
                            invalidated_from_chapter=boundary,
                        )
                        for state in existing_sources
                    ):
                        continue
                    # Remove the cached payload and provenance rather than
                    # retaining readable stale facts in runtime metadata.  The
                    # marker still fences legacy/committed fallback text until
                    # a fresh exact-source runtime summary clears it.
                    for runtime_key in (
                        f"runtime.{summary_key}",
                        f"runtime.{state_key}",
                        f"runtime.{summary_key}_generated_at",
                    ):
                        if runtime_key in metadata:
                            runtime_patch[runtime_key] = None
                    runtime_patch[
                        f"runtime.{summary_key}_invalidated_from_chapter"
                    ] = boundary
                if not runtime_patch:
                    continue
                if not self.update_runtime_fields(
                    str(row[0]),
                    runtime_metadata=runtime_patch,
                    _connection=conn,
                    _commit=False,
                ):
                    raise RuntimeError(f"story node disappeared during runtime invalidation: {row[0]}")
                invalidated.append(str(row[0]))
            if commit_after_write:
                conn.commit()
            return invalidated
        except Exception:
            if owns_transaction and conn.in_transaction:
                conn.rollback()
            raise

    def save_sync(
        self,
        node: StoryNode,
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> StoryNode:
        """同步保存（供 NovelService 等非 async 调用链使用）。"""
        conn = self._get_connection()
        self._assert_write_allowed(
            node.novel_id,
            operation="save_sync",
            capability=_capability,
        )
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO story_nodes (
                    id, novel_id, parent_id, node_type, number, title, description, order_index,
                    planning_status, planning_source,
                    chapter_start, chapter_end, chapter_count, suggested_chapter_count,
                    content, outline, word_count, status,
                    themes, key_events, narrative_arc, conflicts,
                    pov_character_id, timeline_start, timeline_end,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node.id,
                node.novel_id,
                node.parent_id,
                node.node_type.value,
                node.number,
                node.title,
                node.description,
                node.order_index,
                node.planning_status.value,
                node.planning_source.value,
                node.chapter_start,
                node.chapter_end,
                node.chapter_count,
                node.suggested_chapter_count,
                node.content,
                node.outline,
                node.word_count,
                node.status,
                json.dumps(node.themes),
                json.dumps(node.key_events),
                node.narrative_arc,
                json.dumps(node.conflicts),
                node.pov_character_id,
                node.timeline_start,
                node.timeline_end,
                json.dumps(node.metadata),
                node.created_at.isoformat(),
                node.updated_at.isoformat(),
            ))
            if _capability is None:
                conn.commit()
            return node
        finally:
            if self._should_close_after_use():
                conn.close()

    async def save(self, node: StoryNode) -> StoryNode:
        """保存节点"""
        return self.save_sync(node)

    async def update(
        self,
        node: StoryNode,
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> StoryNode:
        """更新节点"""
        node.updated_at = datetime.now()
        conn = self._get_connection()
        if _capability is None and self._runtime_only_update(node):
            self.update_runtime_fields(
                node.id,
                word_count=node.word_count,
                status=node.status,
                runtime_metadata={
                    str(key): value
                    for key, value in self._json_object(node.metadata).items()
                    if str(key).startswith("runtime.")
                },
            )
            return node
        self._assert_write_allowed(
            node.novel_id,
            operation="update",
            capability=_capability,
        )
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE story_nodes SET
                    parent_id = ?,
                    node_type = ?,
                    number = ?,
                    title = ?,
                    description = ?,
                    order_index = ?,
                    planning_status = ?,
                    planning_source = ?,
                    chapter_start = ?,
                    chapter_end = ?,
                    chapter_count = ?,
                    suggested_chapter_count = ?,
                    content = ?,
                    outline = ?,
                    word_count = ?,
                    status = ?,
                    themes = ?,
                    key_events = ?,
                    narrative_arc = ?,
                    conflicts = ?,
                    pov_character_id = ?,
                    timeline_start = ?,
                    timeline_end = ?,
                    metadata = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                node.parent_id,
                node.node_type.value,
                node.number,
                node.title,
                node.description,
                node.order_index,
                node.planning_status.value,
                node.planning_source.value,
                node.chapter_start,
                node.chapter_end,
                node.chapter_count,
                node.suggested_chapter_count,
                node.content,
                node.outline,
                node.word_count,
                node.status,
                json.dumps(node.themes),
                json.dumps(node.key_events),
                node.narrative_arc,
                json.dumps(node.conflicts),
                node.pov_character_id,
                node.timeline_start,
                node.timeline_end,
                json.dumps(node.metadata),
                node.updated_at.isoformat(),
                node.id,
            ))
            if _capability is None:
                conn.commit()
            return node
        finally:
            if self._should_close_after_use():
                conn.close()

    async def save_batch(
        self,
        nodes: List[StoryNode],
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> List[StoryNode]:
        """批量保存节点"""
        if not nodes:
            return nodes
        conn = self._get_connection()
        if _capability is None:
            for node in nodes:
                if is_manifest_authority(conn, node.novel_id) and self._runtime_only_update(node):
                    self.update_runtime_fields(
                        node.id,
                        word_count=node.word_count,
                        status=node.status,
                        runtime_metadata={
                            str(key): value
                            for key, value in self._json_object(node.metadata).items()
                            if str(key).startswith("runtime.")
                        },
                    )
                    continue
                self._assert_write_allowed(node.novel_id, operation="save_batch")
        else:
            for node in nodes:
                self._assert_write_allowed(
                    node.novel_id,
                    operation="save_batch",
                    capability=_capability,
                )
        try:
            cursor = conn.cursor()
            for node in nodes:
                try:
                    if _capability is None and is_manifest_authority(conn, node.novel_id) and self._runtime_only_update(node):
                        continue
                    cursor.execute("""
                        INSERT INTO story_nodes (
                            id, novel_id, parent_id, node_type, number, title, description, order_index,
                            planning_status, planning_source,
                            chapter_start, chapter_end, chapter_count, suggested_chapter_count,
                            content, outline, word_count, status,
                            themes, key_events, narrative_arc, conflicts,
                            pov_character_id, timeline_start, timeline_end,
                            metadata, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            novel_id = excluded.novel_id,
                            parent_id = excluded.parent_id,
                            node_type = excluded.node_type,
                            number = excluded.number,
                            title = excluded.title,
                            description = excluded.description,
                            order_index = excluded.order_index,
                            planning_status = excluded.planning_status,
                            planning_source = excluded.planning_source,
                            chapter_start = excluded.chapter_start,
                            chapter_end = excluded.chapter_end,
                            chapter_count = excluded.chapter_count,
                            suggested_chapter_count = excluded.suggested_chapter_count,
                            content = excluded.content,
                            outline = excluded.outline,
                            word_count = excluded.word_count,
                            status = excluded.status,
                            themes = excluded.themes,
                            key_events = excluded.key_events,
                            narrative_arc = excluded.narrative_arc,
                            conflicts = excluded.conflicts,
                            pov_character_id = excluded.pov_character_id,
                            timeline_start = excluded.timeline_start,
                            timeline_end = excluded.timeline_end,
                            metadata = excluded.metadata,
                            updated_at = excluded.updated_at
                    """, (
                        node.id,
                        node.novel_id,
                        node.parent_id,
                        node.node_type.value,
                        node.number,
                        node.title,
                        node.description,
                        node.order_index,
                        node.planning_status.value,
                        node.planning_source.value,
                        node.chapter_start,
                        node.chapter_end,
                        node.chapter_count,
                        node.suggested_chapter_count,
                        node.content,
                        node.outline,
                        node.word_count,
                        node.status,
                        json.dumps(node.themes),
                        json.dumps(node.key_events),
                        node.narrative_arc,
                        json.dumps(node.conflicts),
                        node.pov_character_id,
                        node.timeline_start,
                        node.timeline_end,
                        json.dumps(node.metadata),
                        node.created_at.isoformat(),
                        node.updated_at.isoformat(),
                    ))
                except sqlite3.IntegrityError as e:
                    if "FOREIGN KEY" in str(e):
                        if not self._novel_exists(cursor, node.novel_id):
                            logger.warning(
                                "save_batch: novel %s 已被删除，跳过 story_node %s 的写入",
                                node.novel_id, node.id,
                            )
                            continue
                    raise
            if _capability is None:
                conn.commit()
            return nodes
        finally:
            if self._should_close_after_use():
                conn.close()

    @staticmethod
    def _novel_exists(cursor: sqlite3.Cursor, novel_id: str) -> bool:
        cursor.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,))
        return cursor.fetchone() is not None

    async def get_by_id(self, node_id: str) -> Optional[StoryNode]:
        """根据 ID 获取节点"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM story_nodes WHERE id = ?", (node_id,))
            row = cursor.fetchone()
            return self._row_to_entity(row) if row else None
        finally:
            if self._should_close_after_use():
                conn.close()

    def get_by_novel_sync(self, novel_id: str) -> List[StoryNode]:
        """同步列出某小说的全部结构节点。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM story_nodes
                WHERE novel_id = ?
                ORDER BY order_index
            """, (novel_id,))
            rows = cursor.fetchall()
            return [self._row_to_entity(row) for row in rows]
        finally:
            if self._should_close_after_use():
                conn.close()

    async def get_by_novel(self, novel_id: str) -> List[StoryNode]:
        """获取小说的所有节点"""
        return self.get_by_novel_sync(novel_id)

    def get_tree_sync(self, novel_id: str) -> StoryTree:
        """同步获取结构树（供 NovelService 使用）。"""
        return StoryTree(novel_id=novel_id, nodes=self.get_by_novel_sync(novel_id))

    async def get_tree(self, novel_id: str) -> StoryTree:
        """获取小说的结构树"""
        return self.get_tree_sync(novel_id)

    def get_children_sync(self, parent_id: str) -> List[StoryNode]:
        """同步获取子节点。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM story_nodes
                WHERE parent_id = ?
                ORDER BY order_index
            """, (parent_id,))
            rows = cursor.fetchall()
            return [self._row_to_entity(row) for row in rows]
        finally:
            if self._should_close_after_use():
                conn.close()

    async def get_children(self, parent_id: str) -> List[StoryNode]:
        """获取子节点"""
        return self.get_children_sync(parent_id)

    async def get_chapters_by_novel(self, novel_id: str) -> List[StoryNode]:
        """获取小说的所有章节"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM story_nodes
                WHERE novel_id = ? AND node_type = 'chapter'
                ORDER BY order_index
            """, (novel_id,))
            rows = cursor.fetchall()
            return [self._row_to_entity(row) for row in rows]
        finally:
            if self._should_close_after_use():
                conn.close()

    async def delete(
        self,
        node_id: str,
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> bool:
        """删除节点（级联删除子节点）"""
        conn = self._get_connection()
        try:
            row = cursor = conn.execute(
                "SELECT novel_id FROM story_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row is not None:
                novel_id = row["novel_id"] if isinstance(row, sqlite3.Row) else row[0]
                self._assert_write_allowed(
                    str(novel_id),
                    operation="delete",
                    capability=_capability,
                )
            cursor = conn.cursor()
            cursor.execute("DELETE FROM story_nodes WHERE id = ?", (node_id,))
            if _capability is None:
                conn.commit()
            return cursor.rowcount > 0
        finally:
            if self._should_close_after_use():
                conn.close()

    async def delete_by_novel(
        self,
        novel_id: str,
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> int:
        """删除小说的所有节点"""
        conn = self._get_connection()
        try:
            self._assert_write_allowed(
                novel_id,
                operation="delete_by_novel",
                capability=_capability,
            )
            cursor = conn.cursor()
            cursor.execute("DELETE FROM story_nodes WHERE novel_id = ?", (novel_id,))
            if _capability is None:
                conn.commit()
            return cursor.rowcount
        finally:
            if self._should_close_after_use():
                conn.close()

    async def apply_merge_plan(
        self,
        creates: List[dict],
        updates: List[dict],
        deletes: List[str],
        *,
        _capability: Optional[ProjectionWriteCapability] = None,
    ) -> None:
        """应用宏观规划合并计划（原子性事务）

        Args:
            creates: 需要创建的节点字典列表
            updates: 需要更新的节点字典列表
            deletes: 需要删除的节点 ID 列表
        """
        conn = self._get_connection()
        novels = {
            str(item.get("novel_id"))
            for item in creates
            if item.get("novel_id")
        }
        lookup_ids = deletes + [str(item.get("id")) for item in updates if item.get("id")]
        if lookup_ids:
            rows = conn.execute(
                "SELECT DISTINCT novel_id FROM story_nodes WHERE id IN (%s)"
                % ",".join("?" for _ in lookup_ids),
                tuple(lookup_ids),
            ).fetchall() if lookup_ids else []
            novels.update(str(row[0]) for row in rows)
        if (creates or updates or deletes) and not novels:
            raise PlanningAuthorityError(
                "cannot determine StoryNode novel scope for merge plan"
            )
        for novel_id in novels:
            self._assert_write_allowed(
                novel_id,
                operation="apply_merge_plan",
                capability=_capability,
            )
        try:
            owns_transaction = _capability is None
            if owns_transaction:
                conn.execute("BEGIN")
            cursor = conn.cursor()

            # 1. 批量删除
            if deletes:
                placeholders = ",".join(["?"] * len(deletes))
                cursor.execute(f"DELETE FROM story_nodes WHERE id IN ({placeholders})", deletes)

            # 2. 批量更新（保留宏观结构的可执行容量）
            if updates:
                for u in updates:
                    cursor.execute("""
                        UPDATE story_nodes
                        SET title=?, description=?, order_index=?,
                            suggested_chapter_count=COALESCE(?, suggested_chapter_count),
                            themes=?, key_events=?, narrative_arc=?, conflicts=?, metadata=?,
                            updated_at=?
                        WHERE id=?
                    """, (
                        u['title'],
                        u.get('description', ''),
                        u['order_index'],
                        u.get('suggested_chapter_count'),
                        json.dumps(u.get('themes', [])),
                        json.dumps(u.get('key_events', [])),
                        u.get('narrative_arc'),
                        json.dumps(u.get('conflicts', [])),
                        json.dumps(u.get('metadata', {})),
                        datetime.now().isoformat(),
                        u['id']
                    ))

            # 3. 批量插入
            if creates:
                for c in creates:
                    cursor.execute("""
                        INSERT INTO story_nodes (
                            id, novel_id, parent_id, node_type, number, title, description, order_index,
                            planning_status, planning_source,
                            chapter_start, chapter_end, chapter_count, suggested_chapter_count,
                            content, outline, word_count, status,
                            themes, key_events, narrative_arc, conflicts,
                            pov_character_id, timeline_start, timeline_end,
                            metadata, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        c['id'],
                        c['novel_id'],
                        c.get('parent_id'),
                        c['node_type'],
                        c.get('number', 0),
                        c['title'],
                        c.get('description', ''),
                        c['order_index'],
                        c.get('planning_status', 'ai_generated'),
                        c.get('planning_source', 'ai_macro'),
                        c.get('chapter_start'),
                        c.get('chapter_end'),
                        c.get('chapter_count'),
                        c.get('suggested_chapter_count'),
                        c.get('content'),
                        c.get('outline'),
                        c.get('word_count', 0),
                        c.get('status', 'draft'),
                        json.dumps(c.get('themes', [])),
                        json.dumps(c.get('key_events', [])),
                        c.get('narrative_arc'),
                        json.dumps(c.get('conflicts', [])),
                        c.get('pov_character_id'),
                        c.get('timeline_start'),
                        c.get('timeline_end'),
                        json.dumps(c.get('metadata', {})),
                        datetime.now().isoformat(),
                        datetime.now().isoformat(),
                    ))

            if owns_transaction:
                conn.commit()
        except Exception as e:
            if _capability is None:
                conn.rollback()
            raise e
        finally:
            if self._should_close_after_use():
                conn.close()

    def _row_to_entity(self, row: sqlite3.Row) -> StoryNode:
        """将数据库行转换为实体"""
        # sqlite3.Row 不支持 .get() 方法，需要先转换为字典
        row_dict = dict(row)

        return StoryNode(
            id=row_dict["id"],
            novel_id=row_dict["novel_id"],
            parent_id=row_dict["parent_id"],
            node_type=NodeType(row_dict["node_type"]),
            number=row_dict["number"],
            title=row_dict["title"],
            description=row_dict["description"],
            order_index=row_dict["order_index"],

            planning_status=PlanningStatus(row_dict.get("planning_status", "draft")),
            planning_source=PlanningSource(row_dict.get("planning_source", "manual")),

            chapter_start=row_dict["chapter_start"],
            chapter_end=row_dict["chapter_end"],
            chapter_count=row_dict["chapter_count"],
            suggested_chapter_count=row_dict.get("suggested_chapter_count"),

            content=row_dict["content"],
            outline=row_dict.get("outline"),
            word_count=row_dict["word_count"],
            status=row_dict["status"],

            themes=row_dict.get("themes", "[]"),
            key_events=row_dict.get("key_events", "[]"),
            narrative_arc=row_dict.get("narrative_arc"),
            conflicts=row_dict.get("conflicts", "[]"),

            pov_character_id=row_dict.get("pov_character_id"),
            timeline_start=row_dict.get("timeline_start"),
            timeline_end=row_dict.get("timeline_end"),

            metadata=row_dict.get("metadata", "{}"),

            created_at=datetime.fromisoformat(row_dict["created_at"]),
            updated_at=datetime.fromisoformat(row_dict["updated_at"]),
        )

    async def update_chapter_ranges(self, novel_id: str) -> None:
        """根据子节点的 chapter_start/chapter_end 更新父节点的章节范围"""
        conn = self._get_connection()
        self._assert_write_allowed(novel_id, operation="update_chapter_ranges")
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, parent_id, chapter_start, chapter_end, node_type
                FROM story_nodes WHERE novel_id = ?
                ORDER BY order_index
            """, (novel_id,))
            rows = cursor.fetchall()

            nodes_by_parent = {}
            for row in rows:
                pid = row[1]
                if pid not in nodes_by_parent:
                    nodes_by_parent[pid] = []
                nodes_by_parent[pid].append(row)

            for parent_id, children in nodes_by_parent.items():
                if not children:
                    continue
                starts = [r[2] for r in children if r[2] is not None]
                ends = [r[3] for r in children if r[3] is not None]
                if starts and ends:
                    new_start = min(starts)
                    new_end = max(ends)
                    cursor.execute("""
                        UPDATE story_nodes
                        SET chapter_start = ?, chapter_end = ?, updated_at = ?
                        WHERE id = ?
                    """, (new_start, new_end, datetime.now().isoformat(), parent_id))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            if self._should_close_after_use():
                conn.close()

    def bulk_replace_text_sync(
        self,
        novel_id: str,
        old_name: str,
        new_name: str,
    ) -> int:
        """将 story_nodes 中 title / description / outline 字段里的 old_name 替换为 new_name。

        用于 Bible 改名后同步刷新结构大纲文本，避免大纲里遗留旧名导致正文生成使用旧名。
        通过 SQLite replace() 函数原地替换，不加载到 Python 层，效率高。

        Returns:
            受影响的行数。
        """
        if not old_name or old_name == new_name:
            return 0
        conn = self._get_connection()
        self._assert_write_allowed(novel_id, operation="bulk_replace_text_sync")
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE story_nodes
                SET
                    title       = replace(title,       ?, ?),
                    description = replace(description, ?, ?),
                    outline     = replace(outline,     ?, ?)
                WHERE novel_id = ?
                  AND (
                      instr(title,       ?) > 0
                   OR instr(description, ?) > 0
                   OR instr(outline,     ?) > 0
                  )
                """,
                (
                    old_name, new_name,
                    old_name, new_name,
                    old_name, new_name,
                    novel_id,
                    old_name, old_name, old_name,
                ),
            )
            affected = cursor.rowcount
            conn.commit()
            return affected
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_use():
                conn.close()
