"""可落库的章节 AI 审阅服务。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from application.ai.llm_json_extract import parse_llm_json_to_dict
from domain.ai.services.llm_service import LLMService
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_keys import CHAPTER_AI_REVIEW
from infrastructure.ai.prompt_utils import render_required_prompt


ReviewStatus = Literal["draft", "reviewed", "approved"]
IssueSeverity = Literal["critical", "warning", "suggestion"]


class ChapterAIReviewContractError(RuntimeError):
    """LLM 审阅结果不满足结构契约。"""


@dataclass(frozen=True)
class ChapterAIReviewIssue:
    severity: IssueSeverity
    location: str
    description: str
    suggestion: str = ""


@dataclass(frozen=True)
class ChapterAIReviewResult:
    status: ReviewStatus
    memo: str
    score: int
    summary: str
    suggestions: List[str] = field(default_factory=list)
    issues: List[ChapterAIReviewIssue] = field(default_factory=list)
    event_coverage: List[Dict[str, str]] = field(default_factory=list)
    rhythm_assessment: Dict[str, Any] = field(default_factory=dict)


def serialize_chapter_ai_review_result(result: Any) -> Dict[str, Any]:
    """Expose one JSON-safe shape to candidate and DAG gateways."""

    issues = list(getattr(result, "issues", []) or [])
    return {
        "status": str(getattr(result, "status", "reviewed") or "reviewed"),
        "score": getattr(result, "score", None),
        "summary": str(getattr(result, "summary", "") or ""),
        "suggestions": list(getattr(result, "suggestions", []) or []),
        "issues": [
            {
                "severity": str(getattr(issue, "severity", "suggestion")),
                "location": str(getattr(issue, "location", "")),
                "description": str(getattr(issue, "description", "")),
                "suggestion": str(getattr(issue, "suggestion", "")),
            }
            for issue in issues
        ],
        "event_coverage": list(getattr(result, "event_coverage", []) or []),
        "rhythm_assessment": dict(getattr(result, "rhythm_assessment", {}) or {}),
    }


class ChapterAIReviewService:
    """面向章节页和生成路由的轻量 AI 审阅服务。

    只负责“审阅正文并产出结构化结果”；是否保存审阅记录由调用方决定。
    提示词必须来自 CPMS，缺失时直接阻塞，不降级成本地隐藏提示词。
    """

    def __init__(self, llm_service: LLMService, *, model: str = "") -> None:
        self._llm_service = llm_service
        self._model = model

    async def review(
        self,
        *,
        chapter_number: int,
        chapter_title: str,
        chapter_content: str,
        chapter_outline: str = "",
        generation_hint: str = "",
        required_events: List[str] | None = None,
    ) -> ChapterAIReviewResult:
        content = (chapter_content or "").strip()
        if not content:
            raise ValueError("章节正文为空，无法进行 AI 审阅")

        prompt = render_required_prompt(
            CHAPTER_AI_REVIEW,
            {
                "chapter_number": str(chapter_number),
                "chapter_title": chapter_title or f"第{chapter_number}章",
                "chapter_content": content,
                "chapter_outline": chapter_outline or "未提供",
                "generation_hint": generation_hint or "未提供",
            },
        )
        config = generation_config_from_profile(
            "review_json",
            model=self._model,
            response_format={"type": "json_object"},
        )
        response = await self._llm_service.generate(prompt, config)
        data, errors = parse_llm_json_to_dict(response.content)
        if not data:
            raise ChapterAIReviewContractError(
                "AI 审阅未返回有效 JSON"
                + (f"：{'; '.join(errors)}" if errors else "")
            )
        return self._normalize_result(
            data,
            required_events=required_events or [],
            chapter_content=content,
            chapter_rhythm=self._extract_chapter_rhythm(chapter_outline),
        )

    def _normalize_result(
        self,
        data: Dict[str, Any],
        *,
        required_events: List[str],
        chapter_content: str,
        chapter_rhythm: Optional[Dict[str, Any]] = None,
    ) -> ChapterAIReviewResult:
        status = self._normalize_status(data.get("status"))
        score = self._normalize_score(data.get("score"))
        summary = str(data.get("summary") or "").strip()
        raw_issues = data.get("issues") if isinstance(data.get("issues"), list) else []
        issues = [self._normalize_issue(item) for item in raw_issues if isinstance(item, dict)]
        suggestions = self._normalize_suggestions(data.get("suggestions"))
        event_coverage = self._normalize_event_coverage(
            data.get("event_coverage"), required_events, chapter_content
        )
        rhythm_assessment = self._normalize_rhythm_assessment(
            data.get("rhythm_assessment"), chapter_rhythm, chapter_content
        )
        if rhythm_assessment.get("status") == "incomplete":
            missing = ", ".join(rhythm_assessment.get("missing", [])) or "required evidence"
            issues.append(
                ChapterAIReviewIssue(
                    severity="critical",
                    location="章级节奏合同",
                    description=f"{rhythm_assessment.get('chapter_function', '本章')}缺少可验证的{missing}。",
                    suggestion="回到 Candidate/作者审核，补足正文中的选择、代价、状态变化或自然交接。",
                )
            )

        if any(issue.severity == "critical" for issue in issues):
            status = "draft"
        elif status == "approved" and score < 80:
            status = "reviewed"
        if not summary:
            summary = self._summary_from_score(score, status)
        if not suggestions and issues:
            suggestions = [
                issue.suggestion or issue.description
                for issue in issues
                if issue.suggestion or issue.description
            ][:5]
        if not suggestions:
            raise ChapterAIReviewContractError("AI 审阅结果缺少可执行建议")

        memo = self._build_memo(
            status=status,
            score=score,
            summary=summary,
            issues=issues,
            suggestions=suggestions,
        )
        return ChapterAIReviewResult(
            status=status,
            memo=memo,
            score=score,
            summary=summary,
            suggestions=suggestions,
            issues=issues,
            event_coverage=event_coverage,
            rhythm_assessment=rhythm_assessment,
        )

    @staticmethod
    def _extract_chapter_rhythm(chapter_outline: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(chapter_outline or "")
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("chapter_rhythm") or data.get("rhythm")
        return value if isinstance(value, dict) and value else None

    @classmethod
    def _normalize_rhythm_assessment(
        cls,
        value: Any,
        contract: Optional[Dict[str, Any]],
        chapter_content: str,
    ) -> Dict[str, Any]:
        if not contract:
            return {}

        function = str(contract.get("chapter_function") or "").strip().lower()
        if not function:
            return {
                "status": "incomplete",
                "chapter_function": "",
                "missing": ["chapter_function"],
            }

        requirements = {
            "transition": ("handoff",),
            "aftermath": ("handoff",),
            "recovery": ("handoff",),
            "escalation": ("choice", "cost_or_risk", "state_delta"),
            "reversal": ("choice", "cost_or_risk", "state_delta"),
            "climax": ("payoff", "cost_or_risk", "state_delta"),
            "payoff": ("payoff", "cost_or_risk", "state_delta"),
            "setup": (),
            "reveal": (),
        }
        required_keys = requirements.get(function)
        if required_keys is None:
            return {
                "status": "incomplete",
                "chapter_function": function,
                "missing": ["chapter_function"],
            }
        requirements = required_keys
        assessment = value if isinstance(value, dict) else {}
        normalized: Dict[str, Any] = {
            "status": "complete",
            "chapter_function": function,
            "evidence": {},
        }
        missing: List[str] = []
        aliases = {
            "choice": ("choice", "decisive_choice"),
            "cost_or_risk": ("cost_or_risk", "cost", "risk"),
            "state_delta": ("state_delta", "chapter_delta", "delta"),
            "payoff": ("payoff", "turn_or_payoff", "turn"),
            "progress": ("progress", "goal", "understanding_change"),
            "handoff": ("handoff", "ending_hook"),
        }
        for key in requirements:
            item = next((assessment.get(alias) for alias in aliases.get(key, (key,)) if alias in assessment), None)
            item = item if isinstance(item, dict) else {}
            status = str(item.get("status") or "").strip().lower()
            evidence = str(item.get("evidence") or "").strip()
            completed = status in {"completed", "complete", "verified"} and bool(evidence) and evidence in chapter_content
            normalized["evidence"][key] = {
                "status": "completed" if completed else "unverified",
                "evidence": evidence if completed else "",
            }
            if not completed:
                missing.append(key)
        progress_keys = {
            "progress": ("progress", "state_delta", "relationship_change", "resource_change", "understanding_change", "goal_shift"),
            "reveal": ("progress", "understanding_change", "state_delta", "goal_shift"),
            "setup": ("progress", "goal", "state_delta", "understanding_change"),
        }
        if function in {"transition", "aftermath", "recovery", "setup", "reveal"}:
            progress_done = False
            for alias in progress_keys.get(function, progress_keys["progress"]):
                raw_item = assessment.get(alias)
                item = raw_item if isinstance(raw_item, dict) else {}
                evidence = str(item.get("evidence") or "").strip()
                if (
                    str(item.get("status") or "").strip().lower()
                    in {"completed", "complete", "verified"}
                    and evidence
                    and evidence in chapter_content
                ):
                    normalized["evidence"]["progress"] = {
                        "status": "completed",
                        "evidence": evidence,
                        "source": alias,
                    }
                    progress_done = True
                    break
            if not progress_done:
                missing.append("progress")
        if missing:
            normalized["status"] = "incomplete"
            normalized["missing"] = missing
        return normalized

    @staticmethod
    def _normalize_event_coverage(
        value: Any, required_events: List[str], chapter_content: str
    ) -> List[Dict[str, str]]:
        """Keep only evidence the reviewer can point to in the candidate prose."""

        if not required_events:
            return []
        items = value if isinstance(value, list) else []
        by_event = {
            str(item.get("event") or "").strip(): item
            for item in items
            if isinstance(item, dict)
        }
        coverage: List[Dict[str, str]] = []
        for event in required_events:
            item = by_event.get(event, {})
            evidence = str(item.get("evidence") or "").strip()
            status = str(item.get("status") or "unverified").strip().lower()
            completed = status == "completed" and bool(evidence) and evidence in chapter_content
            coverage.append(
                {
                    "event": event,
                    "status": "completed" if completed else "unverified",
                    "evidence": evidence if completed else "",
                }
            )
        return coverage

    @staticmethod
    def _normalize_status(value: Any) -> ReviewStatus:
        text = str(value or "").strip().lower()
        if text in {"draft", "reviewed", "approved"}:
            return text  # type: ignore[return-value]
        return "reviewed"

    @staticmethod
    def _normalize_score(value: Any) -> int:
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            score = 70
        return max(0, min(100, score))

    @staticmethod
    def _normalize_issue(item: Dict[str, Any]) -> ChapterAIReviewIssue:
        severity_raw = str(item.get("severity") or "").strip().lower()
        severity: IssueSeverity = (
            severity_raw
            if severity_raw in {"critical", "warning", "suggestion"}
            else "suggestion"
        )  # type: ignore[assignment]
        return ChapterAIReviewIssue(
            severity=severity,
            location=str(item.get("location") or "未定位").strip(),
            description=str(item.get("description") or "").strip(),
            suggestion=str(item.get("suggestion") or "").strip(),
        )

    @staticmethod
    def _normalize_suggestions(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out[:8]

    @staticmethod
    def _summary_from_score(score: int, status: ReviewStatus) -> str:
        if status == "approved":
            return f"本章整体可通过，综合评分 {score}。"
        if status == "draft":
            return f"本章存在阻塞性问题，综合评分 {score}。"
        return f"本章需要修改后再确认，综合评分 {score}。"

    @staticmethod
    def _build_memo(
        *,
        status: ReviewStatus,
        score: int,
        summary: str,
        issues: List[ChapterAIReviewIssue],
        suggestions: List[str],
    ) -> str:
        lines = [
            f"AI 审阅状态：{status}",
            f"综合评分：{score}",
            f"总体判断：{summary}",
            "",
            "主要问题：",
        ]
        if issues:
            for issue in issues[:8]:
                desc = issue.description or "未描述"
                suggestion = f"；建议：{issue.suggestion}" if issue.suggestion else ""
                lines.append(f"- [{issue.severity}] {issue.location}：{desc}{suggestion}")
        else:
            lines.append("- 未发现阻塞性问题。")
        lines.extend(["", "修改建议："])
        lines.extend(f"- {item}" for item in suggestions)
        return "\n".join(lines).strip()
