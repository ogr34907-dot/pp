from types import SimpleNamespace

import pytest

from application.narrative_engine.story_phase_resolution import resolve_story_phase_payload


class _NovelService:
    def __init__(self, target_chapters):
        self._novel = SimpleNamespace(target_chapters=target_chapters)

    def get_novel(self, _novel_id):
        return self._novel


@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
def test_story_phase_requires_strict_persisted_target_before_chapter_lookup(target_chapters):
    chapter_repository = SimpleNamespace(list_by_novel=pytest.fail)
    payload = resolve_story_phase_payload(
        "novel-1",
        novel_service=_NovelService(target_chapters),
        chapter_repository=chapter_repository,
    )

    assert payload == {
        "phase": "setup",
        "progress": 0.0,
        "description": "目标章节数未设置",
        "can_advance": False,
    }
