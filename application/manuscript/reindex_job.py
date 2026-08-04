"""Rebuild chapter entity mention index after chapter content is saved."""
from __future__ import annotations

import logging
import hashlib
from typing import List, Tuple

from domain.novel.value_objects.novel_id import NovelId

from application.manuscript.services.entity_mention_indexer import collect_chapter_entity_rows
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.manuscript_entity_repository import ManuscriptEntityRepository
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from infrastructure.persistence.database.unified_character_repository import SqliteUnifiedCharacterRepository
from infrastructure.persistence.database.unified_prop_repository import SqliteUnifiedPropRepository

logger = logging.getLogger(__name__)


def reindex_chapter_entity_mentions(
    novel_id: str,
    chapter_number: int,
    content: str,
    expected_content_sha256: str | None = None,
    expected_content_revision: int | None = None,
) -> None:
    try:
        db = get_database()
        if expected_content_sha256 or expected_content_revision:
            source = db.fetch_one(
                "SELECT content, content_sha256, content_revision FROM chapters "
                "WHERE novel_id = ? AND number = ?",
                (novel_id, chapter_number),
            )
            actual_hash = hashlib.sha256(
                str(source["content"] if source else "").encode("utf-8")
            ).hexdigest()
            if (
                source is None
                or (expected_content_sha256 and actual_hash != expected_content_sha256)
                or (
                    expected_content_revision is not None
                    and int(source["content_revision"] or 0) != int(expected_content_revision)
                )
            ):
                logger.info(
                    "discard stale chapter entity reindex novel=%s ch=%s",
                    novel_id,
                    chapter_number,
                )
                return
        bible = SqliteBibleRepository(db).get_by_novel_id(NovelId(novel_id))

        chars: List[Tuple[str, str, List[str]]] = [
            (c.id.value, c.name, []) for c in SqliteUnifiedCharacterRepository(db).list_by_novel(novel_id)
        ]
        locs: List[Tuple[str, str, str, List[str]]] = []
        if bible:
            locs = [
                (loc.id, loc.name, getattr(loc, "location_type", None) or "other", [])
                for loc in bible.locations
            ]
        props: List[Tuple[str, str, List[str]]] = [
            (p.id.value, p.name, list(p.aliases or []))
            for p in SqliteUnifiedPropRepository(db).list_by_novel(novel_id)
        ]

        rows = collect_chapter_entity_rows(
            content,
            characters=chars,
            locations=locs,
            props=props,
        )
        ManuscriptEntityRepository(db).replace_chapter_mentions(novel_id, chapter_number, rows)
    except Exception as e:
        logger.warning(
            "reindex chapter entities failed novel=%s ch=%s: %s",
            novel_id,
            chapter_number,
            e,
        )
