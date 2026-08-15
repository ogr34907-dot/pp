"""Persistence behavior for immutable book-level outline manifests."""

import sqlite3

import pytest

from domain.structure.outline_contract import OutlinePayload, OutlineSource
from domain.structure.outline_plan import (
    BackfillStatus,
    OutlinePlanItem,
    PlanReconciliationStatus,
    PlanRevisionStatus,
    PlanningAuthorityMode,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineGateError,
    OutlineContractRepository,
)
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)


@pytest.fixture
def plan_repo(tmp_path):
    database = DatabaseConnection(str(tmp_path / "outline-plan.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Manifest Novel", "manifest-novel", 80),
    )
    conn.commit()
    return database, OutlineContractRepository(database)


def _published_root(
    database: DatabaseConnection,
    repository: OutlineContractRepository,
    *,
    title: str = "总纲",
) -> OutlinePlanItem:
    root = repository.ensure_root("novel-1")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title=title,
            narrative_text="主角在六个阶段中以持续代价追寻目标。",
            creative_goal="完成全书不可逆的核心选择",
            entry_state="旧秩序仍然完整",
            exit_state="新秩序建立但付出代价",
        ),
        source=OutlineSource.AUTHOR,
    )
    published = repository.publish_and_sync(
        root.id,
        expected_revision=draft.draft.revision,
        idempotency_key=f"publish-{title}",
    )
    conn = database.get_connection()
    row = conn.execute(
        """
        SELECT contract.active_version_id AS version_id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version
          ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
        (published.id,),
    ).fetchone()
    return OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(row["version_id"]),
        version_digest=str(row["digest"]),
        level=published.level,
        sibling_index=0,
        expansion_state="unexpanded",
    )


def _cut_over_to_manifest(
    database: DatabaseConnection,
    plan_id: str,
    plan_digest: str,
) -> None:
    """Switch the Head through a separate SQLite connection."""

    competing_database = DatabaseConnection(database.db_path)
    competing_connection = competing_database.get_connection()
    try:
        competing_connection.execute(
            """
            UPDATE outline_planning_heads
            SET authority_mode = 'manifest', authority_generation = 1,
                projection_generation = 1, active_plan_revision_id = ?,
                active_plan_digest = ?
            WHERE novel_id = 'novel-1'
            """,
            (plan_id, plan_digest),
        )
        competing_connection.commit()
    finally:
        competing_connection.close()


def test_legacy_save_draft_rechecks_head_after_racing_manifest_cutover(
    plan_repo, monkeypatch
):
    """A preflight-only legacy guard cannot authorize later planning DML."""

    database, repository = plan_repo
    item = _published_root(database, repository)
    plan = repository.backfill_initial_plan("novel-1").plan
    assert plan is not None
    root = repository.ensure_root("novel-1")
    conn = database.get_connection()
    before_versions = conn.execute(
        "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
        (root.id,),
    ).fetchone()[0]

    original = repository._assert_legacy_mutation
    switched = False

    def switch_after_preflight(novel_id: str, operation: str) -> None:
        nonlocal switched
        original(novel_id, operation)
        if operation == "save_draft" and not switched:
            _cut_over_to_manifest(database, plan.id, plan.digest)
            switched = True

    monkeypatch.setattr(repository, "_assert_legacy_mutation", switch_after_preflight)

    with pytest.raises(PlanningAuthorityError, match="requires PlanRevision transaction"):
        repository.save_draft(
            root.id,
            OutlinePayload(
                title="不应落盘的旧草稿",
                narrative_text="并发切换后不允许旧规划写入。",
                creative_goal="不得绕过 Manifest",
                entry_state="旧状态",
                exit_state="新状态",
            ),
        )

    assert switched is True
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
        (root.id,),
    ).fetchone()[0] == before_versions
    assert repository.get_slot(root.id).draft is None


@pytest.mark.parametrize(
    "operation",
    ("ensure_root", "create_contract", "save_draft", "publish_and_sync"),
)
def test_legacy_mutators_recheck_authority_inside_their_write_transaction(
    plan_repo, monkeypatch, operation
):
    """The second Head read, not the fast preflight, authorizes legacy DML."""

    database, repository = plan_repo
    root = None
    draft = None
    if operation != "ensure_root":
        _published_root(database, repository)
        root = repository.ensure_root("novel-1")
    if operation == "publish_and_sync":
        draft = repository.save_draft(
            root.id,
            OutlinePayload(
                title="待发布草稿",
                narrative_text="此草稿用于验证发布写锁。",
                creative_goal="验证事务边界",
                entry_state="开始",
                exit_state="结束",
            ),
        )

    original = repository._assert_legacy_mutation
    observed_transactions: list[bool] = []

    def observe_authority_check(novel_id: str, mutation: str) -> None:
        observed_transactions.append(database.get_connection().in_transaction)
        original(novel_id, mutation)

    monkeypatch.setattr(repository, "_assert_legacy_mutation", observe_authority_check)

    if operation == "ensure_root":
        repository.ensure_root("novel-1")
    elif operation == "create_contract":
        repository.create_contract(
            novel_id="novel-1",
            level=root.level.child_level,
            parent_contract_id=root.id,
        )
    elif operation == "save_draft":
        repository.save_draft(
            root.id,
            OutlinePayload(
                title="新的草稿",
                narrative_text="此草稿验证事务内 Head 重读。",
                creative_goal="验证事务边界",
                entry_state="开始",
                exit_state="结束",
            ),
        )
    else:
        repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)

    assert observed_transactions == [False, True]


@pytest.mark.parametrize(
    "operation",
    ("ensure_root", "create_contract", "save_draft", "publish_and_sync"),
)
def test_legacy_mutators_reject_racing_manifest_cutover_without_planning_dml(
    plan_repo, monkeypatch, operation
):
    """All legacy planning writers must fail closed after their preflight race."""

    database, repository = plan_repo
    _published_root(database, repository)
    plan = repository.backfill_initial_plan("novel-1").plan
    assert plan is not None
    root = repository.ensure_root("novel-1")
    draft = None
    if operation == "publish_and_sync":
        draft = repository.save_draft(
            root.id,
            OutlinePayload(
                title="不得发布的旧草稿",
                narrative_text="切换到 Manifest 后，旧发布必须被拒绝。",
                creative_goal="验证切换边界",
                entry_state="开始",
                exit_state="结束",
            ),
        )

    conn = database.get_connection()
    before = {
        "contracts": conn.execute(
            "SELECT COUNT(*) FROM outline_contracts WHERE novel_id = 'novel-1'"
        ).fetchone()[0],
        "versions": conn.execute(
            "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
            (root.id,),
        ).fetchone()[0],
        "active_version_id": conn.execute(
            "SELECT active_version_id FROM outline_contracts WHERE id = ?", (root.id,)
        ).fetchone()[0],
        "draft_version_id": conn.execute(
            "SELECT draft_version_id FROM outline_contracts WHERE id = ?", (root.id,)
        ).fetchone()[0],
    }

    original = repository._assert_legacy_mutation
    switched = False

    def switch_after_preflight(novel_id: str, mutation: str) -> None:
        nonlocal switched
        original(novel_id, mutation)
        if mutation == operation and not switched:
            _cut_over_to_manifest(database, plan.id, plan.digest)
            switched = True

    monkeypatch.setattr(repository, "_assert_legacy_mutation", switch_after_preflight)

    with pytest.raises(PlanningAuthorityError, match="requires PlanRevision transaction"):
        if operation == "ensure_root":
            repository.ensure_root("novel-1")
        elif operation == "create_contract":
            repository.create_contract(
                novel_id="novel-1",
                level=root.level.child_level,
                parent_contract_id=root.id,
            )
        elif operation == "save_draft":
            repository.save_draft(
                root.id,
                OutlinePayload(
                    title="不得保存的旧草稿",
                    narrative_text="切换到 Manifest 后，旧草稿必须被拒绝。",
                    creative_goal="验证切换边界",
                    entry_state="开始",
                    exit_state="结束",
                ),
            )
        else:
            repository.publish_and_sync(
                root.id, expected_revision=draft.draft.revision
            )

    assert switched is True
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_contracts WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == before["contracts"]
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
        (root.id,),
    ).fetchone()[0] == before["versions"]
    after = conn.execute(
        "SELECT active_version_id, draft_version_id FROM outline_contracts WHERE id = ?",
        (root.id,),
    ).fetchone()
    assert after["active_version_id"] == before["active_version_id"]
    assert after["draft_version_id"] == before["draft_version_id"]


@pytest.mark.parametrize(
    "operation",
    (
        "start_generation_attempt",
        "append_generation_attempt_delta",
        "finish_generation_attempt",
    ),
)
def test_legacy_generation_attempt_writers_reject_racing_manifest_cutover(
    plan_repo, monkeypatch, operation
):
    """A legacy streaming attempt cannot survive a Head cutover race."""

    database, repository = plan_repo
    _published_root(database, repository)
    plan = repository.backfill_initial_plan("novel-1").plan
    assert plan is not None
    root = repository.ensure_root("novel-1")
    attempt = None
    if operation != "start_generation_attempt":
        attempt = repository.start_generation_attempt(
            root.id,
            prompt_snapshot={"intent": "legacy outline stream"},
            context_digest="legacy-context",
        )

    conn = database.get_connection()
    before_attempt_count = conn.execute(
        "SELECT COUNT(*) FROM outline_generation_attempts WHERE contract_id = ?",
        (root.id,),
    ).fetchone()[0]
    if attempt is not None:
        before_attempt = conn.execute(
            "SELECT status, accumulated_text, completed_at, error FROM outline_generation_attempts "
            "WHERE id = ?",
            (attempt["id"],),
        ).fetchone()
        before_event_count = conn.execute(
            "SELECT COUNT(*) FROM outline_generation_attempt_events WHERE attempt_id = ?",
            (attempt["id"],),
        ).fetchone()[0]

    original = repository._assert_legacy_mutation
    switched = False

    def switch_after_preflight(novel_id: str, mutation: str) -> None:
        nonlocal switched
        original(novel_id, mutation)
        if mutation == operation and not switched:
            _cut_over_to_manifest(database, plan.id, plan.digest)
            switched = True

    monkeypatch.setattr(repository, "_assert_legacy_mutation", switch_after_preflight)

    with pytest.raises(PlanningAuthorityError, match="requires PlanRevision transaction"):
        if operation == "start_generation_attempt":
            repository.start_generation_attempt(
                root.id,
                prompt_snapshot={"intent": "must not be stored"},
                context_digest="cutover-context",
            )
        elif operation == "append_generation_attempt_delta":
            repository.append_generation_attempt_delta(attempt["id"], "must not persist")
        else:
            repository.cancel_generation_attempt(attempt["id"])

    assert switched is True
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_generation_attempts WHERE contract_id = ?",
        (root.id,),
    ).fetchone()[0] == before_attempt_count
    if attempt is not None:
        after_attempt = conn.execute(
            "SELECT status, accumulated_text, completed_at, error FROM outline_generation_attempts "
            "WHERE id = ?",
            (attempt["id"],),
        ).fetchone()
        assert tuple(after_attempt) == tuple(before_attempt)
        assert conn.execute(
            "SELECT COUNT(*) FROM outline_generation_attempt_events WHERE attempt_id = ?",
            (attempt["id"],),
        ).fetchone()[0] == before_event_count


def test_legacy_backfill_cannot_overwrite_a_racing_manifest_head(
    plan_repo, monkeypatch
):
    """Reusing a shadow snapshot must not repoint a just-cut-over Head."""

    database, repository = plan_repo
    item = _published_root(database, repository)
    reusable = repository.backfill_initial_plan("novel-1").plan
    assert reusable is not None
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET active_plan_revision_id = NULL, "
        "active_plan_digest = '' WHERE novel_id = 'novel-1'"
    )
    conn.commit()
    replacement = repository.seal_plan_revision(
        repository.create_plan_draft(
            novel_id="novel-1",
            items=(item,),
            canonical_prefix_digest="replacement-prefix",
            canonical_boundary={"formal_head": 0},
        ).id
    )

    original = repository.ensure_planning_head
    switched = False

    def switch_after_backfill_preflight(novel_id: str):
        nonlocal switched
        head = original(novel_id)
        if not switched:
            _cut_over_to_manifest(database, replacement.id, replacement.digest)
            switched = True
        return head

    monkeypatch.setattr(repository, "ensure_planning_head", switch_after_backfill_preflight)

    result = repository.backfill_initial_plan("novel-1")

    assert switched is True
    assert result.status == BackfillStatus.ALREADY_BACKFILLED
    assert result.plan is not None
    assert result.plan.id == replacement.id
    head = repository.get_planning_head("novel-1")
    assert head.authority_mode == PlanningAuthorityMode.MANIFEST
    assert head.active_plan_revision_id == replacement.id
    assert head.active_plan_digest == replacement.digest
    assert reusable.id != replacement.id


def test_head_starts_legacy_and_working_draft_is_not_active(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)

    first_head = repository.ensure_planning_head("novel-1")
    second_head = repository.ensure_planning_head("novel-1")
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
        author_intent="保留核心结局",
        created_by="author",
    )
    head_with_draft = repository.get_planning_head("novel-1")

    assert first_head == second_head
    assert first_head.authority_mode == PlanningAuthorityMode.LEGACY
    assert first_head.authority_generation == 0
    assert first_head.active_plan_revision_id is None
    assert draft.status == PlanRevisionStatus.DRAFT
    assert head_with_draft.working_plan_revision_id == draft.id
    assert head_with_draft.active_plan_revision_id is None

    sealed = repository.seal_plan_revision(draft.id)
    sealed_head = repository.get_planning_head("novel-1")

    assert sealed.status == PlanRevisionStatus.READY_FOR_REVIEW
    assert sealed.sealed_at
    assert sealed_head.working_plan_revision_id is None
    assert sealed_head.active_plan_revision_id is None


def test_identical_sealed_digest_reuses_existing_revision_and_content_version(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    first = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    sealed = repository.seal_plan_revision(first.id)
    retry = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )

    reused = repository.seal_plan_revision(retry.id)
    conn = database.get_connection()

    assert reused.id == sealed.id
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_plan_revisions WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
        (item.logical_node_id,),
    ).fetchone()[0] == 1


def test_seal_plan_revision_revalidates_inside_its_write_transaction(
    plan_repo, monkeypatch
):
    """No plan/version snapshot may be trusted before the sealing write lock."""

    database, repository = plan_repo
    item = _published_root(database, repository)
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    validate = repository._validate_plan_items
    get_plan_revision = repository.get_plan_revision

    def get_plan_under_write_lock(*args, **kwargs):
        if database.get_connection().in_transaction:
            assert kwargs.get("_connection") is database.get_connection()
        return get_plan_revision(*args, **kwargs)

    def validate_under_write_lock(*args, **kwargs):
        assert database.get_connection().in_transaction
        return validate(*args, **kwargs)

    monkeypatch.setattr(repository, "get_plan_revision", get_plan_under_write_lock)
    monkeypatch.setattr(repository, "_validate_plan_items", validate_under_write_lock)

    sealed = repository.seal_plan_revision(draft.id)

    assert sealed.sealed_at


def test_valid_synced_legacy_projection_backfills_once_without_cutover(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)

    result = repository.backfill_initial_plan("novel-1")
    repeated = repository.backfill_initial_plan("novel-1")
    head = repository.get_planning_head("novel-1")
    active = repository.get_active_plan("novel-1")

    assert result.status == BackfillStatus.MIGRATED
    assert repeated.status == BackfillStatus.ALREADY_BACKFILLED
    assert active is not None
    assert active.id == result.plan.id == repeated.plan.id
    assert active.status == PlanRevisionStatus.READY_FOR_REVIEW
    assert active.items == (item,)
    assert head.active_plan_revision_id == active.id
    assert head.active_plan_digest == active.digest
    assert head.authority_mode == PlanningAuthorityMode.LEGACY


def test_sealed_ready_for_review_head_is_a_valid_active_read(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    plan = repository.backfill_initial_plan("novel-1").plan
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    rows = repository.active_plan_items_with_payload("novel-1")

    assert plan is not None
    assert plan.status == PlanRevisionStatus.READY_FOR_REVIEW
    assert rows[0]["logical_node_id"] == item.logical_node_id


def test_active_manifest_read_rejects_a_tampered_sealed_payload(plan_repo):
    """A matching Head digest is insufficient without its sealed item snapshot."""

    database, repository = plan_repo
    item = _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    # Simulate a database created before immutability hardening or an offline
    # corruption repair. The read path itself must still reject the mismatch.
    conn.execute("DROP TRIGGER trg_outline_contract_versions_sealed_global_update")
    conn.execute("DROP TRIGGER trg_outline_contract_versions_manifest_sealed_update")
    conn.execute(
        "UPDATE outline_contract_versions SET payload_json = ? WHERE id = ?",
        ('{"title": "tampered"}', item.version_id),
    )
    conn.commit()

    with pytest.raises(OutlineGateError, match="payload digest"):
        repository.get_active_plan("novel-1")
    with pytest.raises(OutlineGateError, match="payload digest"):
        repository.active_plan_items_with_payload("novel-1")


def test_active_manifest_read_rejects_an_unsealed_content_version(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    conn.execute("DROP TRIGGER trg_outline_contract_versions_sealed_global_update")
    conn.execute("DROP TRIGGER trg_outline_contract_versions_manifest_sealed_update")
    conn.execute(
        "UPDATE outline_contract_versions SET sealed_at = NULL WHERE id = ?",
        (item.version_id,),
    )
    conn.commit()

    with pytest.raises(OutlineGateError, match="content version is not sealed"):
        repository.get_active_plan("novel-1")


def test_active_manifest_read_revalidates_sealed_topology(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    plan = repository.backfill_initial_plan("novel-1").plan
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    # Read validation is a second line of defense for data predating the
    # sealed-row triggers.
    conn.execute("DROP TRIGGER trg_outline_plan_items_sealed_update")
    conn.execute(
        "UPDATE outline_plan_revision_items "
        "SET validated_previous_sibling_digest = 'tampered' "
        "WHERE plan_revision_id = ?",
        (plan.id,),
    )
    conn.commit()

    with pytest.raises(OutlineGateError, match="sibling handoff"):
        repository.get_active_plan("novel-1")


def test_active_manifest_read_rejects_author_decision_plan_even_if_sealed(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    conn.execute("DROP TRIGGER trg_outline_plan_revisions_sealed_update")
    conn.execute(
        "UPDATE outline_plan_revisions "
        "SET reconciliation_status = 'author_decision_required' "
        "WHERE novel_id = 'novel-1'"
    )
    conn.commit()

    with pytest.raises(OutlineGateError, match="not aligned"):
        repository.get_active_plan("novel-1")


def test_active_manifest_read_rejects_mismatched_head_digest(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    conn.execute("DROP TRIGGER trg_outline_planning_heads_active_update")
    conn.execute("DROP TRIGGER trg_outline_planning_heads_publishable_update")
    conn.execute(
        "UPDATE outline_planning_heads SET active_plan_digest = 'tampered' "
        "WHERE novel_id = 'novel-1'"
    )
    conn.commit()

    with pytest.raises(OutlineGateError, match="Head digest"):
        repository.get_active_plan("novel-1")


def test_mixed_or_tampered_legacy_projection_stays_fail_closed(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    conn = database.get_connection()
    conn.execute("UPDATE outline_plan_projections SET digest = 'tampered'")
    conn.commit()

    result = repository.backfill_initial_plan("novel-1")

    assert result.status == BackfillStatus.PLANNING_MIGRATION_REQUIRED
    assert "digest" in result.reason
    assert repository.get_active_plan("novel-1") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_plan_revisions WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0


def test_sealed_manifest_rows_and_versions_reject_direct_mutation(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    result = repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            projection_generation = 1
        WHERE novel_id = 'novel-1'
        """
    )
    conn.commit()

    statements = (
        (
            "DELETE FROM outline_planning_heads WHERE novel_id = 'novel-1'",
            (),
        ),
        (
            "UPDATE outline_plan_revisions SET author_intent = 'changed' WHERE id = ?",
            (result.plan.id,),
        ),
        (
            "DELETE FROM outline_plan_revisions WHERE id = ?",
            (result.plan.id,),
        ),
        (
            "UPDATE outline_plan_revision_items SET sibling_index = 9 "
            "WHERE plan_revision_id = ?",
            (result.plan.id,),
        ),
        (
            "DELETE FROM outline_plan_revision_items WHERE plan_revision_id = ?",
            (result.plan.id,),
        ),
        (
            "UPDATE outline_contract_versions SET payload_json = '{}' WHERE id = ?",
            (item.version_id,),
        ),
        (
            "DELETE FROM outline_contract_versions WHERE id = ?",
            (item.version_id,),
        ),
    )

    for sql, parameters in statements:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql, parameters)
        conn.rollback()


def test_sealed_content_versions_are_immutable_before_manifest_cutover(plan_repo):
    """Shadow-backfilled content is immutable even while the Head is legacy."""

    database, repository = plan_repo
    item = _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()

    for sql in (
        "UPDATE outline_contract_versions SET payload_json = '{}' WHERE id = ?",
        "DELETE FROM outline_contract_versions WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql, (item.version_id,))
        conn.rollback()


def test_delete_manifest_book_removes_sealed_plan_history_without_orphans(plan_repo):
    """Hardening must not strand a normal whole-book cascade."""

    database, repository = plan_repo
    _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    conn.execute("DELETE FROM novels WHERE id = 'novel-1'")
    conn.commit()

    for table in (
        "outline_planning_heads",
        "outline_plan_revision_items",
        "outline_plan_revisions",
        "outline_plan_projections",
        "outline_contract_versions",
        "outline_contracts",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_manifest_head_rejects_sealed_plan_requiring_author_decision(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    active = repository.backfill_initial_plan("novel-1").plan
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="new-canonical-prefix",
        canonical_boundary={"formal_head": 0},
        parent_plan_revision_id=active.id,
        reconciliation_status=PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED,
    )
    target = repository.seal_plan_revision(draft.id)

    with pytest.raises(sqlite3.IntegrityError, match="ready_for_review.*aligned"):
        conn.execute(
            "UPDATE outline_planning_heads "
            "SET active_plan_revision_id=?, active_plan_digest=?, "
            "authority_generation=2, projection_generation=2 "
            "WHERE novel_id='novel-1'",
            (target.id, target.digest),
        )
    conn.rollback()


def test_manifest_head_cannot_return_to_legacy_after_cutover(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="cannot return to legacy"):
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode='legacy' "
            "WHERE novel_id='novel-1'"
        )
    conn.rollback()


def test_plan_items_require_valid_parent_level_and_parent_digest(plan_repo):
    database, repository = plan_repo
    root_item = _published_root(database, repository)
    root = repository.ensure_root("novel-1")
    part = repository.create_contract(
        novel_id="novel-1", level=root.level.child_level, parent_contract_id=root.id
    )
    draft = repository.save_draft(
        part.id,
        OutlinePayload(
            title="部纲",
            narrative_text="部纲推进",
            creative_goal="推进",
            entry_state="前态",
            exit_state="后态",
        ),
    )
    published = repository.publish_and_sync(part.id, expected_revision=draft.draft.revision)
    conn = database.get_connection()
    version = conn.execute(
        "SELECT id, digest FROM outline_contract_versions WHERE id = "
        "(SELECT active_version_id FROM outline_contracts WHERE id = ?)",
        (published.id,),
    ).fetchone()
    bad = OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(version["id"]),
        version_digest=str(version["digest"]),
        level=published.level,
        sibling_index=0,
        parent_logical_node_id=root_item.logical_node_id,
        validated_parent_digest="not-the-root-digest",
    )

    with pytest.raises(ValueError, match="parent.*digest"):
        repository.create_plan_draft(
            novel_id="novel-1",
            items=(root_item, bad),
            canonical_prefix_digest="",
            canonical_boundary={"formal_head": 0},
        )


def test_legacy_publish_invalidates_only_the_shadow_pointer_then_rebackfills(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository, title="总纲 v1")
    first = repository.backfill_initial_plan("novel-1").plan
    root = repository.ensure_root("novel-1")

    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲 v2",
            narrative_text="未来规划已经改变。",
            creative_goal="新的全书目标",
            entry_state="开始",
            exit_state="新的结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)

    invalidated_head = repository.get_planning_head("novel-1")
    preserved = repository.get_plan_revision(first.id)
    assert invalidated_head.authority_mode == PlanningAuthorityMode.LEGACY
    assert invalidated_head.active_plan_revision_id is None
    assert preserved.digest == first.digest
    assert preserved.items == first.items
    assert database.get_connection().execute(
        "SELECT status FROM outline_contract_versions WHERE id = ?",
        (first.items[0].version_id,),
    ).fetchone()[0] == "synced"

    second = repository.backfill_initial_plan("novel-1").plan
    assert second.id != first.id
    assert second.digest != first.digest


def test_head_rejects_manifest_without_active_plan_and_cross_novel_working_draft(
    plan_repo,
):
    database, repository = plan_repo
    item = _published_root(database, repository)
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    conn = database.get_connection()

    with pytest.raises(sqlite3.IntegrityError, match="active sealed plan"):
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode = 'manifest' "
            "WHERE novel_id = 'novel-1'"
        )
    conn.rollback()

    conn.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-2', 'Other', 'other')"
    )
    conn.execute(
        "INSERT INTO outline_planning_heads (novel_id) VALUES ('novel-2')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="same-novel editable draft"):
        conn.execute(
            "UPDATE outline_planning_heads SET working_plan_revision_id = ? "
            "WHERE novel_id = 'novel-2'",
            (draft.id,),
        )
    conn.rollback()
