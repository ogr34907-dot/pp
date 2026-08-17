"""PolicyValidator — 策略验证器（适配 QualityGuardrail）

对 QualityGuardrail 的薄封装，提供与 Pipeline 兼容的接口。
保持 QualityGuardrail 的全部六维度检查能力不变，
只调整了接口命名和返回值格式。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from engine.validation_status import (
    ADVISORY_FAIL,
    HARD_FAIL,
    PASS,
    UNEVALUATED,
)

logger = logging.getLogger(__name__)


def _is_hard_violation(violation: Any) -> bool:
    severity = violation.get("severity") if isinstance(violation, dict) else violation
    if isinstance(severity, (int, float)):
        return float(severity) >= 0.75
    return str(severity or "").strip().lower() in {
        "critical", "error", "hard", "high", "严重", "致命"
    }


class PolicyReport:
    """策略验证报告（对应 QualityReport）"""

    def __init__(
        self,
        overall_score: Optional[float] = None,
        dimensions: Optional[Dict[str, float]] = None,
        violations: Optional[List[Dict[str, Any]]] = None,
        passed: bool = False,
        status: Optional[str] = None,
        suggestions: Optional[List[str]] = None,
    ):
        self.overall_score = overall_score
        self.language_style_score = (dimensions or {}).get("language_style", 0.0)
        self.character_consistency_score = (dimensions or {}).get("character_consistency", 0.0)
        self.plot_density_score = (dimensions or {}).get("plot_density", 0.0)
        self.naming_score = (dimensions or {}).get("naming", 0.0)
        self.viewpoint_score = (dimensions or {}).get("viewpoint", 0.0)
        self.rhythm_score = (dimensions or {}).get("rhythm", 0.0)
        self.all_violations = violations or []
        self.suggestions = suggestions or []
        self.status = status or (
            PASS
            if passed
            else (
                HARD_FAIL
                if (overall_score is not None and overall_score < 0.4)
                or any(_is_hard_violation(v) for v in self.all_violations)
                else ADVISORY_FAIL
            )
        )
        self.passed = self.status == PASS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall_score": round(self.overall_score, 3) if self.overall_score is not None else None,
            "scores": {
                "language_style": round(self.language_style_score, 3),
                "character_consistency": round(self.character_consistency_score, 3),
                "plot_density": round(self.plot_density_score, 3),
                "naming": round(self.naming_score, 3),
                "viewpoint": round(self.viewpoint_score, 3),
                "rhythm": round(self.rhythm_score, 3),
            },
            "violation_count": len(self.all_violations),
            "violations": self.all_violations,
            "passed": self.passed,
            "status": self.status,
            "suggestions": self.suggestions,
        }


class PolicyViolationError(Exception):
    """策略违规异常（对应 QualityViolationError）"""

    def __init__(self, violations: List[Dict[str, Any]], overall_score: Optional[float]):
        self.violations = violations
        self.overall_score = overall_score
        score = f"{overall_score:.2f}" if overall_score is not None else "unavailable"
        message = f"策略验证不通过(评分{score})，共{len(violations)}项违规"
        super().__init__(message)


class PolicyValidator:
    """策略验证器 — 对 QualityGuardrail 的薄封装

    保留 QualityGuardrail 的全部六维度检查能力，
    只调整接口命名：
    - enforce → enforce（不变，强制执行）
    - advise → advise（不变，建议模式）
    - check → check（不变，执行检查）

    使用方式：
        validator = PolicyValidator()
        report = validator.advise(text=content, character_masks=masks)
        if not report.passed:
            print(f"验证未通过: {report.all_violations}")
    """

    MIN_PASS_SCORE: float = 0.6

    def __init__(self, min_pass_score: float = 0.6):
        self._min_pass_score = min_pass_score
        self._guardrail = None

        # 尝试导入 QualityGuardrail
        try:
            from engine.runtime.quality_guardrails.quality_guardrail import QualityGuardrail
            self._guardrail = QualityGuardrail()
        except ImportError:
            logger.debug("QualityGuardrail 不可用，PolicyValidator 将返回默认结果")

    def check(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> PolicyReport:
        """执行六维度策略检查

        Args:
            text: 待检查文本
            character_masks: 角色面具字典
            chapter_goal: 章节目标
            character_names: 角色名列表
            scene_info: 场景信息
            foreshadows: 活跃伏笔列表
            era: 时代背景
            scene_type: 场景类型

        Returns:
            PolicyReport
        """
        if self._guardrail is not None:
            try:
                report = self._guardrail.check(
                    text=text,
                    character_masks=character_masks,
                    chapter_goal=chapter_goal,
                    character_names=character_names,
                    scene_info=scene_info,
                    foreshadows=foreshadows,
                    era=era,
                    scene_type=scene_type,
                )
                return PolicyReport(
                    overall_score=report.overall_score,
                    dimensions={
                        "language_style": report.language_style_score,
                        "character_consistency": report.character_consistency_score,
                        "plot_density": report.plot_density_score,
                        "naming": report.naming_score,
                        "viewpoint": report.viewpoint_score,
                        "rhythm": report.rhythm_score,
                    },
                    violations=report.all_violations,
                    passed=report.passed,
                )
            except Exception as e:
                logger.warning(f"QualityGuardrail.check 失败: {e}")
                return PolicyReport(
                    overall_score=None,
                    dimensions={},
                    violations=[
                        {"type": "validator_unavailable", "description": str(e)}
                    ],
                    status=UNEVALUATED,
                )

        # 护栏不可用时只记录未评估，绝不伪造分数或通过状态。
        return PolicyReport(
            overall_score=None,
            dimensions={},
            violations=[
                {"type": "validator_unavailable", "description": "QualityGuardrail 不可用"}
            ],
            status=UNEVALUATED,
        )

    def enforce(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
        min_score: Optional[float] = None,
    ) -> PolicyReport:
        """强制执行策略检查（Checkpoint 保存前拦截）

        不达标则抛出 PolicyViolationError

        Returns:
            PolicyReport（通过时）

        Raises:
            PolicyViolationError: 策略不达标时
        """
        report = self.check(
            text=text,
            character_masks=character_masks,
            chapter_goal=chapter_goal,
            character_names=character_names,
            scene_info=scene_info,
            foreshadows=foreshadows,
            era=era,
            scene_type=scene_type,
        )

        threshold = min_score or self._min_pass_score
        if (
            report.status != PASS
            or report.overall_score is None
            or report.overall_score < threshold
        ):
            raise PolicyViolationError(
                violations=report.all_violations,
                overall_score=report.overall_score,
            )

        return report

    def advise(
        self,
        text: str,
        character_masks: Optional[Dict[str, Any]] = None,
        chapter_goal: str = "",
        character_names: Optional[List[str]] = None,
        scene_info: Optional[Dict[str, Any]] = None,
        foreshadows: Optional[List[Dict[str, Any]]] = None,
        era: str = "ancient",
        scene_type: str = "auto",
    ) -> PolicyReport:
        """建议模式（不拦截，仅提供建议）"""
        return self.check(
            text=text,
            character_masks=character_masks,
            chapter_goal=chapter_goal,
            character_names=character_names,
            scene_info=scene_info,
            foreshadows=foreshadows,
            era=era,
            scene_type=scene_type,
        )
