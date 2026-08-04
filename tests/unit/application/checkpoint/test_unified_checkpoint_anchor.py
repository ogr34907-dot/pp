from application.checkpoint.services.unified_checkpoint_service import (
    UnifiedCheckpointService,
)
from infrastructure.persistence.database.connection import DatabaseConnection


class _EmptyChapterRepository:
    def list_by_novel(self, novel_id):
        return []


def test_checkpoint_persists_anchor_from_story_state(tmp_path):
    db = DatabaseConnection(str(tmp_path / "checkpoint-anchor.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    checkpoint_id = UnifiedCheckpointService(
        db,
        _EmptyChapterRepository(),
    ).create_checkpoint(
        novel_id="novel-1",
        trigger_type="CHAPTER",
        name="第4章快照",
        story_state={"chapter": 4},
    )

    row = db.fetch_one(
        "SELECT anchor_chapter FROM novel_checkpoints WHERE id = ?",
        (checkpoint_id,),
    )

    assert row["anchor_chapter"] == 4
