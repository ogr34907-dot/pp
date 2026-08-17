"""Manifest-native sibling cohort generation keeps LLM output draft-only."""

from types import SimpleNamespace
import json
import sqlite3

import pytest

from application.blueprint.services.outline_cohort_generation_service import (
    OutlineCohortGenerationError,
    OutlineCohortGenerationService,
)
from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.novel.candidate_chapter import GenerationRunState, RunMode
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class _LLM:
    def __init__(self, content: str):
        self.content = content
        self.prompts = []

    async def generate(self, prompt, _config):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.content)


class _PlanRacingLLM(_LLM):
    def __init__(self, content: str, database: DatabaseConnection, plan_id: str):
        super().__init__(content)
        self.database = database
        self.plan_id = plan_id

    async def generate(self, prompt, config):
        response = await super().generate(prompt, config)
        conn = self.database.get_connection()
        conn.execute(
            "UPDATE outline_plan_revisions SET digest = 'concurrent-plan-digest' "
            "WHERE id = ?",
            (self.plan_id,),
        )
        conn.commit()
        return response


class _NovelContextRacingLLM(_LLM):
    def __init__(self, content: str, database: DatabaseConnection):
        super().__init__(content)
        self.database = database

    async def generate(self, prompt, config):
        response = await super().generate(prompt, config)
        conn = self.database.get_connection()
        conn.execute(
            "UPDATE novels SET premise = ? WHERE id = 'novel-1'",
            ("The author changed the novel premise while the cohort was generating.",),
        )
        conn.commit()
        return response


def _root_item(database, repository):
    root = repository.ensure_root("novel-1")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="主角在代价中重建秩序。",
            creative_goal="完成核心选择",
            entry_state="旧秩序仍然完整",
            exit_state="新秩序建立但付出代价",
            chapter_start=1,
            chapter_end=10,
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    active = repository.backfill_initial_plan("novel-1").plan
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()
    draft_plan = repository.clone_active_plan_draft(
        "novel-1", author_intent="让主角选择承担代价"
    )
    return active, draft_plan, draft_plan.items[0]


async def _published_manifest_cohort(tmp_path, *, slug: str):
    database = DatabaseConnection(str(tmp_path / f"{slug}.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', ?, 10)",
        (slug,),
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    nodes = StoryNodeRepository(database)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(contract_repository=repository, story_node_repository=nodes),
        _LLM(
            '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
            '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
            '"exit_state":"新秩序建立但付出代价", "conflicts":["仇家追击"],'
            '"state_changes":{"主角":[{"change":"失去依靠"}]},'
            '"handoff_conditions":["后续承接"], "chapter_start":1,"chapter_end":10}]'
        ),
        database,
    )
    candidates = ChapterCandidateRepository(database)
    candidates.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=10)
    candidates.wait_for_outline_expansion("novel-1")
    generated = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )
    published = await service.publish_completed_cohort(
        attempt_id=generated["attempt"]["id"]
    )
    return database, repository, service, active, generated, published


def test_open_or_clone_cohort_draft_refreshes_the_current_formal_boundary(
    tmp_path, monkeypatch
):
    database = DatabaseConnection(str(tmp_path / "cohort-service-current-boundary.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Novel', 'cohort-service-current-boundary', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, draft, root_item = _root_item(database, repository)
    database.execute(
        "UPDATE outline_planning_heads SET working_plan_revision_id = NULL "
        "WHERE novel_id = 'novel-1'"
    )
    database.get_connection().commit()
    contract_service = OutlineContractService(
        contract_repository=repository,
        story_node_repository=StoryNodeRepository(database),
    )
    service = OutlineCohortGenerationService(
        repository, contract_service, _LLM("[]"), database
    )
    monkeypatch.setattr(
        "application.blueprint.services.outline_cohort_generation_service.ChapterCandidateRepository.formal_history_snapshot",
        lambda _self, _novel_id, **_kwargs: (3, ()),
    )
    monkeypatch.setattr(
        contract_service,
        "compute_canonical_prefix",
        lambda _novel_id, through, **_kwargs: SimpleNamespace(
            formal_head=through,
            digest="current-prefix-digest",
            ready=True,
            blockers=(),
        ),
    )

    opened = service.open_or_clone_cohort_draft("novel-1")

    assert opened.id != draft.id
    assert opened.canonical_prefix_digest == "current-prefix-digest"
    assert opened.canonical_boundary == {"formal_head": 3}
    assert opened.items == (root_item,)


def test_open_or_clone_cohort_draft_retires_a_stale_pristine_clone(
    tmp_path, monkeypatch
):
    database = DatabaseConnection(str(tmp_path / "cohort-service-stale-pristine.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Novel', 'cohort-service-stale-pristine', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, initial_draft, _ = _root_item(database, repository)
    database.execute(
        "UPDATE outline_planning_heads SET working_plan_revision_id = NULL "
        "WHERE novel_id = 'novel-1'"
    )
    database.get_connection().commit()
    stale = repository.clone_active_plan_draft(
        "novel-1",
        canonical_prefix_digest="old-prefix-digest",
        canonical_boundary={"formal_head": 0},
    )
    contract_service = OutlineContractService(
        contract_repository=repository,
        story_node_repository=StoryNodeRepository(database),
    )
    service = OutlineCohortGenerationService(
        repository, contract_service, _LLM("[]"), database
    )

    def current_prefix(_novel_id, through, *, connection=None):
        assert connection is database.get_connection()
        assert connection.in_transaction
        return SimpleNamespace(
            formal_head=through,
            digest="current-prefix-digest",
            ready=True,
            blockers=(),
        )

    monkeypatch.setattr(
        "application.blueprint.services.outline_cohort_generation_service.ChapterCandidateRepository.formal_history_snapshot",
        lambda _self, _novel_id, **_kwargs: (2, ()),
    )
    monkeypatch.setattr(
        contract_service,
        "compute_canonical_prefix",
        current_prefix,
    )

    opened = service.open_or_clone_cohort_draft("novel-1")

    assert opened.id not in {initial_draft.id, stale.id}
    assert opened.canonical_prefix_digest == "current-prefix-digest"
    assert opened.canonical_boundary == {"formal_head": 2}
    assert repository.get_plan_revision(stale.id).status.value == "stale"
    assert repository.get_planning_head("novel-1").working_plan_revision_id == opened.id


def test_open_or_clone_cohort_draft_preserves_a_stale_authored_draft(
    tmp_path, monkeypatch
):
    database = DatabaseConnection(str(tmp_path / "cohort-service-stale-authored.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Novel', 'cohort-service-stale-authored', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, stale, _ = _root_item(database, repository)
    contract_service = OutlineContractService(
        contract_repository=repository,
        story_node_repository=StoryNodeRepository(database),
    )
    service = OutlineCohortGenerationService(
        repository, contract_service, _LLM("[]"), database
    )
    monkeypatch.setattr(
        "application.blueprint.services.outline_cohort_generation_service.ChapterCandidateRepository.formal_history_snapshot",
        lambda _self, _novel_id, **_kwargs: (2, ()),
    )
    monkeypatch.setattr(
        contract_service,
        "compute_canonical_prefix",
        lambda _novel_id, through, **_kwargs: SimpleNamespace(
            formal_head=through,
            digest="current-prefix-digest",
            ready=True,
            blockers=(),
        ),
    )

    with pytest.raises(OutlineCohortGenerationError, match="explicit recovery"):
        service.open_or_clone_cohort_draft("novel-1")

    preserved = repository.get_plan_revision(stale.id)
    assert preserved.status.value == "draft"
    assert preserved.author_intent == "让主角选择承担代价"
    assert repository.get_planning_head("novel-1").working_plan_revision_id == stale.id


@pytest.mark.asyncio
async def test_generate_manifest_cohort_persists_attempt_then_replaces_draft(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service', 10)"
    )
    database.execute("INSERT INTO bibles (id, novel_id) VALUES ('bible-1', 'novel-1')")
    database.execute(
        "INSERT INTO bible_world_settings (id, novel_id, name, description) VALUES "
        "('world-1', 'novel-1', '世界规则', '力量必须付出代价')"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    contract_service = OutlineContractService(
        contract_repository=repository,
        story_node_repository=StoryNodeRepository(database),
    )
    llm = _LLM(
        '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
        '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"主角失去依靠", "conflicts":["仇家追击"],'
        '"state_changes":{"主角":[{"change":"失去依靠"}]},'
        '"handoff_conditions":["第二部承接追击"], "chapter_start":1,"chapter_end":5},'
        '{"title":"第二部","narrative_text":"主角反击并付出代价。",'
        '"creative_goal":"完成核心选择", "entry_state":"主角失去依靠",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["反击失败风险"],'
        '"state_changes":{"主角":[{"change":"承担代价"}]},'
        '"handoff_conditions":["后续承接新秩序"], "chapter_start":6,"chapter_end":10}]'
    )
    service = OutlineCohortGenerationService(repository, contract_service, llm, database)

    result = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )

    assert result["attempt"]["status"] == "completed"
    assert result["plan"].id == draft.id
    assert [item.level for item in result["plan"].items] == [
        OutlineLevel.OUTLINE,
        OutlineLevel.PART,
        OutlineLevel.PART,
    ]
    assert repository.get_planning_head("novel-1").active_plan_revision_id == active.id
    assert "世界规则" in llm.prompts[0].user
    assert "让主角选择承担代价" in llm.prompts[0].user
    assert "canonical_boundary" in llm.prompts[0].user


@pytest.mark.asyncio
async def test_invalid_manifest_cohort_response_marks_attempt_failed_without_plan_mutation(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-invalid.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-invalid', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        _LLM('{"not":"a cohort"}'),
        database,
    )

    with pytest.raises(OutlineCohortGenerationError, match="JSON"):
        await service.generate_cohort(
            plan_revision_id=draft.id,
            parent_logical_node_id=root_item.logical_node_id,
            level=OutlineLevel.PART,
        )

    row = database.get_connection().execute(
        "SELECT id FROM outline_plan_cohort_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    attempt = repository.get_manifest_cohort_attempt(str(row["id"]))
    assert attempt["status"] == "failed"
    assert repository.get_plan_revision(draft.id).items == draft.items
    assert repository.get_planning_head("novel-1").active_plan_revision_id == active.id


@pytest.mark.asyncio
async def test_cohort_payload_and_attempt_completion_roll_back_together(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-atomic-complete.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Novel', 'cohort-service-atomic-complete', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, draft, root_item = _root_item(database, repository)
    conn = database.get_connection()
    conn.execute(
        """
        CREATE TRIGGER fail_cohort_attempt_completion
        BEFORE UPDATE OF status ON outline_plan_cohort_attempts
        WHEN NEW.status = 'completed'
        BEGIN
            SELECT RAISE(ABORT, 'forced cohort completion failure');
        END
        """
    )
    conn.commit()
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        _LLM(
            '[{"title":"Part one","narrative_text":"The protagonist leaves home.",'
            '"creative_goal":"Force a choice","entry_state":"Old order",'
            '"exit_state":"New order","conflicts":["Pursuit"],'
            '"state_changes":{"hero":[{"change":"leaves"}]},'
            '"handoff_conditions":["continue"],"chapter_start":1,"chapter_end":10}]'
        ),
        database,
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced cohort completion failure"):
        await service.generate_cohort(
            plan_revision_id=draft.id,
            parent_logical_node_id=root_item.logical_node_id,
            level=OutlineLevel.PART,
        )

    current = repository.get_plan_revision(draft.id)
    assert current.items == draft.items
    row = conn.execute(
        "SELECT id FROM outline_plan_cohort_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert repository.get_manifest_cohort_attempt(str(row["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_cohort_result_rejects_a_plan_digest_changed_while_the_llm_was_running(
    tmp_path,
):
    database = DatabaseConnection(str(tmp_path / "cohort-service-context-race.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Novel', 'cohort-service-context-race', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, draft, root_item = _root_item(database, repository)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        _PlanRacingLLM(
            '[{"title":"Part one","narrative_text":"The protagonist leaves home.",'
            '"creative_goal":"Force a choice","entry_state":"Old order",'
            '"exit_state":"New order","conflicts":["Pursuit"],'
            '"state_changes":{"hero":[{"change":"leaves"}]},'
            '"handoff_conditions":["continue"],"chapter_start":1,"chapter_end":10}]',
            database,
            draft.id,
        ),
        database,
    )

    with pytest.raises(Exception, match="plan digest changed"):
        await service.generate_cohort(
            plan_revision_id=draft.id,
            parent_logical_node_id=root_item.logical_node_id,
            level=OutlineLevel.PART,
        )

    current = repository.get_plan_revision(draft.id)
    assert current.items == draft.items
    row = database.get_connection().execute(
        "SELECT id FROM outline_plan_cohort_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert repository.get_manifest_cohort_attempt(str(row["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_cohort_result_rejects_a_prompt_premise_changed_while_the_llm_was_running(
    tmp_path,
):
    """A mutable novel input in the prompt invalidates an in-flight response."""

    database = DatabaseConnection(str(tmp_path / "cohort-service-novel-context-race.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters, premise) VALUES "
        "('novel-1', 'Novel', 'cohort-service-novel-context-race', 10, 'Original premise')"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, draft, root_item = _root_item(database, repository)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        _NovelContextRacingLLM(
            '[{"title":"Part one","narrative_text":"The protagonist leaves home.",'
            '"creative_goal":"Force a choice","entry_state":"Old order",'
            '"exit_state":"New order","conflicts":["Pursuit"],'
            '"state_changes":{"hero":[{"change":"leaves"}]},'
            '"handoff_conditions":["continue"],"chapter_start":1,"chapter_end":10}]',
            database,
        ),
        database,
    )

    with pytest.raises(Exception, match="context"):
        await service.generate_cohort(
            plan_revision_id=draft.id,
            parent_logical_node_id=root_item.logical_node_id,
            level=OutlineLevel.PART,
        )

    assert repository.get_plan_revision(draft.id).items == draft.items
    row = database.get_connection().execute(
        "SELECT id FROM outline_plan_cohort_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert repository.get_manifest_cohort_attempt(str(row["id"]))["status"] == "failed"


@pytest.mark.asyncio
async def test_cohort_generation_never_overwrites_author_locked_fields(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-author-lock.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-author-lock', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    _, draft, root_item = _root_item(database, repository)
    author_payload = OutlinePayload.from_dict(
        {
            "title": "作者第一部",
            "narrative_text": "作者逐字写下的第一部梗概。",
            "extra": {
                "_field_provenance": {
                    "title": {"source": "author", "locked": True},
                    "narrative_text": {"source": "author", "locked": True},
                }
            },
        }
    )
    llm = _LLM(
        '[{"title":"AI 第一部", "narrative_text":"AI 改写",'
        '"creative_goal":"AI 补全目标", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["冲突"],'
        '"state_changes":{"主角":[{"change":"代价"}]},'
        '"handoff_conditions":["承接"], "chapter_start":1,"chapter_end":10}]'
    )
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        llm,
        database,
    )

    result = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
        author_payloads=(author_payload,),
    )

    child = next(item for item in result["plan"].items if item.level == OutlineLevel.PART)
    row = database.get_connection().execute(
        "SELECT payload_json FROM outline_contract_versions WHERE id = ?", (child.version_id,)
    ).fetchone()
    payload = OutlinePayload.from_dict(__import__("json").loads(row["payload_json"]))
    assert payload.title == "作者第一部"
    assert payload.narrative_text == "作者逐字写下的第一部梗概。"
    assert payload.creative_goal == "AI 补全目标"
    assert result["author_conflicts"] == ((0, ("title", "narrative_text")),)


@pytest.mark.asyncio
async def test_publish_completed_cohort_seals_projects_and_activates_the_draft(tmp_path):
    """Publishing a completed attempt is binding-driven and performs no new LLM call."""

    database = DatabaseConnection(str(tmp_path / "cohort-service-publish.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-publish', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    llm = _LLM(
        '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
        '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["仇家追击"],'
        '"state_changes":{"主角":[{"change":"失去依靠"}]},'
        '"handoff_conditions":["后续承接"], "chapter_start":1,"chapter_end":10}]'
    )
    nodes = StoryNodeRepository(database)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(contract_repository=repository, story_node_repository=nodes),
        llm,
        database,
    )
    candidates = ChapterCandidateRepository(database)
    candidates.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=10)
    paused = candidates.wait_for_outline_expansion("novel-1")
    assert paused.state == GenerationRunState.WAITING_PLANNING

    generated = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )
    published = await service.publish_completed_cohort(
        attempt_id=generated["attempt"]["id"]
    )
    replayed = await service.publish_completed_cohort(
        attempt_id=generated["attempt"]["id"]
    )

    head = repository.get_planning_head("novel-1")
    assert published["plan"].id == head.active_plan_revision_id
    assert published["plan"].id != active.id
    assert head.active_plan_digest == published["plan"].digest
    assert head.working_plan_revision_id is None
    assert published["run"] is not None
    assert published["run"].state == GenerationRunState.RUNNING
    assert replayed["plan"].id == published["plan"].id
    assert replayed["reconciliation"].status == published["reconciliation"].status
    assert replayed["run"] is not None
    assert replayed["run"].state == GenerationRunState.RUNNING
    assert len(llm.prompts) == 1
    part = next(item for item in published["plan"].items if item.level == OutlineLevel.PART)
    binding = next(
        row
        for row in repository.projection_bindings_for_revision(published["plan"].id)
        if row["logical_node_id"] == part.logical_node_id
    )
    node = await nodes.get_by_id(binding["story_node_id"])
    assert node is not None and node.title == "第一部"


@pytest.mark.asyncio
async def test_publish_completed_cohort_replay_rejects_a_moved_manifest_head(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-publish-head-moved.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-publish-head-moved', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    llm = _LLM(
        '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
        '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["仇家追击"],'
        '"state_changes":{"主角":[{"change":"失去依靠"}]},'
        '"handoff_conditions":["后续承接"], "chapter_start":1,"chapter_end":10}]'
    )
    nodes = StoryNodeRepository(database)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(contract_repository=repository, story_node_repository=nodes),
        llm,
        database,
    )
    candidates = ChapterCandidateRepository(database)
    candidates.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=10)
    candidates.wait_for_outline_expansion("novel-1")
    generated = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )
    await service.publish_completed_cohort(attempt_id=generated["attempt"]["id"])

    database.execute(
        "UPDATE outline_planning_heads SET active_plan_revision_id = ?, "
        "active_plan_digest = ?, working_plan_revision_id = NULL "
        "WHERE novel_id = 'novel-1'",
        (active.id, active.digest),
    )
    database.get_connection().commit()

    with pytest.raises(OutlineCohortGenerationError, match="manifest working revision"):
        await service.publish_completed_cohort(attempt_id=generated["attempt"]["id"])


@pytest.mark.asyncio
async def test_publish_completed_cohort_replay_returns_the_current_advanced_run(
    tmp_path,
):
    database, _, service, _, generated, _ = await _published_manifest_cohort(
        tmp_path, slug="cohort-service-publish-replay-advanced-run"
    )
    database.execute(
        """
        UPDATE novel_generation_runs
        SET state = 'waiting_review', next_action = 'review_candidate',
            current_candidate_id = 'candidate-1', current_candidate_chapter = 1
        WHERE novel_id = 'novel-1'
        """
    )
    database.get_connection().commit()

    replayed = await service.publish_completed_cohort(
        attempt_id=generated["attempt"]["id"]
    )

    assert replayed["run"] is not None
    assert replayed["run"].state == GenerationRunState.WAITING_REVIEW
    assert replayed["run"].current_candidate_id == "candidate-1"


@pytest.mark.asyncio
async def test_publish_completed_cohort_replay_rejects_an_advanced_head_generation(
    tmp_path,
):
    database, _, service, _, generated, _ = await _published_manifest_cohort(
        tmp_path, slug="cohort-service-publish-replay-head-generation"
    )
    database.execute(
        """
        UPDATE outline_planning_heads
        SET authority_generation = authority_generation + 2,
            projection_generation = projection_generation + 2
        WHERE novel_id = 'novel-1'
        """
    )
    database.get_connection().commit()

    with pytest.raises(OutlineCohortGenerationError, match="manifest working revision"):
        await service.publish_completed_cohort(attempt_id=generated["attempt"]["id"])


@pytest.mark.asyncio
async def test_publish_completed_cohort_replay_rejects_malformed_stored_reconciliation(
    tmp_path,
):
    database, _, service, _, generated, published = await _published_manifest_cohort(
        tmp_path, slug="cohort-service-publish-replay-malformed-reconciliation"
    )
    conn = database.get_connection()
    conn.execute("DROP TRIGGER trg_outline_plan_revisions_sealed_update")
    row = conn.execute(
        "SELECT reconciliation_report_json FROM outline_plan_revisions WHERE id = ?",
        (published["plan"].id,),
    ).fetchone()
    payload = json.loads(str(row["reconciliation_report_json"]))
    payload["canonical_ready"] = "false"
    conn.execute(
        "UPDATE outline_plan_revisions SET reconciliation_report_json = ? WHERE id = ?",
        (json.dumps(payload), published["plan"].id),
    )
    conn.commit()

    with pytest.raises(OutlineCohortGenerationError, match="valid stored reconciliation"):
        await service.publish_completed_cohort(attempt_id=generated["attempt"]["id"])


@pytest.mark.asyncio
async def test_publish_completed_cohort_requires_waiting_run_before_sealing(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-no-waiting-run.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-no-waiting-run', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    llm = _LLM(
        '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
        '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["仇家追击"],'
        '"state_changes":{"主角":[{"change":"失去依靠"}]},'
        '"handoff_conditions":["后续承接"], "chapter_start":1,"chapter_end":10}]'
    )
    nodes = StoryNodeRepository(database)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(contract_repository=repository, story_node_repository=nodes),
        llm,
        database,
    )
    generated = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )

    with pytest.raises(OutlineCohortGenerationError, match="waiting planning run"):
        await service.publish_completed_cohort(attempt_id=generated["attempt"]["id"])

    head = repository.get_planning_head("novel-1")
    assert head.active_plan_revision_id == active.id
    assert head.working_plan_revision_id == draft.id
    assert repository.get_plan_revision(draft.id).sealed_at is None
    assert not [
        node
        for node in nodes.get_by_novel_sync("novel-1")
        if node.node_type.value == "part"
    ]
