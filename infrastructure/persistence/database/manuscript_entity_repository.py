"""手稿实体：道具 CRUD + 章节提及索引。"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from infrastructure.persistence.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)


class ManuscriptEntityRepository:
    def __init__(self, db: DatabaseConnection):
        self.db = db

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _attributes(raw: Any) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _legacy_prop_row(self, row: Any) -> Dict[str, Any]:
        item = dict(row)
        attributes = self._attributes(item.get("attributes_json"))
        return {
            "id": item["id"],
            "novel_id": item["novel_id"],
            "name": item["name"],
            "description": item.get("description") or "",
            "aliases_json": item.get("aliases_json") or "[]",
            "holder_character_id": item.get("holder_character_id"),
            "first_chapter": item.get("introduced_chapter"),
            "is_key": int(bool(attributes.get("is_key", False))),
            "created_at": item.get("created_at") or "",
            "updated_at": item.get("updated_at") or "",
        }

    @staticmethod
    def _prop_columns() -> str:
        return (
            "id, novel_id, name, description, aliases_json, holder_character_id, "
            "introduced_chapter, attributes_json, created_at, updated_at"
        )

    def list_props(self, novel_id: str) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            f"SELECT {self._prop_columns()} FROM unified_props "
            "WHERE novel_id = ? ORDER BY name COLLATE NOCASE",
            (novel_id,),
        )
        return [self._legacy_prop_row(row) for row in rows]

    def create_prop(
        self,
        novel_id: str,
        *,
        name: str,
        description: str = "",
        aliases: Optional[List[str]] = None,
        holder_character_id: Optional[str] = None,
        first_chapter: Optional[int] = None,
    ) -> Dict[str, Any]:
        pid = str(uuid.uuid4())
        now = self._now()
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        self.db.execute(
            """
            INSERT INTO unified_props (
                id, novel_id, name, description, aliases_json, prop_category,
                lifecycle_state, introduced_chapter, resolved_chapter,
                holder_character_id, attributes_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'OTHER', 'DORMANT', ?, NULL, ?, '{}', ?, ?)
            """,
            (
                pid,
                novel_id,
                name.strip(),
                description or "",
                aliases_json,
                first_chapter,
                holder_character_id,
                now,
                now,
            ),
        )
        self.db.get_connection().commit()
        return self.get_prop(novel_id, pid) or {}

    def get_prop(self, novel_id: str, prop_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.fetch_one(
            f"SELECT {self._prop_columns()} FROM unified_props WHERE novel_id = ? AND id = ?",
            (novel_id, prop_id),
        )
        return self._legacy_prop_row(row) if row else None

    def update_prop(
        self,
        novel_id: str,
        prop_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        holder_character_id: Optional[str] = None,
        first_chapter: Optional[int] = None,
        is_key: Optional[bool] = None,
    ) -> None:
        cur = self.get_prop(novel_id, prop_id)
        if not cur:
            return
        now = self._now()
        nm = name if name is not None else cur["name"]
        desc = description if description is not None else cur["description"]
        aj = json.dumps(aliases, ensure_ascii=False) if aliases is not None else cur["aliases_json"]
        hc = holder_character_id if holder_character_id is not None else cur.get("holder_character_id")
        fc = first_chapter if first_chapter is not None else cur.get("first_chapter")
        ik = int(is_key) if is_key is not None else cur.get("is_key", 0)
        attributes_row = self.db.fetch_one(
            "SELECT attributes_json FROM unified_props WHERE novel_id = ? AND id = ?",
            (novel_id, prop_id),
        )
        attributes = self._attributes(dict(attributes_row).get("attributes_json") if attributes_row else None)
        attributes["is_key"] = bool(ik)
        self.db.execute(
            """
            UPDATE unified_props
            SET name = ?, description = ?, aliases_json = ?, holder_character_id = ?, introduced_chapter = ?,
                attributes_json = ?, updated_at = ?
            WHERE novel_id = ? AND id = ?
            """,
            (nm, desc, aj, hc, fc, json.dumps(attributes, ensure_ascii=False), now, novel_id, prop_id),
        )
        self.db.get_connection().commit()

    def delete_prop(self, novel_id: str, prop_id: str) -> None:
        self.db.execute("DELETE FROM unified_props WHERE novel_id = ? AND id = ?", (novel_id, prop_id))
        self.db.get_connection().commit()

    def replace_chapter_mentions(
        self,
        novel_id: str,
        chapter_number: int,
        rows: Sequence[Tuple[str, str, str, int]],
    ) -> None:
        """rows: (kind, entity_id, display_label, count)"""
        conn = self.db.get_connection()
        conn.execute(
            "DELETE FROM chapter_entity_mentions WHERE novel_id = ? AND chapter_number = ?",
            (novel_id, chapter_number),
        )
        now = self._now()
        for kind, eid, label, cnt in rows:
            conn.execute(
                """
                INSERT INTO chapter_entity_mentions (
                    novel_id, chapter_number, entity_kind, entity_id, display_label, mention_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (novel_id, chapter_number, kind, eid, label or eid, max(1, int(cnt)), now),
            )
        conn.commit()

    def list_chapter_mentions(self, novel_id: str, chapter_number: int) -> List[Dict[str, Any]]:
        rows = self.db.fetch_all(
            """
            SELECT entity_kind, entity_id, display_label, mention_count, updated_at
            FROM chapter_entity_mentions
            WHERE novel_id = ? AND chapter_number = ?
            ORDER BY mention_count DESC, display_label COLLATE NOCASE
            """,
            (novel_id, chapter_number),
        )
        return [dict(r) for r in rows]
