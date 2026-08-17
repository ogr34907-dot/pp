"""Application orchestration for the one-way Legacy to Manifest cutover."""

from __future__ import annotations

from typing import Any

from domain.structure.outline_plan import BackfillStatus, PlanningAuthorityMode
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineGateError,
)
from infrastructure.persistence.database.plan_projection_writer import PlanProjectionWriter


class ManifestPlanningService:
    """Move one novel through the existing backfill and projection contracts."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def ensure_manifest_planning_authority(self, novel_id: str):
        repository = OutlineContractRepository(self.db)
        head = repository.ensure_planning_head(novel_id)
        if head.authority_mode == PlanningAuthorityMode.MANIFEST:
            return head
        if head.authority_mode != PlanningAuthorityMode.LEGACY:
            raise OutlineGateError("planning authority mode is invalid")

        if not head.active_plan_revision_id:
            result = repository.backfill_initial_plan(novel_id)
            if result.status == BackfillStatus.PLANNING_MIGRATION_REQUIRED:
                raise OutlineGateError(
                    "manifest cutover requires planning migration: " + result.reason
                )
            head = result.head
        else:
            head = repository.get_planning_head(novel_id)
        if not head.active_plan_revision_id or not head.active_plan_digest:
            raise OutlineGateError("manifest cutover has no sealed active plan")

        connection = self.db.get_connection()
        if connection.in_transaction:
            raise OutlineGateError("manifest cutover requires a clean connection")
        # OutlineContractRepository intentionally does not own the physical
        # repository; use the same connection-backed adapter that projection
        # code already trusts.
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

        writer = PlanProjectionWriter(StoryNodeRepository(self.db))
        with writer._projection_transaction(
            novel_id=novel_id,
            plan_revision_id=str(head.active_plan_revision_id),
            operation="cutover",
            expected_active_plan_revision_id=head.active_plan_revision_id,
            expected_active_plan_digest=head.active_plan_digest,
            expected_authority_generation=head.authority_generation,
            expected_projection_generation=head.projection_generation,
        ) as capability:
            writer._activate_head(capability)

        cutover = repository.get_planning_head(novel_id)
        if cutover.authority_mode != PlanningAuthorityMode.MANIFEST:
            raise OutlineGateError("manifest cutover did not activate the planning Head")
        return cutover
