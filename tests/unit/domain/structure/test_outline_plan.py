"""Pure contracts for immutable book-level outline manifests."""

from dataclasses import replace

from domain.structure.outline_contract import OutlineLevel
from domain.structure.outline_plan import (
    OutlinePlanItem,
    PlanningAuthorityMode,
    PlanningHead,
    canonical_plan_digest,
)


def _item(
    logical_node_id: str,
    *,
    sibling_index: int,
    version_digest: str,
) -> OutlinePlanItem:
    return OutlinePlanItem(
        logical_node_id=logical_node_id,
        version_id=f"version-{logical_node_id}",
        version_digest=version_digest,
        level=OutlineLevel.PART,
        sibling_index=sibling_index,
        parent_logical_node_id="outline-root",
    )


def test_plan_digest_is_order_independent_but_binds_history_topology_and_content():
    first = _item("part-a", sibling_index=0, version_digest="digest-a")
    second = _item("part-b", sibling_index=1, version_digest="digest-b")

    expected = canonical_plan_digest(
        canonical_prefix_digest="history-v1",
        items=(first, second),
    )

    assert canonical_plan_digest(
        canonical_prefix_digest="history-v1",
        items=(second, first),
    ) == expected
    assert canonical_plan_digest(
        canonical_prefix_digest="history-v2",
        items=(first, second),
    ) != expected
    assert canonical_plan_digest(
        canonical_prefix_digest="history-v1",
        items=(first, replace(second, sibling_index=2)),
    ) != expected
    assert canonical_plan_digest(
        canonical_prefix_digest="history-v1",
        items=(first, replace(second, version_digest="digest-b2")),
    ) != expected


def test_working_plan_pointer_never_becomes_the_active_planning_authority():
    head = PlanningHead(
        novel_id="novel-1",
        authority_mode=PlanningAuthorityMode.MANIFEST,
        authority_generation=7,
        active_plan_revision_id="plan-active",
        active_plan_digest="digest-active",
        working_plan_revision_id="plan-draft",
        projection_generation=7,
    )

    assert head.active_plan_revision_id == "plan-active"
    assert head.working_plan_revision_id == "plan-draft"
    assert head.active_plan_revision_id != head.working_plan_revision_id
