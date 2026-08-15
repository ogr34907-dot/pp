"""Load the setup-guide chapter target from the Novel aggregate."""

from domain.novel.target_chapters import positive_integer_or_none
from domain.novel.value_objects.novel_id import NovelId


def load_persisted_target_chapters(novel_id: str) -> int:
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_novel_repository import (
        SqliteNovelRepository,
    )

    novel = SqliteNovelRepository(get_database()).get_by_id(NovelId(novel_id))
    target_chapters = positive_integer_or_none(
        getattr(novel, "target_chapters", None)
    )
    if target_chapters is None:
        raise ValueError("novels.target_chapters must be a positive integer")
    return target_chapters
