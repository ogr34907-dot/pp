"""Gateway 节点 — 网关与熔断（4 个节点）

- gw_circuit: 熔断保护
- gw_review: 审阅网关
- gw_condition: 条件路由
- gw_retry: 重试网关
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from application.engine.dag.models import (
    NodeCategory,
    NodeMeta,
    NodePort,
    NodeResult,
    NodeStatus,
    PortDataType,
)
from application.engine.dag.registry import BaseNode, NodeRegistry
from infrastructure.ai.prompt_keys import (
    CIRCUIT_BREAKER,
    CONDITION_GATEWAY,
    RETRY_GATEWAY,
    REVIEW_GATEWAY,
)

logger = logging.getLogger(__name__)


# ─── gw_circuit: 熔断保护 ───


@NodeRegistry.register("gw_circuit")
class CircuitNode(BaseNode):
    """熔断保护 — CircuitBreaker"""

    meta = NodeMeta(
        node_type="gw_circuit",
        display_name="熔断保护",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#ef4444",
        input_ports=[
            NodePort(name="error_count", data_type=PortDataType.SCORE, required=False, default=0),
            NodePort(name="max_errors", data_type=PortDataType.SCORE, required=False, default=3),
            NodePort(name="drift_alert", data_type=PortDataType.BOOLEAN, required=False, default=False),
            NodePort(name="severity_score", data_type=PortDataType.SCORE, required=False, default=0),
            NodePort(name="composite", data_type=PortDataType.SCORE, required=False, default=50),
        ],
        output_ports=[
            NodePort(name="breaker_status", data_type=PortDataType.TEXT),
            NodePort(name="review_required", data_type=PortDataType.BOOLEAN),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=False,
        default_timeout_seconds=5,
        cpms_node_key=CIRCUIT_BREAKER,
        description="CircuitBreaker 熔断保护网关",
        default_edges=["val_narrative"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            error_count = int(inputs.get("error_count", 0) or 0)
            thresholds = self._config.thresholds if self._config else {}
            max_errors = thresholds.get("max_errors", inputs.get("max_errors", 3))
            anti_ai_limit = thresholds.get("anti_ai_max_severity", 1)
            tension_floor = thresholds.get("tension_floor", 30)
            drift_alert = bool(inputs.get("drift_alert", False))
            severity_score = float(inputs.get("severity_score", 0) or 0)
            composite = float(inputs.get("composite", 50) or 0)
            review_required = (
                error_count >= int(max_errors)
                or drift_alert
                or severity_score >= float(anti_ai_limit)
                or composite < float(tension_floor)
            )
            breaker_status = "open" if review_required else "closed"

            return NodeResult(
                outputs={"breaker_status": breaker_status, "review_required": review_required},
                status=NodeStatus.WARNING if breaker_status == "open" else NodeStatus.SUCCESS,
                metrics={"error_count": float(error_count)},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"breaker_status": "open"}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── gw_review: 审阅网关 ───


@NodeRegistry.register("gw_review")
class ReviewNode(BaseNode):
    """审阅网关 — PAUSED_FOR_REVIEW 状态"""

    meta = NodeMeta(
        node_type="gw_review",
        display_name="⏸️ 审阅网关",
        category=NodeCategory.GATEWAY,
        icon="⏸️",
        color="#f59e0b",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=False),
            NodePort(name="metrics", data_type=PortDataType.JSON, required=False),
            NodePort(name="review_required", data_type=PortDataType.BOOLEAN, required=False, default=False),
            NodePort(name="run_mode", data_type=PortDataType.TEXT, required=False, default="chapter_review"),
        ],
        output_ports=[
            NodePort(name="approved", data_type=PortDataType.BOOLEAN),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=10,
        cpms_node_key=REVIEW_GATEWAY,
        description="PAUSED_FOR_REVIEW 审阅网关",
        default_edges=[],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            semantic_review: dict[str, Any] = {}
            # 检查是否自动审批模式
            approved = not bool(inputs.get("review_required", False))

            # 如果有关键指标异常，不自动审批
            metrics = inputs.get("metrics", {})
            if isinstance(metrics, dict):
                if metrics.get("drift_alert", False):
                    approved = False
                if metrics.get("breaker_status") == "open":
                    approved = False

            if str(inputs.get("run_mode") or context.get("run_mode") or "chapter_review") != "continuous":
                approved = False

            if context.get("candidate_mode"):
                semantic_review, semantic_approved = await self._review_candidate(
                    str(inputs.get("content") or ""), context
                )
                approved = approved and semantic_approved

            return NodeResult(
                outputs={
                    "approved": approved,
                    "review_required": not approved,
                    "semantic_review": semantic_review,
                },
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"approved": False}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True

    @staticmethod
    async def _review_candidate(content: str, context: Dict[str, Any]) -> tuple[dict[str, Any], bool]:
        reviewer = context.get("candidate_semantic_reviewer")
        if reviewer is None or not content.strip():
            return {
                "status": "unavailable",
                "machine_review_failed": True,
                "issues": [],
                "suggestions": [],
            }, False

        chain = context.get("outline_chain")
        chapter = chain.get("chapter", {}) if isinstance(chain, dict) else {}
        payload = chapter.get("payload", {}) if isinstance(chapter, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        from application.engine.dag.plan.schema import (
            chapter_rhythm_from_outline_payload,
            serialize_chapter_rhythm,
        )
        from application.audit.services.chapter_ai_review_service import (
            serialize_chapter_ai_review_result,
        )

        chapter_rhythm = context.get("chapter_rhythm")
        if not chapter_rhythm:
            chapter_rhythm = serialize_chapter_rhythm(
                chapter_rhythm_from_outline_payload(payload)
            )
        required_events = [
            str(item).strip()
            for item in payload.get("required_events", [])
            if str(item).strip()
        ]
        try:
            result = await reviewer.review(
                chapter_number=int(context.get("chapter_number") or 0),
                chapter_title=str(context.get("chapter_title") or ""),
                chapter_content=content,
                chapter_outline=json.dumps(
                    {
                        "creative_goal": payload.get("creative_goal", ""),
                        "entry_state": payload.get("entry_state", ""),
                        "exit_state": payload.get("exit_state", ""),
                        "required_events": required_events,
                        "forbidden_events": payload.get("forbidden_events", []),
                        "handoff_conditions": payload.get("handoff_conditions", []),
                        "chapter_rhythm": chapter_rhythm,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                generation_hint=(
                    "审查人物、时间线、世界规则、已发生事件、伏笔、AI味和商业节奏。"
                    "过渡章可以舒缓，但必须承担承接或推进；高潮章必须有升级、代价或兑现。"
                ),
                required_events=required_events,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "machine_review_failed": True,
                "error": str(exc),
                "issues": [],
                "suggestions": [],
            }, False

        issues = list(getattr(result, "issues", []) or [])
        critical = any(str(getattr(issue, "severity", "")).lower() == "critical" for issue in issues)
        status = str(getattr(result, "status", "reviewed") or "reviewed")
        rhythm_assessment = dict(getattr(result, "rhythm_assessment", {}) or {})
        if chapter_rhythm and rhythm_assessment.get("status") != "complete":
            critical = True
            status = "draft"
            rhythm_assessment.setdefault("status", "incomplete")
        review = serialize_chapter_ai_review_result(result)
        review["status"] = status
        review["rhythm_assessment"] = rhythm_assessment
        review["machine_review_incomplete"] = bool(
            chapter_rhythm and rhythm_assessment.get("status") != "complete"
        )
        return review, status == "approved" and not critical


# ─── gw_condition: 条件路由 ───


@NodeRegistry.register("gw_condition")
class ConditionNode(BaseNode):
    """条件路由 — 根据输入条件决定走哪条分支"""

    meta = NodeMeta(
        node_type="gw_condition",
        display_name="条件路由",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#3b82f6",
        input_ports=[
            NodePort(name="input", data_type=PortDataType.JSON, required=True),
        ],
        output_ports=[
            NodePort(name="output_true", data_type=PortDataType.JSON),
            NodePort(name="output_false", data_type=PortDataType.JSON),
        ],
        prompt_variables=[],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=5,
        cpms_node_key=CONDITION_GATEWAY,
        description="条件路由网关",
        default_edges=[],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            input_data = inputs.get("input", {})

            # 简单条件判断：检查是否有异常标志
            condition_met = True
            if isinstance(input_data, dict):
                condition_met = not (
                    input_data.get("drift_alert", False) or
                    input_data.get("breaker_status") == "open" or
                    input_data.get("error")
                )

            return NodeResult(
                outputs={
                    "output_true": input_data if condition_met else None,
                    "output_false": input_data if not condition_met else None,
                },
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"output_true": None, "output_false": None}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "input" in inputs


# ─── gw_retry: 重试网关 ───


@NodeRegistry.register("gw_retry")
class RetryNode(BaseNode):
    """Emit a bounded candidate-revision retry request.

    The retry is intentionally terminal for one acyclic DAG run.  The
    candidate workflow creates the next content revision and invokes the DAG
    again; a graph back-edge would block its initial writer run and cannot
    persist revision state safely.
    """

    meta = NodeMeta(
        node_type="gw_retry",
        display_name="重写网关",
        category=NodeCategory.GATEWAY,
        icon="",
        color="#8b5cf6",
        input_ports=[
            NodePort(name="breaker_status", data_type=PortDataType.TEXT, required=False),
            NodePort(name="max_attempts", data_type=PortDataType.SCORE, required=False, default=2),
        ],
        output_ports=[
            NodePort(name="retry_requested", data_type=PortDataType.BOOLEAN),
            NodePort(name="retry_exhausted", data_type=PortDataType.BOOLEAN),
            NodePort(name="retry_feedback", data_type=PortDataType.TEXT),
            NodePort(name="attempts_used", data_type=PortDataType.SCORE),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=False,
        default_timeout_seconds=10,
        cpms_node_key=RETRY_GATEWAY,
        description="文风检查失败时触发重写的重试网关",
        default_edges=["gw_review"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            max_attempts = inputs.get("max_attempts", 2)
            if self._config and self._config.max_retries:
                max_attempts = self._config.max_retries

            # 检查当前重试次数
            retry_count = context.get("shared_state", {}).get("candidate_revision", 0) if isinstance(context, dict) else 0

            if retry_count < max_attempts:
                return NodeResult(
                    outputs={
                        "retry_requested": True,
                        "retry_exhausted": False,
                        "retry_feedback": "候选稿未通过文风或安全审查，请按审查结果重写。",
                        "attempts_used": retry_count + 1,
                    },
                    status=NodeStatus.SUCCESS,
                    metrics={"retry_count": float(retry_count + 1)},
                    duration_ms=int((time.time() - start) * 1000),
                )
            else:
                return NodeResult(
                    outputs={
                        "retry_requested": False,
                        "retry_exhausted": True,
                        "retry_feedback": "候选稿重写次数已用尽，转入人工审核。",
                        "attempts_used": retry_count,
                    },
                    status=NodeStatus.WARNING,
                    metrics={"retry_count": float(retry_count)},
                    duration_ms=int((time.time() - start) * 1000),
                )
        except Exception as e:
            return NodeResult(outputs={"retry_requested": False, "retry_feedback": str(e), "attempts_used": 0}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True
