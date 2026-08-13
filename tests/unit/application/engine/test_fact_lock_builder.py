from types import SimpleNamespace

import pytest

from application.engine.services.memory_engine import FactLockBuilder


def _character(name, description="", relationships=None, **attributes):
    return SimpleNamespace(
        name=name,
        description=description,
        relationships=relationships or [],
        public_profile="",
        **attributes,
    )


def test_fact_lock_only_marks_explicit_or_clear_personal_death_as_dead():
    characters = [
        _character("目击者", "亲眼目睹父亲死亡"),
        _character("具名目击者", "具名目击者亲眼目睹父亲已故"),
        _character("献身者", "为目标不惜牺牲一切"),
        _character("幸存者", "曾被杀手追杀"),
        _character("遇害者", "三年前已经遇害身亡"),
        _character("状态死亡者", "仍有未完心愿", status="dead"),
        _character("显式死亡者", "仍有未完心愿", is_dead=True),
        _character("受威胁者", "死亡威胁仍在", status="alive"),
    ]

    dead_characters = FactLockBuilder(object())._extract_dead_characters(characters)

    assert {item["name"] for item in dead_characters} == {
        "遇害者",
        "状态死亡者",
        "显式死亡者",
    }


def test_fact_lock_does_not_treat_a_bible_read_failure_as_an_empty_bible():
    class BrokenBibleRepository:
        def get_by_novel_id(self, _novel_id):
            raise OSError("bible database is unavailable")

    builder = FactLockBuilder(BrokenBibleRepository())

    with pytest.raises(RuntimeError, match="fact_lock_unavailable: bible database is unavailable"):
        builder.build("novel-1", current_chapter=2)
