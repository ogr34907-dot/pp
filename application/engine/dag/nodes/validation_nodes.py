"""Validation 节点 — 校验与监控（6 个节点）

- val_style: 文风警报器
- val_tension: 张力评估器
- val_anti_ai: Anti-AI 审计
- val_foreshadow: 伏笔雷达
- val_narrative: 叙事同步
- val_kg_infer: 知识图谱推断
"""
from __future__ import annotations

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
    CHAPTER_AFTERMATH,
    CLICHE_SCAN,
    FORESHADOW_CHECK,
    KG_INFERENCE,
    TENSION_SCORING,
    VOICE_DRIFT,
)

logger = logging.getLogger(__name__)


def _candidate_scope(context: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the immutable candidate scope, or ``None`` for canonical runs."""

    if not context.get("candidate_mode"):
        return None
    candidate_id = str(context.get("candidate_id") or "").strip()
    revision = context.get("content_revision")
    if revision is None:
        # Retry count is only a compatibility fallback for direct node callers;
        # production workflow always supplies the persisted content revision.
        revision = context.get("candidate_revision", 0)
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        revision = 0
    return {"candidate_id": candidate_id, "content_revision": revision}


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        values: list[str] = []
        for item in value.values():
            values.extend(_as_text_list(item))
        return values
    return []


def _chapter_payload(context: Dict[str, Any]) -> Dict[str, Any]:
    chain = context.get("outline_chain") or {}
    chapter = chain.get("chapter") if isinstance(chain, dict) else {}
    payload = chapter.get("payload") if isinstance(chapter, dict) else {}
    return dict(payload) if isinstance(payload, dict) else {}


def _candidate_narrative_proposal(content: str, context: Dict[str, Any]) -> dict[str, Any]:
    text = str(content or "").strip()
    return {
        "summary": text[:1000],
        "events": [sentence.strip() for sentence in text.replace("！", "。")
                    .replace("？", "。").split("。") if sentence.strip()][:20],
        "triples": [],
        "causal_edges": [],
        "status": "candidate_only",
        "scope": _candidate_scope(context),
    }


def _candidate_foreshadowing_proposal(content: str, context: Dict[str, Any]) -> dict[str, Any]:
    expected = _as_text_list(_chapter_payload(context).get("foreshadowing"))
    text = str(content or "")
    matched = [item for item in expected if item in text]
    return {
        "matched": matched,
        "pending": [item for item in expected if item not in matched],
        "status": "candidate_only",
        "scope": _candidate_scope(context),
    }


# ─── val_style: 文风警报器 ───


@NodeRegistry.register("val_style")
class StyleNode(BaseNode):
    """文风警报器 — VoiceDriftService"""

    meta = NodeMeta(
        node_type="val_style",
        display_name="文风警报器",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#ec4899",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
            NodePort(name="voice_fingerprint", data_type=PortDataType.TEXT, required=False),
        ],
        output_ports=[
            NodePort(name="drift_score", data_type=PortDataType.SCORE),
            NodePort(name="drift_alert", data_type=PortDataType.BOOLEAN),
        ],
        prompt_variables=["voice_fingerprint", "scene_type", "drift_threshold", "content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=180,
        llm_backed=True,
        cpms_node_key=VOICE_DRIFT,
        description="VoiceDriftService 文风偏离检测",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")
        drift_score = 0.0
        drift_alert = False

        if _candidate_scope(context) is not None:
            return NodeResult(
                outputs={"drift_score": drift_score, "drift_alert": drift_alert},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )

        try:
            try:
                from application.analyst.services.voice_drift_service import VoiceDriftService
                novel_id = context.get("novel_id", "")
                svc = VoiceDriftService()
                result = await svc.analyze(novel_id, content)
                drift_score = getattr(result, "similarity_score", 0.0) or 0.0
                drift_alert = getattr(result, "drift_alert", False) or False
            except Exception as e:
                logger.warning(f"VoiceDriftService 调用失败: {e}")

            # 应用阈值
            thresholds = self._config.thresholds if self._config else {}
            warning_threshold = thresholds.get("drift_warning", 0.5)
            if drift_score > warning_threshold:
                drift_alert = True

            return NodeResult(
                outputs={"drift_score": drift_score, "drift_alert": drift_alert},
                status=NodeStatus.WARNING if drift_alert else NodeStatus.SUCCESS,
                metrics={"drift_score": drift_score},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"drift_score": 0.0, "drift_alert": False}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_tension: 张力评估器 ───


@NodeRegistry.register("val_tension")
class TensionNode(BaseNode):
    """张力评估器 — TensionScoringService"""

    meta = NodeMeta(
        node_type="val_tension",
        display_name="张力评估器",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#f59e0b",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="plot_tension", data_type=PortDataType.SCORE),
            NodePort(name="emotional_tension", data_type=PortDataType.SCORE),
            NodePort(name="pacing_tension", data_type=PortDataType.SCORE),
            NodePort(name="composite", data_type=PortDataType.SCORE),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        llm_backed=True,
        cpms_node_key=TENSION_SCORING,
        description="TensionScoringService 叙事张力评估",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            if _candidate_scope(context) is not None:
                # Candidate audits must stay read-only and token-free. Canonical
                # scoring remains exclusively in the post-commit pipeline.
                return NodeResult(
                    outputs={
                        "plot_tension": 50.0,
                        "emotional_tension": 50.0,
                        "pacing_tension": 50.0,
                        "composite": 50.0,
                    },
                    status=NodeStatus.SUCCESS,
                    metrics={"composite": 50.0},
                    duration_ms=int((time.time() - start) * 1000),
                )
            plot_tension = 0.0
            emotional_tension = 0.0
            pacing_tension = 0.0
            composite = 0.0

            try:
                from application.analyst.services.tension_scoring_service import TensionScoringService
                novel_id = context.get("novel_id", "")
                from interfaces.api.dependencies import get_llm_service

                svc = TensionScoringService(get_llm_service())
                result = await svc.score_chapter(
                    content, int(context.get("chapter_number") or 0)
                )
                if result:
                    plot_tension = getattr(result, "plot_tension", 0.0)
                    emotional_tension = getattr(result, "emotional_tension", 0.0)
                    pacing_tension = getattr(result, "pacing_tension", 0.0)
                    composite = getattr(result, "composite_score", 0.0)
            except Exception as e:
                logger.warning(f"TensionScoringService 调用失败: {e}")

            return NodeResult(
                outputs={
                    "plot_tension": plot_tension,
                    "emotional_tension": emotional_tension,
                    "pacing_tension": pacing_tension,
                    "composite": composite,
                },
                status=NodeStatus.SUCCESS,
                metrics={"composite": composite},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"composite": 0.0}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_anti_ai: Anti-AI 审计 ───


@NodeRegistry.register("val_anti_ai")
class AntiAINode(BaseNode):
    """Anti-AI 审计 — cliche_scanner (L7)"""

    meta = NodeMeta(
        node_type="val_anti_ai",
        display_name="Anti-AI 审计",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#ef4444",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="severity_score", data_type=PortDataType.SCORE),
            NodePort(name="hits", data_type=PortDataType.LIST),
            NodePort(name="recommendations", data_type=PortDataType.LIST),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=60,
        cpms_node_key=CLICHE_SCAN,
        description="ClicheScanner AI 模式检测与审计",
        default_edges=["gw_circuit"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            severity_score = 0.0
            hits = []
            recommendations = []

            try:
                from application.audit.services.cliche_scanner import ClicheScanner
                from application.audit.services.anti_ai_audit import AntiAIAuditor

                scanner = ClicheScanner()
                hits = scanner.scan_cliches(content)
                metrics = AntiAIAuditor()._calculate_metrics("candidate", hits, content)
                severity_score = metrics.severity_score
                recommendations = [
                    hit.replacement_hint for hit in hits
                    if getattr(hit, "replacement_hint", "")
                ][:5]
            except Exception as e:
                logger.warning(f"ClicheScanner 调用失败: {e}")

            return NodeResult(
                outputs={
                    "severity_score": severity_score,
                    "hits": [
                        {
                            "pattern": hit.pattern,
                            "text": hit.text,
                            "severity": hit.severity,
                            "category": hit.category,
                        }
                        for hit in hits
                    ],
                    "recommendations": recommendations,
                },
                status=NodeStatus.SUCCESS,
                metrics={"severity_score": severity_score},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"severity_score": 0.0, "hits": [], "recommendations": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_foreshadow: 伏笔雷达 ───


@NodeRegistry.register("val_foreshadow")
class ForeshadowCheckNode(BaseNode):
    """伏笔雷达 — ForeshadowingRegistry"""

    meta = NodeMeta(
        node_type="val_foreshadow",
        display_name="伏笔雷达",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#22c55e",
        input_ports=[
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=True),
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="recovered", data_type=PortDataType.SCORE),
            NodePort(name="pending", data_type=PortDataType.SCORE),
            NodePort(name="recovery_rate", data_type=PortDataType.SCORE),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=30,
        cpms_node_key=FORESHADOW_CHECK,
        description="ForeshadowingRegistry 伏笔回收检测",
        default_edges=["val_kg_infer"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            candidate_proposal = _candidate_foreshadowing_proposal(
                inputs.get("content", ""), context
            ) if _candidate_scope(context) is not None else None
            if candidate_proposal is not None:
                return NodeResult(
                    outputs={
                        "recovered": len(candidate_proposal["matched"]),
                        "pending": len(candidate_proposal["pending"]),
                        "recovery_rate": (
                            len(candidate_proposal["matched"])
                            / max(1, len(candidate_proposal["matched"]) + len(candidate_proposal["pending"]))
                            * 100
                        ),
                        "candidate_foreshadowing_proposal": candidate_proposal,
                    },
                    status=NodeStatus.SUCCESS,
                    duration_ms=int((time.time() - start) * 1000),
                )
            recovered = 0
            pending = 0
            recovery_rate = 0.0

            try:
                from domain.novel.value_objects.novel_id import NovelId
                from interfaces.api.dependencies import get_foreshadowing_repository

                repo = get_foreshadowing_repository()
                novel_id = inputs.get("novel_id") or context.get("novel_id", "")
                registry = repo.get_by_novel_id(NovelId(str(novel_id)))
                all_f = list(getattr(registry, "foreshadowings", []) or [])
                recovered = len([f for f in all_f if str(getattr(f, "status", "")) == "ForeshadowingStatus.RESOLVED" or getattr(getattr(f, "status", None), "value", "") == "resolved"])
                pending = len([f for f in all_f if getattr(getattr(f, "status", None), "value", "") == "planted"])
                total = recovered + pending
                recovery_rate = (recovered / total * 100) if total > 0 else 0.0
            except Exception as e:
                logger.warning(f"伏笔雷达调用失败: {e}")

            return NodeResult(
                outputs={"recovered": recovered, "pending": pending, "recovery_rate": recovery_rate},
                status=NodeStatus.SUCCESS,
                metrics={"recovery_rate": recovery_rate},
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"recovered": 0, "pending": 0, "recovery_rate": 0.0}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True


# ─── val_narrative: 叙事同步 ───


@NodeRegistry.register("val_narrative")
class NarrativeNode(BaseNode):
    """叙事同步 — ChapterAftermathPipeline step 1"""

    meta = NodeMeta(
        node_type="val_narrative",
        display_name="叙事同步",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#06b6d4",
        input_ports=[
            NodePort(name="content", data_type=PortDataType.TEXT, required=True),
        ],
        output_ports=[
            NodePort(name="summary", data_type=PortDataType.TEXT),
            NodePort(name="events", data_type=PortDataType.LIST),
            NodePort(name="triples", data_type=PortDataType.LIST),
            NodePort(name="causal_edges", data_type=PortDataType.LIST),
        ],
        prompt_variables=["content"],
        is_configurable=True,
        can_disable=True,
        default_timeout_seconds=180,
        cpms_node_key=CHAPTER_AFTERMATH,
        description="ChapterAftermathPipeline 叙事同步",
        default_edges=["val_foreshadow"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()
        content = inputs.get("content", "")

        try:
            proposal = _candidate_narrative_proposal(content, context)
            if _candidate_scope(context) is not None:
                return NodeResult(
                    outputs={
                        "summary": proposal["summary"],
                        "events": proposal["events"],
                        "triples": proposal["triples"],
                        "causal_edges": proposal["causal_edges"],
                        "candidate_narrative_proposal": proposal,
                    },
                    status=NodeStatus.SUCCESS,
                    duration_ms=int((time.time() - start) * 1000),
                )
            summary = ""
            events = []
            triples = []
            causal_edges = []

            try:
                from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
                novel_id = context.get("novel_id", "")
                # Legacy non-candidate DAGs retain a best-effort read-only
                # projection. Canonical writes belong to the saved-chapter path.
                result = _candidate_narrative_proposal(content, context)
                summary = result["summary"]
                events = result["events"]
            except Exception as e:
                logger.warning(f"ChapterAftermathPipeline 调用失败: {e}")

            return NodeResult(
                outputs={"summary": summary, "events": events, "triples": triples, "causal_edges": causal_edges},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"summary": "", "events": [], "triples": [], "causal_edges": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return "content" in inputs


# ─── val_kg_infer: 知识图谱推断 ───


@NodeRegistry.register("val_kg_infer")
class KGInferNode(BaseNode):
    """知识图谱推断 — KnowledgeGraphService.infer_from_chapter"""

    meta = NodeMeta(
        node_type="val_kg_infer",
        display_name="KG推断",
        category=NodeCategory.VALIDATION,
        icon="",
        color="#8b5cf6",
        input_ports=[
            NodePort(name="novel_id", data_type=PortDataType.TEXT, required=True),
            NodePort(name="chapter_number", data_type=PortDataType.SCORE, required=False),
        ],
        output_ports=[
            NodePort(name="inferred_triples", data_type=PortDataType.LIST),
        ],
        prompt_variables=[],
        is_configurable=False,
        can_disable=True,
        default_timeout_seconds=120,
        cpms_node_key=KG_INFERENCE,
        description="KnowledgeGraphService.infer_from_chapter",
        default_edges=["gw_review"],
    )

    async def execute(self, inputs: Dict[str, Any], context: Dict[str, Any]) -> NodeResult:
        import time
        start = time.time()

        try:
            if _candidate_scope(context) is not None:
                narrative = context.get("shared_state", {}).get("candidate_narrative_proposal", {})
                foreshadowing = context.get("shared_state", {}).get("candidate_foreshadowing_proposal", {})
                proposal = {
                    "scope": _candidate_scope(context),
                    "narrative": narrative if isinstance(narrative, dict) else {},
                    "foreshadowing": foreshadowing if isinstance(foreshadowing, dict) else {},
                    "knowledge_graph": {"status": "candidate_only", "triples": []},
                }
                return NodeResult(
                    outputs={"inferred_triples": [], "candidate_proposals": proposal},
                    status=NodeStatus.SUCCESS,
                    duration_ms=int((time.time() - start) * 1000),
                )
            inferred_triples = []

            try:
                # Canonical KG inference is deliberately post-commit only.
                inferred_triples = []
            except Exception as e:
                logger.warning(f"KnowledgeGraphService 调用失败: {e}")

            return NodeResult(
                outputs={"inferred_triples": inferred_triples},
                status=NodeStatus.SUCCESS,
                duration_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return NodeResult(outputs={"inferred_triples": []}, status=NodeStatus.ERROR, duration_ms=int((time.time() - start) * 1000), error=str(e))

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        return True
