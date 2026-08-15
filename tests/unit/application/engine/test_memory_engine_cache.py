import json
from types import SimpleNamespace

import pytest

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.memory_engine import (
    CompletedBeatItem,
    MemoryEngine,
    MemoryStateUnavailableError,
    MemoryState,
    RevealedClueItem,
)
from application.engine.services.memory_engine_settings import MemoryEngineRuntimeSettings


def _engine(*, ttl: float = 100.0, max_size: int = 2) -> MemoryEngine:
    return MemoryEngine(
        llm_service=object(),
        bible_repository=object(),
        runtime_settings=MemoryEngineRuntimeSettings(
            state_cache_ttl_seconds=ttl,
            state_cache_max_size=max_size,
        ),
    )


def test_memory_engine_state_cache_reuses_loaded_state(monkeypatch):
    engine = _engine()
    calls = []

    def load_state(novel_id: str) -> MemoryState:
        calls.append(novel_id)
        return MemoryState(novel_id=novel_id)

    monkeypatch.setattr(engine, "_load_from_db", load_state)

    first = engine._get_or_load_state("n1")
    second = engine._get_or_load_state("n1")

    assert first is second
    assert calls == ["n1"]


def test_memory_engine_state_cache_evicts_lru_entry(monkeypatch):
    engine = _engine(max_size=2)
    calls = []

    def load_state(novel_id: str) -> MemoryState:
        calls.append(novel_id)
        return MemoryState(novel_id=novel_id)

    monkeypatch.setattr(engine, "_load_from_db", load_state)

    engine._get_or_load_state("n1")
    engine._get_or_load_state("n2")
    engine._get_or_load_state("n1")
    engine._get_or_load_state("n3")

    assert calls == ["n1", "n2", "n3"]
    assert list(engine._cache) == ["n1", "n3"]
    assert "n2" not in engine._cache_loaded_at


def test_memory_engine_state_cache_expires(monkeypatch):
    engine = _engine(ttl=1.0, max_size=2)
    now = 10.0
    calls = []

    monkeypatch.setattr(
        "application.engine.services.memory_engine.time.monotonic",
        lambda: now,
    )

    def load_state(novel_id: str) -> MemoryState:
        calls.append(novel_id)
        return MemoryState(novel_id=novel_id, last_updated_chapter=len(calls))

    monkeypatch.setattr(engine, "_load_from_db", load_state)

    first = engine._get_or_load_state("n1")
    now = 10.5
    second = engine._get_or_load_state("n1")
    now = 11.1
    third = engine._get_or_load_state("n1")

    assert first is second
    assert third is not first
    assert third.last_updated_chapter == 2
    assert calls == ["n1", "n1"]


def test_memory_engine_state_cache_can_be_disabled(monkeypatch):
    engine = _engine(ttl=0.0, max_size=2)
    calls = []

    def load_state(novel_id: str) -> MemoryState:
        calls.append(novel_id)
        return MemoryState(novel_id=novel_id)

    monkeypatch.setattr(engine, "_load_from_db", load_state)

    engine._get_or_load_state("n1")
    engine._get_or_load_state("n1")

    assert calls == ["n1", "n1"]
    assert engine._cache == {}
    assert engine._cache_loaded_at == {}


def test_memory_engine_allows_an_empty_state_only_after_a_successful_db_read(tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "memory-empty.db"))
    engine = MemoryEngine(
        llm_service=object(),
        bible_repository=object(),
        db_connection=database,
    )

    assert engine.get_completed_beats_section("new-novel") == ""
    assert engine.get_revealed_clues_section("new-novel") == ""


def test_memory_engine_does_not_treat_a_database_read_failure_as_empty_history():
    class BrokenDatabase:
        def execute(self, *_args, **_kwargs):
            raise OSError("memory database is unavailable")

        def commit(self):
            raise OSError("memory database is unavailable")

    engine = MemoryEngine(
        llm_service=object(),
        bible_repository=object(),
        db_connection=BrokenDatabase(),
    )

    with pytest.raises(MemoryStateUnavailableError, match="memory_state_unavailable"):
        engine.get_completed_beats_section("novel-1")


def test_memory_engine_does_not_treat_invalid_persisted_json_as_empty_history(tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "memory-invalid-json.db"))
    engine = MemoryEngine(
        llm_service=object(),
        bible_repository=object(),
        db_connection=database,
    )
    database.execute(
        "INSERT INTO memory_engine_state "
        "(novel_id, state_json, last_updated_chapter, updated_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("novel-1", "{not valid json", 12),
    )
    database.commit()

    with pytest.raises(MemoryStateUnavailableError, match="memory_state_unavailable"):
        engine.get_revealed_clues_section("novel-1")


def test_memory_engine_loads_only_the_recent_persisted_memory_window(tmp_path):
    from infrastructure.persistence.database.connection import DatabaseConnection

    database = DatabaseConnection(str(tmp_path / "memory-window.db"))
    engine = MemoryEngine(
        llm_service=object(),
        bible_repository=object(),
        db_connection=database,
    )
    persisted_state = {
        "completed_beats": [
            {"beat_id": f"beat-{chapter}", "chapter": chapter, "summary": f"节拍-{chapter}"}
            for chapter in range(1, 506)
        ],
        "revealed_clues": [
            {
                "clue_id": f"clue-{chapter}",
                "revealed_at_chapter": chapter,
                "content": f"线索-{chapter}",
            }
            for chapter in range(1, 806)
        ],
    }
    database.execute(
        "INSERT INTO memory_engine_state "
        "(novel_id, state_json, last_updated_chapter, updated_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("novel-1", json.dumps(persisted_state), 805),
    )
    database.commit()

    state = engine._get_or_load_state("novel-1")

    assert len(state.completed_beats) == 500
    assert state.completed_beats[0]["beat_id"] == "beat-6"
    assert state.completed_beats[-1]["beat_id"] == "beat-505"
    assert len(state.revealed_clues) == 800
    assert state.revealed_clues[0]["clue_id"] == "clue-6"
    assert state.revealed_clues[-1]["clue_id"] == "clue-805"
    raw_state = json.loads(
        database.execute(
            "SELECT state_json FROM memory_engine_state WHERE novel_id = ?", ("novel-1",)
        ).fetchone()[0]
    )
    assert len(raw_state["completed_beats"]) == 505
    assert len(raw_state["revealed_clues"]) == 805


def test_completed_beats_render_recent_entries_within_the_context_slot_budget():
    engine = _engine()
    state = MemoryState(
        novel_id="n1",
        completed_beats=[
            {
                "beat_id": f"beat-{chapter}",
                "chapter": chapter,
                "summary": f"节拍-{chapter}-" + "甲" * 60,
            }
            for chapter in range(1, 81)
        ],
    )
    engine._remember_state("n1", state)

    rendered = engine.get_completed_beats_section("n1")

    assert "[第80章] 节拍-80" in rendered
    assert "[第1章] 节拍-1-" not in rendered
    assert "不要重新展开写" in rendered
    assert ContextBudgetAllocator().estimate_tokens(rendered) <= 1000


def test_revealed_clues_render_recent_valid_entries_within_the_context_slot_budget():
    engine = _engine()
    state = MemoryState(
        novel_id="n1",
        revealed_clues=[
            {
                "clue_id": f"clue-{chapter}",
                "revealed_at_chapter": chapter,
                "content": f"线索-{chapter}-" + "乙" * 60,
                "category": "truth",
                "is_still_valid": True,
            }
            for chapter in range(1, 81)
        ],
    )
    engine._remember_state("n1", state)

    rendered = engine.get_revealed_clues_section("n1")

    assert "[第80章] 线索-80" in rendered
    assert "[第1章] 线索-1-" not in rendered
    assert "不要再把它们当作'新发现'" in rendered
    assert ContextBudgetAllocator().estimate_tokens(rendered) <= 800


def test_memory_state_discards_old_entries_after_soft_storage_limits():
    engine = _engine()
    state = MemoryState(novel_id="n1")

    engine._merge_beats(
        state,
        [
            CompletedBeatItem(
                beat_id=f"beat-{chapter}",
                summary=f"节拍-{chapter}",
                chapter=chapter,
            )
            for chapter in range(1, 506)
        ],
        chapter=505,
    )
    engine._merge_clues(
        state,
        [
            RevealedClueItem(
                clue_id=f"clue-{chapter}",
                content=f"线索-{chapter}",
                revealed_at_chapter=chapter,
            )
            for chapter in range(1, 806)
        ],
        chapter=805,
    )

    assert len(state.completed_beats) == 500
    assert state.completed_beats[0]["beat_id"] == "beat-6"
    assert state.completed_beats[-1]["beat_id"] == "beat-505"
    assert len(state.revealed_clues) == 800
    assert state.revealed_clues[0]["clue_id"] == "clue-6"
    assert state.revealed_clues[-1]["clue_id"] == "clue-805"


@pytest.mark.asyncio
async def test_memory_engine_skips_items_without_final_affirmative_evidence(monkeypatch):
    class BibleRepository:
        def get_by_novel_id(self, novel_id):
            return None

    class LLMService:
        async def generate(self, prompt, config):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "completed_beats": [
                            {
                                "beat_id": "missing-evidence",
                                "summary": "林澈杀死反派甲",
                                "chapter": 1,
                            },
                            {
                                "beat_id": "forged-evidence",
                                "summary": "林澈得到密钥",
                                "chapter": 1,
                                "evidence_text": "正文中不存在的密钥",
                            },
                            {
                                "beat_id": "negated-evidence",
                                "summary": "林澈杀死反派甲",
                                "chapter": 1,
                                "evidence_text": "林澈没有杀死反派甲。",
                            },
                            {
                                "beat_id": "unsupported-evidence",
                                "summary": "林澈杀死反派甲",
                                "chapter": 1,
                                "evidence_text": "林澈站在城门前。",
                            },
                            {
                                "beat_id": "verified-beat",
                                "summary": "林澈在城门前交出铜铃",
                                "chapter": 1,
                                "evidence_text": "林澈在城门前交出铜铃。",
                            },
                        ],
                        "revealed_clues": [
                            {
                                "clue_id": "missing-evidence",
                                "content": "反派甲已经死亡",
                                "revealed_at_chapter": 1,
                            },
                            {
                                "clue_id": "forged-evidence",
                                "content": "密钥藏在塔顶",
                                "revealed_at_chapter": 1,
                                "evidence_text": "正文中不存在的密钥",
                            },
                            {
                                "clue_id": "negated-evidence",
                                "content": "反派甲已经死亡",
                                "revealed_at_chapter": 1,
                                "evidence_text": "林澈没有杀死反派甲。",
                            },
                            {
                                "clue_id": "unsupported-evidence",
                                "content": "反派甲已经死亡",
                                "revealed_at_chapter": 1,
                                "evidence_text": "林澈站在城门前。",
                            },
                            {
                                "clue_id": "verified-clue",
                                "content": "铜铃能打开城门",
                                "revealed_at_chapter": 1,
                                "evidence_text": "守卫说：铜铃能打开城门。",
                            },
                        ],
                        "fact_violations": [],
                    }
                )
            )

    monkeypatch.setattr(
        "application.engine.services.memory_engine.get_prompt_gateway",
        lambda: SimpleNamespace(
            render=lambda *args, **kwargs: SimpleNamespace(prompt="memory prompt")
        ),
    )
    engine = MemoryEngine(LLMService(), BibleRepository())

    result = await engine.update_from_chapter(
        "n1",
        1,
        "林澈没有杀死反派甲。林澈在城门前交出铜铃。守卫说：铜铃能打开城门。",
        "大纲",
    )

    state = engine._get_or_load_state("n1")
    assert [beat["beat_id"] for beat in state.completed_beats] == ["verified-beat"]
    assert [clue["clue_id"] for clue in state.revealed_clues] == ["verified-clue"]
    assert result["unverified_beats"] == 4
    assert result["unverified_clues"] == 4


def test_memory_engine_evidence_rejects_an_unrelated_final_quote():
    beats, skipped_beats = MemoryEngine._verified_memory_items(
        [
            CompletedBeatItem(
                beat_id="unsupported-beat",
                summary="林澈杀死反派甲",
                chapter=1,
                evidence_text="林澈站在城门前。",
            )
        ],
        "林澈站在城门前。",
    )
    clues, skipped_clues = MemoryEngine._verified_memory_items(
        [
            RevealedClueItem(
                clue_id="unsupported-clue",
                content="反派甲已经死亡",
                revealed_at_chapter=1,
                evidence_text="林澈站在城门前。",
            )
        ],
        "林澈站在城门前。",
    )

    assert beats == []
    assert clues == []
    assert skipped_beats == 1
    assert skipped_clues == 1


@pytest.mark.asyncio
async def test_memory_engine_reports_persistence_failure_without_caching_uncommitted_state(
    monkeypatch,
):
    class FailingDatabase:
        def execute(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def commit(self):
            raise RuntimeError("disk full")

    class BibleRepository:
        def get_by_novel_id(self, novel_id):
            return None

    class LLMService:
        async def generate(self, prompt, config):
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "completed_beats": [
                            {
                                "beat_id": "ch1-arrival",
                                "summary": "主角抵达城门",
                                "chapter": 1,
                            }
                        ],
                        "revealed_clues": [],
                        "fact_violations": [],
                    }
                )
            )

    monkeypatch.setattr(
        "application.engine.services.memory_engine.get_prompt_gateway",
        lambda: SimpleNamespace(
            render=lambda *args, **kwargs: SimpleNamespace(prompt="memory prompt")
        ),
    )
    engine = MemoryEngine(
        llm_service=LLMService(),
        bible_repository=BibleRepository(),
        db_connection=FailingDatabase(),
    )

    result = await engine.update_from_chapter("n1", 1, "正文", "大纲")

    assert result["errors"]
    cached = engine._get_cached_state("n1")
    assert cached is None
