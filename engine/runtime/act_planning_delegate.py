"""幕级规划委托 — Phase 5 从 AutopilotDaemon 迁入 engine/runtime"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping

from domain.novel.entities.novel import Novel, NovelStage, AutopilotStatus
from domain.structure.story_node import StoryNode, NodeType, PlanningStatus, PlanningSource
from application.engine.services.hierarchical_narrative_alignment_gate import (
    HierarchicalNarrativeAlignmentGate,
)

logger = logging.getLogger(__name__)


def _positive_node_capacity(node: StoryNode, field: str) -> int:
    try:
        value = int(getattr(node, field, 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return max(value, 0)


def _reserved_act_capacity(act_node: StoryNode) -> int:
    for field in ("chapter_count", "suggested_chapter_count"):
        capacity = _positive_node_capacity(act_node, field)
        if capacity > 0:
            return capacity
    return 0


def _reserved_volume_capacity(volume_node: StoryNode, act_nodes: List[StoryNode]) -> int:
    return max(
        _positive_node_capacity(volume_node, "suggested_chapter_count"),
        sum(
            _reserved_act_capacity(act_node)
            for act_node in act_nodes
            if act_node.parent_id == volume_node.id
        ),
    )


def _remaining_part_capacity(
    part_node: StoryNode,
    volume_nodes: List[StoryNode],
    act_nodes: List[StoryNode],
) -> int | None:
    part_capacity = _positive_node_capacity(part_node, "suggested_chapter_count")
    if part_capacity <= 0:
        return None
    reserved = sum(
        _reserved_volume_capacity(volume_node, act_nodes)
        for volume_node in volume_nodes
        if volume_node.parent_id == part_node.id
    )
    return max(part_capacity - reserved, 0)


def _read_shared_state(novel_id: str) -> dict[str, Any]:
    from application.ai_invocation.autopilot.shared_state import read_autopilot_shared_state

    return read_autopilot_shared_state(novel_id)


def _consume_pending_act_plan(host: Any, *, novel_id: str, act_id: str) -> dict[str, Any] | None:
    shared = _read_shared_state(novel_id)
    pending_act_id = str(shared.get("autopilot_pending_act_plan_id") or "")
    pending = shared.get("autopilot_pending_act_chapters")
    if pending_act_id != str(act_id) or not isinstance(pending, list):
        return None
    host._update_shared_state(
        novel_id,
        autopilot_pending_act_plan_id=None,
        autopilot_pending_act_chapters=None,
        active_invocation_session_id="",
        active_invocation_operation="",
        active_invocation_node_key="",
        active_invocation_status="completed",
        active_invocation_policy="",
        has_active_invocation=False,
        requires_ai_review=False,
        autopilot_pause_reason="",
    )
    return {"success": True, "act_id": act_id, "chapters": list(pending)}


def _write_act_variables(*, novel_id: str, act_id: str, context: str, chapter_count: int, node_key: str) -> None:
    from application.ai_invocation.variable_hub import VariableWrite
    from infrastructure.persistence.database.connection import get_database
    from infrastructure.persistence.database.sqlite_ai_invocation_repository import SqliteVariableHubRepository

    repo = SqliteVariableHubRepository(get_database())
    context_key = f"novel_id:{novel_id}|act_id:{act_id}"
    for key, value, value_type in (
        ("novel.planning.act.context", context, "string"),
        ("novel.planning.act.chapter_count", int(chapter_count or 0), "integer"),
    ):
        repo.set_value(
            VariableWrite(
                key=key,
                value=value,
                context_key=context_key,
                source_node_key=node_key,
                source_trace_id=node_key,
                scope="novel",
                stage="planning",
                display_name=key,
                value_type=value_type,
            )
        )


def _pause_for_invocation(host: Any, novel: Novel, outcome) -> None:
    session = outcome.payload.get("session") if isinstance(outcome.payload, Mapping) else None
    policy_value = getattr(getattr(session, "policy", ""), "value", "") or "AUTOPILOT_PAUSE"
    host._update_shared_state(
        novel.novel_id.value,
        active_invocation_session_id=outcome.session_id,
        active_invocation_operation=outcome.operation,
        active_invocation_node_key=outcome.node_key,
        active_invocation_status=outcome.status,
        active_invocation_policy=policy_value,
        has_active_invocation=True,
        requires_ai_review=True,
        autopilot_pause_reason=outcome.autopilot_pause_reason or "awaiting_ai_review",
    )
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    host._flush_novel(novel)


async def _request_act_invocation(
    host: Any,
    novel: Novel,
    *,
    target_act: StoryNode,
    chapter_budget: int,
) -> Any:
    from application.ai_invocation.autopilot.factory import get_or_create_autopilot_orchestrator
    from application.ai_invocation.autopilot.intents import AutopilotInvocationIntent
    from application.ai_invocation.autopilot.policy import AutopilotInvocationPolicyResolver
    from application.ai_invocation.contracts import ensure_invocation_contract
    from infrastructure.ai.generation_profiles import generation_config_from_profile
    from infrastructure.ai.prompt_keys import PLANNING_ACT
    from infrastructure.persistence.database.connection import get_database

    bible_context = host.planning_service._get_bible_context(novel.novel_id.value)
    previous_summary = await host.planning_service._get_previous_acts_summary(target_act)
    prompt = host.planning_service._build_act_planning_prompt(
        target_act,
        bible_context,
        previous_summary,
        int(chapter_budget or 0),
    )
    variables = {
        "context": prompt.user,
        "chapter_count": int(chapter_budget or 0),
    }
    ensure_invocation_contract("autopilot.act.plan", PLANNING_ACT, get_database())
    _write_act_variables(
        novel_id=novel.novel_id.value,
        act_id=target_act.id,
        context=prompt.user,
        chapter_count=int(chapter_budget or 0),
        node_key=PLANNING_ACT,
    )
    policy = AutopilotInvocationPolicyResolver().resolve(
        operation="autopilot.act.plan",
        node_key=PLANNING_ACT,
        novel=novel,
        context={"novel_id": novel.novel_id.value, "act_id": target_act.id},
    )
    config = generation_config_from_profile("planning_act")
    return await get_or_create_autopilot_orchestrator(host).request(
        AutopilotInvocationIntent(
            novel_id=novel.novel_id.value,
            stage="planning",
            operation="autopilot.act.plan",
            node_key=PLANNING_ACT,
            context={
                "novel_id": novel.novel_id.value,
                "act_id": target_act.id,
                "chapter_count": int(chapter_budget or 0),
            },
            explicit_variables=variables,
            continuation_handler_key="autopilot_act_plan",
            policy_hint=policy,
            metadata={"source": "act_planning_delegate"},
            config=config,
        )
    )


async def run_act_planning(host: Any, novel: Novel) -> None:
    """处理幕级规划（插入缓冲章策略 + 动态幕生成）"""
    if not host._is_still_running(novel):
        return

    host._update_shared_state(
        novel.novel_id.value,
        writing_substep="act_planning",
        writing_substep_label=f"第 {novel.current_act + 1} 幕规划",
    )

    novel_id = novel.novel_id.value
    target_act_number = novel.current_act + 1

    from application.blueprint.services.continuous_planning_service import calculate_structure_params

    target_chapters = novel.target_chapters or 100
    struct_params = calculate_structure_params(target_chapters)
    rec_chapters_per_act = struct_params["chapters_per_act"]
    rec_acts_per_volume = struct_params["acts_per_volume"]

    all_nodes = await host.story_node_repo.get_by_novel(novel_id)
    chapter_nodes = [
        node for node in all_nodes if node.node_type.value == "chapter"
    ]
    highest_planned_chapter = max(
        (int(node.number) for node in chapter_nodes), default=0
    )
    remaining_chapter_capacity = target_chapters - highest_planned_chapter
    if remaining_chapter_capacity <= 0:
        logger.info(
            "[%s] 已无剩余章节容量（target=%s planned=%s），暂停幕级规划",
            novel.novel_id,
            target_chapters,
            highest_planned_chapter,
        )
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        novel.autopilot_status = AutopilotStatus.STOPPED
        host._flush_novel(novel)
        return
    act_nodes = sorted(
        [n for n in all_nodes if n.node_type.value == "act"],
        key=lambda n: n.number,
    )
    volume_nodes = sorted(
        [n for n in all_nodes if n.node_type.value == "volume"],
        key=lambda n: n.number,
    )
    volume_ids = {node.id for node in volume_nodes}
    reserved_novel_capacity = sum(
        _reserved_volume_capacity(node, act_nodes) for node in volume_nodes
    ) + sum(
        _reserved_act_capacity(node)
        for node in act_nodes
        if node.parent_id not in volume_ids
    )
    unreserved_chapter_capacity = max(
        min(
            remaining_chapter_capacity,
            target_chapters - reserved_novel_capacity,
        ),
        0,
    )

    target_act = next((n for n in act_nodes if n.number == target_act_number), None)

    if not target_act:
        if not volume_nodes:
            logger.error(
                "[%s] 宏观规划缺少卷节点！无法进行幕级规划。"
                "parts=%s, volumes=0, acts=%s. 触发重新规划...",
                novel_id,
                len([n for n in all_nodes if n.node_type.value == "part"]),
                len(act_nodes),
            )
            novel.current_stage = NovelStage.MACRO_PLANNING
            novel.current_act = 0
            host._flush_novel(novel)
            return

        parent_volume = host._find_parent_volume_for_new_act(
            volume_nodes=volume_nodes,
            act_nodes=act_nodes,
            current_auto_chapters=novel.current_auto_chapters or 0,
            target_chapters=target_chapters,
            rec_acts_per_volume=rec_acts_per_volume,
            novel_id=novel.novel_id,
        )

        if parent_volume is None:
            if unreserved_chapter_capacity <= 0:
                logger.info(
                    "[%s] 已无未预留章节容量（target=%s reserved=%s），暂停新增幕",
                    novel.novel_id,
                    target_chapters,
                    target_chapters - unreserved_chapter_capacity,
                )
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                novel.autopilot_status = AutopilotStatus.STOPPED
                host._flush_novel(novel)
                return
            next_chapter_number = (
                max((int(n.number) for n in chapter_nodes), default=0) + 1
            )
            if target_chapters > 0 and next_chapter_number > target_chapters:
                logger.info(
                    "[%s] 结构已规划至目标章节 %s，暂停新增幕",
                    novel.novel_id,
                    target_chapters,
                )
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                novel.autopilot_status = AutopilotStatus.STOPPED
                host._flush_novel(novel)
                return

            last_volume = volume_nodes[-1]
            if not last_volume.parent_id:
                logger.error(
                    "[%s] 最后一个卷缺少父部，无法安全创建下一卷",
                    novel.novel_id,
                )
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                novel.autopilot_status = AutopilotStatus.ERROR
                host._flush_novel(novel)
                return

            parent_part = next(
                (
                    node
                    for node in all_nodes
                    if node.id == last_volume.parent_id
                    and node.node_type == NodeType.PART
                ),
                None,
            )
            remaining_part_capacity = (
                _remaining_part_capacity(parent_part, volume_nodes, act_nodes)
                if parent_part is not None
                else None
            )
            next_volume_number = max(int(node.number) for node in volume_nodes) + 1
            continuation_volume_capacity = min(
                rec_chapters_per_act * rec_acts_per_volume,
                unreserved_chapter_capacity,
                remaining_part_capacity
                if remaining_part_capacity is not None
                else unreserved_chapter_capacity,
            )
            if continuation_volume_capacity <= 0:
                logger.info(
                    "[%s] 父部或全书已无剩余章节容量，暂停新增卷",
                    novel.novel_id,
                )
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                novel.autopilot_status = AutopilotStatus.STOPPED
                host._flush_novel(novel)
                return
            parent_volume = StoryNode(
                id=f"volume-{novel_id}-{next_volume_number}",
                novel_id=novel_id,
                parent_id=last_volume.parent_id,
                node_type=NodeType.VOLUME,
                number=next_volume_number,
                title=f"第{next_volume_number}卷",
                description="动态续规划卷",
                order_index=max(
                    (int(getattr(node, "order_index", 0) or 0) for node in all_nodes),
                    default=0,
                ) + 1,
                planning_status=PlanningStatus.CONFIRMED,
                planning_source=PlanningSource.AI_MACRO,
                suggested_chapter_count=continuation_volume_capacity,
            )
            parent_volume.metadata["contract_digest"] = (
                HierarchicalNarrativeAlignmentGate.derive_contract_digest(parent_volume)
            )
            await host.story_node_repo.save(parent_volume)
            if (
                parent_part is not None
                and hasattr(parent_part, "suggested_chapter_count")
                and _positive_node_capacity(parent_part, "suggested_chapter_count") <= 0
            ):
                existing_volume_capacity = sum(
                    _reserved_volume_capacity(node, act_nodes)
                    for node in volume_nodes
                    if node.parent_id == parent_part.id
                )
                parent_part.suggested_chapter_count = min(
                    target_chapters,
                    existing_volume_capacity + continuation_volume_capacity,
                )
                await host.story_node_repo.save(parent_part)
            logger.info(
                "[%s] 所有现有卷已满，已创建第 %s 卷承接后续幕",
                novel.novel_id,
                next_volume_number,
            )

        if parent_volume:
            logger.info(
                "[%s] 动态生成第 %s 幕（父卷：第 %s 卷，每幕建议 %s 章）",
                novel.novel_id,
                target_act_number,
                parent_volume.number,
                rec_chapters_per_act,
            )
            try:
                last_act = act_nodes[-1] if act_nodes else None
                if last_act:
                    await host.planning_service.create_next_act_auto(
                        novel_id=novel_id,
                        current_act_id=last_act.id,
                        parent_volume_id=parent_volume.id,
                    )
                else:
                    logger.info("[%s] 创建首幕", novel.novel_id)
                    try:
                        parent_volume_capacity = int(
                            getattr(parent_volume, "suggested_chapter_count", 0) or 0
                        )
                    except (TypeError, ValueError):
                        parent_volume_capacity = 0
                    first_act_capacity = min(
                        rec_chapters_per_act,
                        parent_volume_capacity,
                    ) if parent_volume_capacity > 0 else rec_chapters_per_act
                    parent_part = next(
                        (
                            node
                            for node in all_nodes
                            if node.id == parent_volume.parent_id
                            and node.node_type == NodeType.PART
                        ),
                        None,
                    )
                    contract_lines = []
                    if parent_part is not None:
                        contract_lines.append(f"当前部《{parent_part.title}》")
                        if parent_part.description:
                            contract_lines.append(str(parent_part.description))
                    contract_lines.append(f"当前卷《{parent_volume.title}》")
                    if parent_volume.description:
                        contract_lines.append(str(parent_volume.description))
                    else:
                        contract_lines.append("首幕必须先落实当前卷已经确认的核心约定。")
                    first_act = StoryNode(
                        id=f"act-{novel_id}-1",
                        novel_id=novel_id,
                        parent_id=parent_volume.id,
                        node_type=NodeType.ACT,
                        number=1,
                        title="第一幕 · 开端",
                        description="\n".join(contract_lines),
                        order_index=max(
                            (int(getattr(node, "order_index", 0) or 0) for node in all_nodes),
                            default=0,
                        ) + 1,
                        planning_status=PlanningStatus.CONFIRMED,
                        planning_source=PlanningSource.AI_MACRO,
                        suggested_chapter_count=first_act_capacity,
                    )
                    first_act.metadata["contract_digest"] = (
                        HierarchicalNarrativeAlignmentGate.derive_contract_digest(first_act)
                    )
                    await host.story_node_repo.save(first_act)

                all_nodes = await host.story_node_repo.get_by_novel(novel_id)
                act_nodes = sorted(
                    [n for n in all_nodes if n.node_type.value == "act"],
                    key=lambda n: n.number,
                )
                target_act = next((n for n in act_nodes if n.number == target_act_number), None)
            except Exception as e:
                logger.warning("[%s] 动态幕生成失败: %s", novel.novel_id, e)

        if not target_act:
            logger.error(
                "[%s] 找不到第 %s 幕，且动态生成失败，回退到宏观规划",
                novel.novel_id,
                target_act_number,
            )
            novel.current_stage = NovelStage.MACRO_PLANNING
            novel.current_act = 0
            host._flush_novel(novel)
            return

    act_children = host.story_node_repo.get_children_sync(target_act.id)
    confirmed_chapters = [n for n in act_children if n.node_type.value == "chapter"]

    just_created_chapter_plan = False
    if not confirmed_chapters:
        chapter_budget = min(
            target_act.suggested_chapter_count or rec_chapters_per_act,
            remaining_chapter_capacity,
        )
        parent_volume = next(
            (
                node
                for node in all_nodes
                if node.id == target_act.parent_id and node.node_type == NodeType.VOLUME
            ),
            None,
        )
        if parent_volume is not None:
            try:
                parent_volume_capacity = int(
                    getattr(parent_volume, "suggested_chapter_count", 0) or 0
                )
            except (TypeError, ValueError):
                parent_volume_capacity = 0
            if parent_volume_capacity > 0:
                reserved_capacity = sum(
                    _reserved_act_capacity(sibling_act)
                    for sibling_act in act_nodes
                    if sibling_act.id != target_act.id
                    and sibling_act.parent_id == parent_volume.id
                )
                remaining_volume_capacity = max(
                    parent_volume_capacity - reserved_capacity,
                    0,
                )
                chapter_budget = min(chapter_budget, remaining_volume_capacity)
                logger.info(
                    "[%s] 第%s卷剩余章节容量 %s/%s，幕 %s 本次规划预算=%s",
                    novel.novel_id,
                    parent_volume.number,
                    remaining_volume_capacity,
                    parent_volume_capacity,
                    target_act_number,
                    chapter_budget,
                )
        if chapter_budget <= 0:
            logger.info(
                "[%s] 幕 %s 没有正章节预算，暂停幕级规划",
                novel.novel_id,
                target_act_number,
            )
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            novel.autopilot_status = AutopilotStatus.STOPPED
            host._flush_novel(novel)
            return
        if not target_act.suggested_chapter_count:
            logger.info(
                "[%s] 幕 %s 无 suggested_chapter_count，使用引擎推荐值 %s",
                novel.novel_id,
                target_act_number,
                rec_chapters_per_act,
            )
        plan_result: Dict[str, Any] = _consume_pending_act_plan(
            host,
            novel_id=novel_id,
            act_id=target_act.id,
        ) or {}
        if not plan_result:
            shared_state = _read_shared_state(novel_id)
            if shared_state.get("active_invocation_session_id") and shared_state.get("has_active_invocation"):
                logger.info(
                    "[%s] 幕级规划已有待处理 invocation session=%s，等待面板处理",
                    novel.novel_id,
                    shared_state.get("active_invocation_session_id"),
                )
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                novel.autopilot_status = AutopilotStatus.RUNNING
                host._flush_novel(novel)
                return
            try:
                outcome = await _request_act_invocation(
                    host,
                    novel,
                    target_act=target_act,
                    chapter_budget=chapter_budget,
                )
                if outcome.status in {
                    "awaiting_pre_call_review",
                    "awaiting_acceptance",
                    "awaiting_commit",
                    "blocked",
                    "failed",
                    "cancelled",
                }:
                    _pause_for_invocation(host, novel, outcome)
                    return
                commit = outcome.payload.get("commit") if isinstance(outcome.payload, Mapping) else None
                commit_result = getattr(commit, "result", None) if commit is not None else None
                continuation = commit_result.get("continuation") if isinstance(commit_result, Mapping) else None
                plan_result = dict((continuation or {}).get("act_plan") or {})
            except Exception as e:
                logger.warning("[%s] autopilot.act.plan 未捕获异常: %s", novel.novel_id, e, exc_info=True)
                plan_result = {}

        if not host._is_still_running(novel):
            logger.info("[%s] 幕级规划返回后检测到停止，不再落库", novel.novel_id)
            return

        raw = plan_result.get("chapters")
        chapters_data: List[Dict[str, Any]] = raw if isinstance(raw, list) else []
        if not chapters_data:
            logger.error("[%s] 幕 %s 规划失败：未得到有效章节规划", novel.novel_id, target_act_number)
            novel.consecutive_error_count = (novel.consecutive_error_count or 0) + 1
            if novel.consecutive_error_count >= 3:
                novel.autopilot_status = AutopilotStatus.ERROR
                logger.error("[%s] 连续失败达3次，已挂起", novel.novel_id)
            host._flush_novel(novel)
            return

        try:
            await host.planning_service.confirm_act_planning(
                act_id=target_act.id,
                chapters=chapters_data,
            )
        except Exception as exc:
            logger.warning(
                "[%s] 幕 %s 结构化规划落库失败: %s",
                novel.novel_id,
                target_act_number,
                exc,
            )
            novel.consecutive_error_count = (novel.consecutive_error_count or 0) + 1
            if novel.consecutive_error_count >= 3:
                novel.autopilot_status = AutopilotStatus.ERROR
                novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
                logger.error("[%s] 幕级规划连续失败达3次，已挂起", novel.novel_id)
            host._flush_novel(novel)
            return
        just_created_chapter_plan = True

    act_children = host.story_node_repo.get_children_sync(target_act.id)
    confirmed_chapters = [n for n in act_children if n.node_type.value == "chapter"]

    novel.current_act = target_act_number - 1

    if not confirmed_chapters:
        logger.error("[%s] 幕 %s 仍无章节节点，下轮继续幕级规划", novel.novel_id, target_act_number)
        novel.current_stage = NovelStage.ACT_PLANNING
        return

    if just_created_chapter_plan:
        if getattr(novel, "auto_approve_mode", False):
            novel.current_stage = NovelStage.WRITING
            host._flush_novel(novel)
            logger.info("[%s] 全自动模式：第 %s 幕规划完成，直接进入写作", novel.novel_id, target_act_number)
        else:
            novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
            host._flush_novel(novel)
            logger.info("[%s] 第 %s 幕规划完成，进入审阅等待", novel.novel_id, target_act_number)
    else:
        novel.current_stage = NovelStage.WRITING
        host._flush_novel(novel)
        logger.info("[%s] 第 %s 幕章节节点已存在，进入写作", novel.novel_id, target_act_number)
