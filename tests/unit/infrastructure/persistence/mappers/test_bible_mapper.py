from copy import deepcopy

from application.world.dtos.bible_dto import CharacterDTO
from domain.bible.entities.bible import Bible
from domain.bible.entities.character import Character
from domain.bible.value_objects.character_id import CharacterId
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.mappers.bible_mapper import BibleMapper


def test_bible_character_death_state_round_trips_with_a_legacy_default():
    bible = Bible(id="bible-1", novel_id=NovelId("novel-1"))
    character = Character(
        id=CharacterId("character-1"),
        name="已故角色",
        description="状态由结构化字段声明",
        is_dead=True,
    )
    bible.add_character(character)

    dto = CharacterDTO.from_domain(character)
    payload = BibleMapper.to_dict(bible)
    restored = BibleMapper.from_dict(payload)
    legacy_payload = deepcopy(payload)
    legacy_payload["characters"][0].pop("is_dead")
    legacy_payload["characters"][0]["status"] = "dead"
    restored_legacy = BibleMapper.from_dict(legacy_payload)

    assert dto.is_dead is True
    assert payload["characters"][0]["is_dead"] is True
    assert restored.characters[0].is_dead is True
    assert restored_legacy.characters[0].is_dead is False
    assert restored_legacy.characters[0].status == "dead"
