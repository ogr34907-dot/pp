"""章级执行规划 — Pydantic Schema（与 DAG 端口 JSON 对齐，可版本演进）"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PlanDecompositionMode(StrEnum):
    """章前规划拆解来源。

    统一枚举可以避免调用链散落字符串常量，后续新增拆解器时只需扩展这里。
    """

    BEAT_SHEET = "beat_sheet"
    STRUCTURED_OUTLINE = "structured_outline"
    LLM_OUTLINE_DECOMPOSE = "llm_outline_decompose"
    RAW_OUTLINE_SINGLE = "raw_outline_single"
    EMPTY_OUTLINE = "empty_outline"
    ERROR_SINGLE_OUTLINE = "error_single_outline"


class ChapterFunction(StrEnum):
    """章级叙事职责；不是逐拍 function 的替代品。"""

    SETUP = "setup"
    TRANSITION = "transition"
    ESCALATION = "escalation"
    REVERSAL = "reversal"
    CLIMAX = "climax"
    AFTERMATH = "aftermath"
    PAYOFF = "payoff"
    RECOVERY = "recovery"
    REVEAL = "reveal"


class IntensityLevel(StrEnum):
    """只表达强度，不混入 release/unresolved 等节奏状态。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PEAK = "peak"


class PlanningEnvelope(BaseModel):
    """章约束信封：标识与预算，不承载叙事条文。"""

    novel_id: Optional[str] = None
    chapter_number: Optional[int] = None
    target_chapter_words: int = Field(default=2500, ge=200, le=100_000)
    source_outline_hash: Optional[str] = Field(
        default=None,
        description="可选：章纲指纹，便于缓存/幂等",
    )


class PlanAtomSpec(BaseModel):
    """最小叙事推进单元（抽象节拍规格），投影为下游 Beat / 提示词块。"""

    id: str = Field(min_length=1, max_length=64)
    intent: str = Field(
        min_length=1,
        description="该拍在叙事上要完成什么（非句法切分，而是事件/推进单元）",
    )
    weight: float = Field(default=1.0, ge=0.01, le=100.0, description="相对字数权重，供预算分配")
    source_hint: Optional[str] = Field(
        default=None,
        description="可选：引用章纲片段，仅作调试/溯源",
    )
    extensions: Dict[str, Any] = Field(default_factory=dict)


class ChapterRhythmContract(BaseModel):
    """单份章级节奏合同；逐拍 Beat 只保存自己的局部字段。"""

    chapter_function: Optional[ChapterFunction] = None
    intensity_curve: Optional[List[IntensityLevel]] = Field(
        default=None,
        min_length=1,
        max_length=8,
    )
    chapter_goal: Optional[str] = None
    decisive_choice: Optional[str] = None
    cost_or_risk: Optional[str] = None
    chapter_delta: Optional[str] = None
    turn_or_payoff: Optional[str] = None
    ending_hook: Optional[str] = None


_RHYTHM_FIELDS = {
    "chapter_function",
    "intensity_curve",
    "chapter_goal",
    "decisive_choice",
    "cost_or_risk",
    "chapter_delta",
    "turn_or_payoff",
    "ending_hook",
}
_EXPLICIT_RHYTHM_FIELDS = _RHYTHM_FIELDS - {"ending_hook"}

_CHAPTER_FUNCTION_ALIASES = {
    "setup": "setup",
    "establish": "setup",
    "transition": "transition",
    "bridge": "transition",
    "escalation": "escalation",
    "pressure": "escalation",
    "reversal": "reversal",
    "turn": "reversal",
    "reverse": "reversal",
    "climax": "climax",
    "aftermath": "aftermath",
    "release": "aftermath",
    "payoff": "payoff",
    "recovery": "recovery",
    "restoration": "recovery",
    "reveal": "reveal",
    "setup/establish": "setup",
    "过渡": "transition",
    "余波": "aftermath",
    "恢复": "recovery",
    "升级": "escalation",
    "反转": "reversal",
    "高潮": "climax",
    "兑现": "payoff",
    "揭示": "reveal",
}

_INTENSITY_ALIASES = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "peak": "peak",
    "低": "low",
    "中": "medium",
    "高": "high",
    "峰值": "peak",
}


def _mapping(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method_name in ("model_dump", "canonical_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                dumped = method(mode="python")
            if isinstance(dumped, dict):
                return dumped
    return {}


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_function(value: Any) -> Optional[str]:
    text = _text_or_none(value)
    return _CHAPTER_FUNCTION_ALIASES.get(text.lower()) if text else None


def _normalize_intensity_curve(value: Any) -> Optional[List[str]]:
    if isinstance(value, str):
        value = [part for part in value.replace("→", ",").replace(">", ",").split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return None
    levels: List[str] = []
    for item in value:
        key = _text_or_none(item)
        level = _INTENSITY_ALIASES.get(key.lower()) if key else None
        if level is None:
            return None
        levels.append(level)
    return levels or None


def _rhythm_source(payload: Any) -> Dict[str, Any]:
    """Read explicit rhythm from raw payload or OutlinePayload.extra.

    A legacy chapter with only the old outline fields returns an empty mapping;
    this is deliberate so no chapter function is guessed.
    """

    data = _mapping(payload)
    sources = [data]
    extra = data.get("extra")
    if isinstance(extra, dict):
        sources.append(extra)

    for source in sources:
        for key in ("chapter_rhythm", "rhythm"):
            nested = source.get(key)
            if isinstance(nested, dict) and nested:
                return {**source, **nested}

    explicit: Dict[str, Any] = {}
    for source in sources:
        for key in _EXPLICIT_RHYTHM_FIELDS:
            if key in source:
                explicit[key] = source[key]
    return explicit


def chapter_rhythm_from_outline_payload(payload: Any) -> Optional[ChapterRhythmContract]:
    """Normalize one explicit published-outline rhythm into the plan contract."""

    source = _rhythm_source(payload)
    if not source:
        return None

    values: Dict[str, Any] = {}
    function = _normalize_function(source.get("chapter_function"))
    if function:
        values["chapter_function"] = function

    curve = _normalize_intensity_curve(source.get("intensity_curve"))
    if curve:
        values["intensity_curve"] = curve

    for key in _RHYTHM_FIELDS - {"chapter_function", "intensity_curve"}:
        value = _text_or_none(source.get(key))
        if value:
            values[key] = value

    # Existing five-level fields are projected only after an explicit rhythm
    # field/object is present; old outlines therefore remain rhythm=None.
    if "chapter_goal" not in values:
        goal = _text_or_none(source.get("creative_goal"))
        if goal:
            values["chapter_goal"] = goal
    if "ending_hook" not in values:
        hook = _text_or_none(source.get("ending_hook"))
        if hook:
            values["ending_hook"] = hook

    return ChapterRhythmContract.model_validate(values) if values else None


def serialize_chapter_rhythm(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, ChapterRhythmContract):
        contract = value
    else:
        contract = chapter_rhythm_from_outline_payload({"rhythm": _mapping(value)})
    if contract is None:
        return None
    return contract.model_dump(mode="json", exclude_none=True)


def render_chapter_rhythm_block(value: Any) -> str:
    """Render the compact contract shared by script and prose prompts."""

    data = serialize_chapter_rhythm(value)
    if not data:
        return ""
    function = data.get("chapter_function")
    guidance = {
        "transition": "允许舒缓，但必须让关系、信息、资源或目标至少发生一项可见推进，并留下自然交接。",
        "aftermath": "承担余波和状态重排，不强求大冲突，但必须留下推进与交接。",
        "recovery": "允许恢复和消化，但必须改变关系、资源、目标或情绪债中的至少一项。",
        "escalation": "让阻力升级，通过行动体现选择，并落下具体代价或风险增量。",
        "reversal": "让前置线索支撑局势反转，选择和代价必须能在正文中观察到。",
        "climax": "完成前置承诺的兑现，让核心冲突实质变化，并写清代价与结果。",
        "payoff": "兑现前置承诺，交代选择的代价和结果，同时留下新的驱动力。",
        "setup": "建立可行动的目标、规则或悬念，不写成脱离人物行动的设定说明。",
        "reveal": "让揭示改变人物或读者对事实的理解、目标或选择，不止于解释背景。",
    }.get(function, "用行动、选择、代价和可观察的状态变化完成本章职责。")
    return f"{json.dumps(data, ensure_ascii=False, sort_keys=True)}\n执行原则：{guidance}"


class ChapterExecutionPlan(BaseModel):
    """章前规划根文档 — DAG `chapter_plan_json` 的标准外形。"""

    schema_version: str = Field(default="plotpilot.chapter_plan.v1")
    envelope: PlanningEnvelope
    atoms: List[PlanAtomSpec] = Field(default_factory=list)
    rhythm: Optional[ChapterRhythmContract] = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="可选的单份章级叙事职责合同；旧计划缺失时保持 None",
    )
    extensions: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(
        default_factory=dict,
        description="如 node_type、decomposition_mode、model 等可追溯信息",
    )
