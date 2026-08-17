import pytest

from application.blueprint.services.manifest_planning_service import (
    ManifestPlanningService,
)
from domain.structure.outline_contract import OutlinePayload, OutlineSource
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


@pytest.fixture
def db(tmp_path):
    database = DatabaseConnection(str(tmp_path / "manifest-cutover.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Cutover Novel", "cutover-novel", 20),
    )
    conn.commit()
    return database


@pytest.fixture
def test_novel_id():
    return "novel-1"


def test_ensure_manifest_planning_authority_uses_backfill_and_projection_cutover(
    db, test_novel_id
):
    repository = OutlineContractRepository(db)
    root = repository.ensure_root(test_novel_id)
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="主线",
            creative_goal="完成目标",
            entry_state="开始",
            exit_state="结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(
        root.id, expected_revision=draft.draft.revision, idempotency_key="cutover-root"
    )

    head = ManifestPlanningService(db).ensure_manifest_planning_authority(test_novel_id)

    assert head.authority_mode.value == "manifest"
    assert head.active_plan_revision_id
    assert head.authority_generation == 1
    assert head.projection_generation == 1
